"""Durable step executor: replay events, run the next step, commit it atomically."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import asyncpg

from phoenix.leases import (
    Claim,
    LeaseLostError,
    assert_lease,
    claim_task,
    enqueue_task,
    fencing_enabled,
    heartbeat,
    reap_expired,
    release_task,
)
from phoenix.llm.base import LLMProvider
from phoenix.sandbox.runner import Sandbox
from phoenix.store import Event, append_event, insert_run, read_events, step_key


@dataclass(frozen=True)
class StepOutcome:
    type: str
    payload: dict[str, Any]
    cost_usd: Decimal = Decimal(0)
    final: bool = False  # last step: also completes the task and finishes the run
    final_status: str = "succeeded"  # run status when final: succeeded|failed|budget_exceeded


class Workflow(Protocol):
    async def next_step(self, claim: Claim, step_index: int, events: list[Event]) -> StepOutcome:
        """Compute the step for `step_index` from the replayed events."""
        ...


class ToyWorkflow:
    """N deterministic steps with a small delay each (so tests can kill mid-run)."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.steps = int(spec.get("steps", 20))
        self.delay = float(spec.get("step_delay", 0.0))
        self.cost = Decimal(str(spec.get("step_cost", "0.0001")))

    async def next_step(self, claim: Claim, step_index: int, events: list[Event]) -> StepOutcome:
        await asyncio.sleep(self.delay)
        return StepOutcome(
            type="toy_step",
            payload={
                "i": step_index,
                "value": step_index**2,
                "worker": claim.worker_id,
                "attempt": claim.attempt,
                "cost_usd": str(self.cost),
            },
            cost_usd=self.cost,
            final=step_index == self.steps - 1,
        )


@dataclass
class EngineContext:
    """Dependencies for workflows that need them. Built per worker process."""

    provider: LLMProvider | None = None  # None: choose per run (mock scenario / real LLM)
    sandbox: Sandbox | None = None
    workspace_root: Path = Path("workspaces")


def _provider_for(spec: dict[str, Any], ctx: EngineContext) -> LLMProvider:
    if ctx.provider is not None:
        return ctx.provider
    if os.environ.get("PHOENIX_REAL_LLM") == "1":
        from phoenix.llm.factory import real_provider

        return real_provider()
    if "mock_script" in spec:  # inline script, used by the eval suite
        from phoenix.llm.mock import MockProvider

        return MockProvider(spec["mock_script"])
    if "mock_scenario" in spec:
        from phoenix.llm.mock import get_scenario

        return get_scenario(spec["mock_scenario"])
    raise RuntimeError("no provider: set PHOENIX_REAL_LLM=1 or give the run a mock_scenario")


def workflow_for(spec: dict[str, Any], pool: asyncpg.Pool, ctx: EngineContext) -> Workflow:
    kind = spec.get("kind")
    if kind == "toy":
        return ToyWorkflow(spec)
    if kind == "agent":
        from phoenix.agent import AgentWorkflow  # local import: agent depends on this module
        from phoenix.sandbox.runner import DockerRunner

        sandbox = ctx.sandbox or DockerRunner()
        return AgentWorkflow(spec, pool, _provider_for(spec, ctx), sandbox, ctx.workspace_root)
    raise ValueError(f"unknown workflow kind: {kind!r}")


async def submit_run(
    pool: asyncpg.Pool, spec: dict[str, Any], budget_usd: str | Decimal = "1.0"
) -> uuid.UUID:
    """Create the run and its task in one transaction: no run without a task."""
    async with pool.acquire() as conn, conn.transaction():
        run_id = await insert_run(conn, spec, budget_usd)
        await enqueue_task(conn, run_id)
        return run_id


async def commit_step(
    pool: asyncpg.Pool, claim: Claim, step_index: int, outcome: StepOutcome
) -> bool:
    """ONE transaction: fence check + append event + update spend (+ complete task/run).
    Raises LeaseLostError if the caller is a zombie. Returns False if the step was
    already committed (idempotent no-op)."""
    async with pool.acquire() as conn, conn.transaction():
        await assert_lease(conn, claim)
        # "meta" records who wrote the event; chaos invariant #2 uses it to spot stale writers.
        payload = {
            **outcome.payload,
            "meta": {"worker": claim.worker_id, "attempt": claim.attempt, "final": outcome.final},
        }
        res = await append_event(
            conn, claim.run_id, step_key(claim.run_id, step_index), outcome.type, payload
        )
        if not res.inserted:
            return False
        await conn.execute(
            "UPDATE runs SET spent_usd = spent_usd + $2 WHERE id=$1", claim.run_id, outcome.cost_usd
        )
        if outcome.final:
            await conn.execute("UPDATE tasks SET status='done' WHERE id=$1", claim.task_id)
            await conn.execute(
                "UPDATE runs SET status=$2, finished_at=now() WHERE id=$1",
                claim.run_id,
                outcome.final_status,
            )
        return True


