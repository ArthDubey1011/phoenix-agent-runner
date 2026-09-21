"""Agent loop tests. MockProvider only: no test here can reach a real LLM."""

import asyncio
import shutil
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from hypothesis import given
from hypothesis import strategies as st

from phoenix.agent import UnsafePathError, safe_join
from phoenix.engine import EngineContext, execute_task, run_worker, submit_run
from phoenix.leases import claim_task, reap_expired
from phoenix.llm.base import LLMResponse
from phoenix.llm.cache import complete_cached
from phoenix.llm.mock import MOCK_COST, MockProvider, get_scenario
from phoenix.store import read_events


def spec(scenario: str, **extra: int) -> dict[str, Any]:
    return {"kind": "agent", "prompt": "print hello", "mock_scenario": scenario, **extra}


async def drive(
    pool: asyncpg.Pool, ctx: EngineContext, run_id: uuid.UUID, worker: str = "w1"
) -> None:
    claim = await claim_task(pool, worker, 60)
    assert claim is not None and claim.run_id == run_id
    await execute_task(pool, claim, ctx)


async def status(pool: asyncpg.Pool, run_id: uuid.UUID) -> str:
    return str(await pool.fetchval("SELECT status FROM runs WHERE id=$1", run_id))


async def calls(pool: asyncpg.Pool, run_id: uuid.UUID) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM provider_calls WHERE run_id=$1", run_id))


