import uuid
from decimal import Decimal

import asyncpg

from phoenix.engine import EngineContext, StepOutcome, commit_step, execute_task, submit_run
from phoenix.leases import claim_task, reap_expired
from phoenix.timeline import build_timeline


async def test_timeline_marks_takeover(pool: asyncpg.Pool) -> None:
    run_id = await submit_run(pool, {"kind": "toy", "steps": 4})
    cost = Decimal("0.0001")

    def out(i: int) -> StepOutcome:
        return StepOutcome("toy_step", {"i": i, "cost_usd": str(cost)}, cost, final=i == 3)

    a = await claim_task(pool, "A", 60)
    assert a is not None
    await commit_step(pool, a, 0, out(0))
    await commit_step(pool, a, 1, out(1))
    await pool.execute("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")
    await reap_expired(pool)
    b = await claim_task(pool, "B", 60)
    assert b is not None
    await commit_step(pool, b, 2, out(2))
    await commit_step(pool, b, 3, out(3))

    text = await build_timeline(pool, run_id)
    assert "status=succeeded" in text and "attempts=2" in text
    assert text.count("seq=") == 4
    assert "-- takeover: A#1 -> B#2" in text
    assert text.index("A#1") < text.index("takeover") < text.rindex("B#2")
    assert "finished after" in text


async def test_timeline_shows_agent_steps_and_errors(
    pool: asyncpg.Pool, ctx: EngineContext
) -> None:
    spec = {"kind": "agent", "prompt": "x", "mock_scenario": "fail_twice_then_fix"}
    run_id = await submit_run(pool, spec)
    claim = await claim_task(pool, "w1", 60)
    assert claim is not None
    await execute_task(pool, claim, ctx)
    text = await build_timeline(pool, run_id)
    assert "write_file main.py" in text
    assert "run_code main.py -> exit 1 | ZeroDivisionError" in text
    assert "NameError" in text and "run_code main.py -> exit 0" in text
    assert "finish" in text and "takeover" not in text


async def test_timeline_unknown_run(pool: asyncpg.Pool) -> None:
    assert "not found" in await build_timeline(pool, uuid.uuid4())