async def execute_task(pool: asyncpg.Pool, claim: Claim, ctx: EngineContext | None = None) -> None:
    spec = json.loads(
        await pool.fetchval("SELECT task_spec::text FROM runs WHERE id=$1", claim.run_id)
    )
    workflow = workflow_for(spec, pool, ctx or EngineContext())
    while True:
        events = await read_events(pool, claim.run_id)  # replay = state
        step_index = len(events)
        outcome = await workflow.next_step(claim, step_index, events)
        await commit_step(pool, claim, step_index, outcome)
        if outcome.final:
            return


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _handle_error(
    pool: asyncpg.Pool, claim: Claim, exc: Exception, max_attempts: int, backoff: float
) -> None:
    """A step raised. Retry (fresh attempt, replaying events) until max_attempts, then fail
    the run so a poison run cannot retry forever."""
    fatal = bool(getattr(exc, "fatal", False))  # e.g. a daily quota: retrying is pointless
    if claim.attempt < max_attempts and not fatal:
        await release_task(pool, claim, backoff)
        return
    index = await pool.fetchval("SELECT count(*) FROM events WHERE run_id=$1", claim.run_id)
    outcome = StepOutcome(
        type="failed",
        payload={
            "reason": (
                f"fatal error: {exc}"
                if fatal
                else f"worker error after {claim.attempt} attempts: {exc!r}"
            )
        },
        final=True,
        final_status="failed",
    )
    try:
        await commit_step(pool, claim, int(index), outcome)
    except LeaseLostError:
        pass  # someone else owns the task now; they will hit the same error and decide


async def _heartbeat_loop(
    pool: asyncpg.Pool, claim: Claim, lease_seconds: float, main: asyncio.Task[None]
) -> None:
    while True:
        await asyncio.sleep(lease_seconds / 3)
        if not await heartbeat(pool, claim, lease_seconds) and fencing_enabled():
            _log(f"LEASE_LOST worker={claim.worker_id} run={claim.run_id} (heartbeat)")
            main.cancel()
            return


async def _reaper_loop(pool: asyncpg.Pool, interval: float) -> None:
    while True:
        n = await reap_expired(pool)
        if n:
            _log(f"REAPED {n} expired lease(s)")
        await asyncio.sleep(interval)


async def run_worker(
    pool: asyncpg.Pool,
    worker_id: str,
    *,
    lease_seconds: float = 5.0,
    poll_interval: float = 0.2,
    stop: asyncio.Event | None = None,
    ctx: EngineContext | None = None,
    max_attempts: int = 5,
    retry_backoff: float = 0.5,
) -> None:
    stop = stop or asyncio.Event()
    reaper = asyncio.create_task(_reaper_loop(pool, min(1.0, lease_seconds / 2)))
    try:
        while not stop.is_set():
            claim = await claim_task(pool, worker_id, lease_seconds)
            if claim is None:
                await asyncio.sleep(poll_interval)
                continue
            _log(f"CLAIMED worker={worker_id} run={claim.run_id} attempt={claim.attempt}")
            main = asyncio.create_task(execute_task(pool, claim, ctx))
            hb = asyncio.create_task(_heartbeat_loop(pool, claim, lease_seconds, main))
            try:
                await main
                _log(f"DONE worker={worker_id} run={claim.run_id}")
            except LeaseLostError:
                _log(f"LEASE_LOST worker={worker_id} run={claim.run_id} (commit rejected)")
            except asyncio.CancelledError:
                if not hb.done():
                    raise  # the worker itself is being cancelled, not just this task
            except Exception as exc:
                _log(f"ERROR worker={worker_id} run={claim.run_id}: {exc!r}")
                await _handle_error(pool, claim, exc, max_attempts, retry_backoff)
            finally:
                hb.cancel()
    finally:
        reaper.cancel()