async def test_fail_twice_then_fix_succeeds(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    run_id = await submit_run(pool, spec("fail_twice_then_fix"))
    await drive(pool, ctx, run_id)
    events = await read_events(pool, run_id)
    exit_codes = [
        e.payload["observation"]["exit_code"]
        for e in events
        if e.payload["action"]["tool"] == "run_code"
    ]
    assert exit_codes == [1, 1, 0]
    assert "ZeroDivisionError" in events[1].payload["observation"]["stderr"]
    assert "NameError" in events[3].payload["observation"]["stderr"]
    assert events[-1].type == "finish" and await status(pool, run_id) == "succeeded"
    assert await calls(pool, run_id) == 7
    assert await pool.fetchval("SELECT spent_usd FROM runs WHERE id=$1", run_id) == 7 * MOCK_COST


async def test_budget_cap_stops_run(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    run_id = await submit_run(pool, spec("endless_progress"), budget_usd="0.001")
    await drive(pool, ctx, run_id)
    events = await read_events(pool, run_id)
    assert events[-1].type == "budget_exceeded"
    assert await status(pool, run_id) == "budget_exceeded"
    assert await calls(pool, run_id) == 3  # 3 * 0.00035 >= 0.001; no 4th call
    spent = await pool.fetchval("SELECT spent_usd FROM runs WHERE id=$1", run_id)
    assert Decimal("0.001") <= spent < Decimal("0.001") + MOCK_COST  # overshoot < one call


async def test_loop_detection_stops_run(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    run_id = await submit_run(pool, spec("stuck_loop"))
    await drive(pool, ctx, run_id)
    events = await read_events(pool, run_id)
    assert events[-1].type == "failed" and "loop" in events[-1].payload["reason"]
    assert await status(pool, run_id) == "failed"
    assert await calls(pool, run_id) == 4  # write + 3 identical run_code, then stopped


async def test_max_steps_stops_run(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    run_id = await submit_run(pool, spec("endless_progress", max_steps=5), budget_usd="10")
    await drive(pool, ctx, run_id)
    events = await read_events(pool, run_id)
    assert events[-1].type == "failed" and "max_steps" in events[-1].payload["reason"]
    assert await calls(pool, run_id) == 5


async def test_path_traversal_is_rejected(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    run_id = await submit_run(pool, spec("write_outside"))
    await drive(pool, ctx, run_id)
    events = await read_events(pool, run_id)
    assert "unsafe path" in events[0].payload["observation"]["error"]
    assert not list(ctx.workspace_root.rglob("evil.txt"))


class CrashingProvider(MockProvider):
    """Raises on the Nth call, like a worker dying right after it asked the model."""

    def __init__(self, script: list[dict[str, Any]], crash_on: int) -> None:
        super().__init__(script)
        self.crash_on = crash_on

    async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        if self.calls + 1 == self.crash_on:
            self.calls += 1
            raise RuntimeError("simulated crash")
        return await super().complete(messages)


async def test_resume_rebuilds_workspace_and_repeats_only_the_crash_window(
    pool: asyncpg.Pool, ctx: EngineContext
) -> None:
    run_id = await submit_run(pool, spec("fail_twice_then_fix"))
    script = get_scenario("fail_twice_then_fix").script
    claim = await claim_task(pool, "w1", 60)
    assert claim is not None
    with pytest.raises(RuntimeError):
        await execute_task(
            pool, claim, EngineContext(CrashingProvider(script, 4), ctx.sandbox, ctx.workspace_root)
        )
    assert len(await read_events(pool, run_id)) == 3  # steps 0..2 committed

    shutil.rmtree(ctx.workspace_root)  # the worker's disk is gone; events are the truth
    await pool.execute("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")
    assert await reap_expired(pool) == 1
    await drive(pool, ctx, run_id, "w2")

    events = await read_events(pool, run_id)
    assert [e.seq for e in events] == list(range(1, 8))
    assert await status(pool, run_id) == "succeeded"
    rows = await pool.fetch(
        "SELECT step_index, count(*) AS n FROM provider_calls WHERE run_id=$1 "
        "GROUP BY step_index ORDER BY step_index",
        run_id,
    )
    # every step called once, except step 3: it was asked before the crash, then again after
    # (the documented crash window: response never reached the cache)
    assert {r["step_index"]: r["n"] for r in rows} == {0: 1, 1: 1, 2: 1, 3: 2, 4: 1, 5: 1, 6: 1}
    # the final exit code proves the workspace was rebuilt from events: main.py existed
    assert events[5].payload["observation"]["exit_code"] == 0


async def test_llm_cache_prevents_a_second_provider_call(pool: asyncpg.Pool) -> None:
    run_id = await submit_run(pool, spec("fail_twice_then_fix"))
    provider = get_scenario("fail_twice_then_fix")
    msgs = [{"role": "user", "content": "hi"}]
    a = await complete_cached(pool, provider, run_id, 0, msgs)
    b = await complete_cached(pool, provider, run_id, 0, msgs)  # e.g. after a crash + resume
    assert a == b and provider.calls == 1 and await calls(pool, run_id) == 1


async def test_poison_run_fails_after_max_attempts(pool: asyncpg.Pool, ctx: EngineContext) -> None:
    class Boom(MockProvider):
        async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
            raise RuntimeError("provider down")

    run_id = await submit_run(pool, spec("fail_twice_then_fix"))
    stop = asyncio.Event()
    bad = EngineContext(Boom([]), ctx.sandbox, ctx.workspace_root)
    task = asyncio.create_task(
        run_worker(
            pool, "w", ctx=bad, max_attempts=3, retry_backoff=0.05, poll_interval=0.05, stop=stop
        )
    )
    try:
        async with asyncio.timeout(20):
            while await status(pool, run_id) != "failed":
                await asyncio.sleep(0.1)
    finally:
        stop.set()
        await task
    events = await read_events(pool, run_id)
    assert events[-1].type == "failed" and "3 attempts" in events[-1].payload["reason"]
    assert await pool.fetchval("SELECT attempt FROM tasks WHERE run_id=$1", run_id) == 3


async def test_real_llm_is_disabled_by_default(
    pool: asyncpg.Pool, ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phoenix.llm.anthropic import AnthropicProvider

    monkeypatch.delenv("PHOENIX_REAL_LLM", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        AnthropicProvider()


def test_anthropic_message_translation() -> None:
    from phoenix.llm.anthropic import to_anthropic_messages

    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "action": {"tool": "run_code", "args": {"path": "a.py"}}},
        {"role": "user", "observation": {"exit_code": 0}},
    ]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][0]["id"] == out[2]["content"][0]["tool_use_id"]


@given(st.text(max_size=40))
def test_safe_join_never_escapes(rel: str) -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        try:
            target = safe_join(root, rel)
        except UnsafePathError:
            return
        assert target.is_relative_to(root)


async def test_fatal_provider_error_fails_the_run_immediately(
    pool: asyncpg.Pool, ctx: EngineContext
) -> None:
    class Quota(RuntimeError):
        fatal = True

    class Broke(MockProvider):
        async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
            raise Quota("daily quota exhausted")

    run_id = await submit_run(pool, spec("fail_twice_then_fix"))
    stop = asyncio.Event()
    bad = EngineContext(Broke([]), ctx.sandbox, ctx.workspace_root)
    task = asyncio.create_task(
        run_worker(pool, "w", ctx=bad, max_attempts=5, poll_interval=0.05, stop=stop)
    )
    try:
        async with asyncio.timeout(20):
            while await status(pool, run_id) != "failed":
                await asyncio.sleep(0.1)
    finally:
        stop.set()
        await task
    events = await read_events(pool, run_id)
    assert events[-1].payload["reason"].startswith("fatal error: daily quota")
    assert (
        await pool.fetchval("SELECT attempt FROM tasks WHERE run_id=$1", run_id) == 1
    )  # no retries
