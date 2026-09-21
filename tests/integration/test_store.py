import asyncio
import random
import uuid

import asyncpg

from phoenix.store import append_event, create_run, read_events, step_key


async def test_same_step_twice_yields_one_event(pool: asyncpg.Pool) -> None:
    run_id = await create_run(pool, {"task": "x"}, budget_usd="1.0")
    key = step_key(run_id, 0)
    async with pool.acquire() as conn:
        first = await append_event(conn, run_id, key, "step", {"n": 1})
        second = await append_event(conn, run_id, key, "step", {"n": 2})
    assert first.inserted and not second.inserted
    assert first.seq == second.seq
    events = await read_events(pool, run_id)
    assert len(events) == 1
    assert events[0].payload == {"n": 1}  # first write wins


async def test_20_concurrent_writers_one_step_exactly_one_event(pool: asyncpg.Pool) -> None:
    run_id = await create_run(pool, {}, budget_usd="1.0")
    key = step_key(run_id, 7)

    async def writer(i: int) -> bool:
        async with pool.acquire() as conn:
            return (await append_event(conn, run_id, key, "step", {"writer": i})).inserted

    results = await asyncio.gather(*(writer(i) for i in range(20)))
    assert sum(results) == 1
    assert len(await read_events(pool, run_id)) == 1


async def test_concurrent_distinct_steps_have_gapless_seq(pool: asyncpg.Pool) -> None:
    run_id = await create_run(pool, {}, budget_usd="1.0")
    steps = list(range(30))
    random.Random(1234).shuffle(steps)  # seeded so a failure reproduces

    async def writer(step: int) -> None:
        async with pool.acquire() as conn:
            await append_event(conn, run_id, step_key(run_id, step), "step", {"s": step})

    await asyncio.gather(*(writer(s) for s in steps))
    events = await read_events(pool, run_id)
    assert [e.seq for e in events] == list(range(1, 31))
    assert len({e.idem_key for e in events}) == 30


async def test_unknown_run_raises(pool: asyncpg.Pool) -> None:
    import pytest

    from phoenix.store import RunNotFoundError

    missing = uuid.uuid4()
    async with pool.acquire() as conn:
        with pytest.raises(RunNotFoundError):
            await append_event(conn, missing, step_key(missing, 0), "step", {})
