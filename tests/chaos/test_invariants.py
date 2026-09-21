"""Each invariant must pass on a clean run and catch its own kind of corruption."""

import uuid
from decimal import Decimal

import asyncpg

from phoenix.chaos import invariants as inv
from phoenix.engine import StepOutcome, commit_step, submit_run
from phoenix.leases import claim_task

COST = Decimal("0.0001")


async def finished_run(pool: asyncpg.Pool, steps: int = 4) -> uuid.UUID:
    run_id = await submit_run(pool, {"kind": "toy", "steps": steps})
    claim = await claim_task(pool, "A", 60)
    assert claim is not None
    for i in range(steps):
        out = StepOutcome("toy_step", {"i": i, "cost_usd": str(COST)}, COST, final=i == steps - 1)
        await commit_step(pool, claim, i, out)
    return run_id


async def test_clean_run_passes_all(pool: asyncpg.Pool) -> None:
    run_id = await finished_run(pool)
    results = await inv.check_all(pool, [run_id])
    assert [r.passed for r in results] == [True] * 4


async def test_termination_detects_stuck_run(pool: asyncpg.Pool) -> None:
    run_id = await submit_run(pool, {"kind": "toy"})
    res = await inv.termination(pool, [run_id])
    assert not res.passed and "stuck" in res.violations[0]


async def test_log_integrity_detects_seq_gap(pool: asyncpg.Pool) -> None:
    run_id = await finished_run(pool)
    await pool.execute("DELETE FROM events WHERE run_id=$1 AND seq=2", run_id)
    res = await inv.log_integrity(pool, [run_id])
    assert not res.passed and "gaps" in res.violations[0]


async def test_log_integrity_detects_stale_writer(pool: asyncpg.Pool) -> None:
    run_id = await finished_run(pool)
    await pool.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{meta,attempt}', '0') "
        "WHERE run_id=$1 AND seq=3",
        run_id,
    )
    await pool.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{meta,attempt}', '2') "
        "WHERE run_id=$1 AND seq=2",
        run_id,
    )
    res = await inv.log_integrity(pool, [run_id])
    assert not res.passed and "stale attempt" in res.violations[0]


async def test_log_integrity_detects_event_after_final(pool: asyncpg.Pool) -> None:
    run_id = await finished_run(pool)
    await pool.execute(
        "INSERT INTO events(run_id, seq, type, payload, idem_key) "
        "VALUES ($1, 5, 'toy_step', '{}', $2)",
        run_id,
        f"{run_id}:4",
    )
    res = await inv.log_integrity(pool, [run_id])
    assert not res.passed and "after the final" in res.violations[0]


async def test_double_billing_same_attempt_fails_later_attempt_is_a_reported_retry(
    pool: asyncpg.Pool,
) -> None:
    run_id = await finished_run(pool)
    ins = "INSERT INTO provider_calls(run_id, step_index, cache_key, attempt) VALUES ($1,$2,'k',$3)"
    await pool.execute(ins, run_id, 0, 1)
    await pool.execute(ins, run_id, 1, 1)
    await pool.execute(ins, run_id, 1, 2)  # step 1 asked again by a later attempt
    res = await inv.no_double_billing(pool, [run_id])
    assert res.passed and len(res.retries) == 1 and "step 1" in res.retries[0]

    await pool.execute(ins, run_id, 0, 1)  # same attempt twice: real double billing
    res = await inv.no_double_billing(pool, [run_id])
    assert not res.passed and "step 0" in res.violations[0]


async def test_spend_consistency_detects_drift(pool: asyncpg.Pool) -> None:
    run_id = await finished_run(pool)
    await pool.execute("UPDATE runs SET spent_usd = spent_usd + 0.5 WHERE id=$1", run_id)
    res = await inv.spend_consistency(pool, [run_id])
    assert not res.passed and "spent_usd" in res.violations[0]
