import asyncio
import random
from decimal import Decimal

import asyncpg
import pytest

from phoenix.engine import StepOutcome, commit_step, submit_run
from phoenix.leases import LeaseLostError, claim_task, heartbeat, reap_expired
from phoenix.store import read_events

SPEC = {"kind": "toy", "steps": 3}


async def test_submit_creates_run_and_task_together(pool: asyncpg.Pool) -> None:
    run_id = await submit_run(pool, SPEC)
    assert await pool.fetchval("SELECT count(*) FROM tasks WHERE run_id=$1", run_id) == 1


async def test_concurrent_claims_never_double_claim(pool: asyncpg.Pool) -> None:
    rng = random.Random(42)  # seeded: which worker asks first varies, reproducibly
    run_ids = {await submit_run(pool, SPEC) for _ in range(10)}
    workers = [f"w{i}" for i in range(25)]
    rng.shuffle(workers)
    claims = await asyncio.gather(*(claim_task(pool, w, 30) for w in workers))
    got = [c for c in claims if c is not None]
    assert len(got) == 10
    assert {c.run_id for c in got} == run_ids  # each task exactly once


async def _expire(pool: asyncpg.Pool) -> None:
    await pool.execute("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")


async def test_zombie_commit_is_rejected_after_takeover(pool: asyncpg.Pool) -> None:
    """Fencing: A loses its lease, B takes over (attempt 2); A's late commit must fail."""
    run_id = await submit_run(pool, SPEC)
    a = await claim_task(pool, "A", 30)
    assert a is not None and a.attempt == 1
    assert await commit_step(pool, a, 0, StepOutcome("s", {"by": "A"}, Decimal("0.0001")))

    await _expire(pool)
    assert await reap_expired(pool) == 1
    b = await claim_task(pool, "B", 30)
    assert b is not None and b.attempt == 2

    with pytest.raises(LeaseLostError):  # A wakes up and tries to commit step 1
        await commit_step(pool, a, 1, StepOutcome("s", {"by": "A"}, Decimal("0.0001")))
    assert len(await read_events(pool, run_id)) == 1  # nothing written, spend untouched
    assert await pool.fetchval("SELECT spent_usd FROM runs WHERE id=$1", run_id) == Decimal(
        "0.0001"
    )

    assert await commit_step(pool, b, 1, StepOutcome("s", {"by": "B"}, Decimal("0.0001")))
    assert [e.payload["by"] for e in await read_events(pool, run_id)] == ["A", "B"]


async def test_expired_but_unreaped_lease_cannot_commit(pool: asyncpg.Pool) -> None:
    await submit_run(pool, SPEC)
    a = await claim_task(pool, "A", 30)
    assert a is not None
    await _expire(pool)
    with pytest.raises(LeaseLostError):
        await commit_step(pool, a, 0, StepOutcome("s", {}))


async def test_heartbeat_extends_and_fails_once_lost(pool: asyncpg.Pool) -> None:
    await submit_run(pool, SPEC)
    a = await claim_task(pool, "A", 5)
    assert a is not None
    assert await heartbeat(pool, a, 60)
    assert await pool.fetchval("SELECT lease_expires_at > now() + interval '30 seconds' FROM tasks")
    await _expire(pool)
    await reap_expired(pool)
    assert not await heartbeat(pool, a, 60)


async def test_reaper_ignores_live_leases(pool: asyncpg.Pool) -> None:
    await submit_run(pool, SPEC)
    await claim_task(pool, "A", 30)
    assert await reap_expired(pool) == 0


async def test_final_commit_completes_task_and_run(pool: asyncpg.Pool) -> None:
    run_id = await submit_run(pool, SPEC)
    a = await claim_task(pool, "A", 30)
    assert a is not None
    await commit_step(pool, a, 0, StepOutcome("s", {}, final=True))
    row = await pool.fetchrow(
        "SELECT r.status, t.status AS ts FROM runs r JOIN tasks t ON t.run_id=r.id WHERE r.id=$1",
        run_id,
    )
    assert (row["status"], row["ts"]) == ("succeeded", "done")
