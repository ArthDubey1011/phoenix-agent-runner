"""Submit every eval task through the real platform (Postgres, worker processes, Docker
sandbox), wait for the runs, then grade each result with its hidden test in the sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from phoenix.agent import materialize_workspace
from phoenix.chaos.injector import Injector
from phoenix.chaos.invariants import TERMINAL
from phoenix.engine import submit_run
from phoenix.evals.catalog import EvalTask, load_tasks
from phoenix.evals.report import TaskResult
from phoenix.llm.factory import default_model, provider_kind, required_key_env
from phoenix.sandbox.policy import SandboxPolicy
from phoenix.sandbox.runner import DockerRunner
from phoenix.store import read_events

HIDDEN_TEST_NAME = "_hidden_test.py"  # agent paths cannot start with "_", so it cannot collide
GRADE_POLICY = SandboxPolicy(timeout_s=20.0)


class EvalConfigError(Exception):
    pass


def check_real_mode(real: bool, per_run_usd: Decimal, n_tasks: int, cap_usd: Decimal) -> None:
    """Real-LLM evals cost money: require both the flag and a key, and refuse up front if the
    worst case (every run spending its full budget) would exceed the global cap."""
    if per_run_usd * n_tasks > cap_usd:
        raise EvalConfigError(
            f"worst case {n_tasks} x ${per_run_usd} = ${per_run_usd * n_tasks} exceeds the "
            f"global cap ${cap_usd}; lower --budget or raise --max-total-usd"
        )
    if real:
        if os.environ.get("PHOENIX_REAL_LLM") != "1":
            raise EvalConfigError("--real also requires PHOENIX_REAL_LLM=1 in the environment")
        try:
            key_env = required_key_env()
        except RuntimeError as exc:
            raise EvalConfigError(str(exc)) from exc
        if not os.environ.get(key_env):
            raise EvalConfigError(f"--real requires {key_env} in the environment")


def spec_for(task: EvalTask, real: bool) -> dict[str, Any]:
    spec: dict[str, Any] = {"kind": "agent", "prompt": task.prompt, "max_steps": 20}
    if not real:
        spec["mock_script"] = task.mock_script()
    return spec


async def _wait_terminal(pool: asyncpg.Pool, run_ids: list[uuid.UUID], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await pool.fetch("SELECT status FROM runs WHERE id = ANY($1)", run_ids)
        if all(r["status"] in TERMINAL for r in rows):
            return
        await asyncio.sleep(0.3)


async def _grade(
    pool: asyncpg.Pool, runner: DockerRunner, task: EvalTask, run_id: uuid.UUID, tmp: Path
) -> TaskResult:
    run = await pool.fetchrow(
        "SELECT status, spent_usd, EXTRACT(EPOCH FROM (finished_at - created_at)) AS latency "
        "FROM runs WHERE id=$1",
        run_id,
    )
    events = await read_events(pool, run_id)
    status = run["status"] if run["status"] in TERMINAL else "timeout"
    infra = (
        events
        and events[-1].type == "failed"
        and str(events[-1].payload.get("reason", "")).startswith(("worker error", "fatal error"))
    )
    if infra:  # the platform or provider broke; that says nothing about the model
        status = "error"
    reason = str(events[-1].payload.get("reason", ""))[:90] if infra else ""
    tokens = sum(
        e.payload.get("input_tokens", 0) + e.payload.get("output_tokens", 0) for e in events
    )
    attempts = sum(1 for e in events if e.payload.get("action", {}).get("tool") == "run_code")
    passed, note = False, ""
    if status == "succeeded":
        grade_dir = tmp / f"grade-{task.id}"
        materialize_workspace(grade_dir, events)  # rebuild the agent's files from the log
        (grade_dir / HIDDEN_TEST_NAME).write_text(task.hidden_test(), encoding="utf8")
        res = await asyncio.to_thread(
            runner.run, grade_dir, ["python", HIDDEN_TEST_NAME], GRADE_POLICY
        )
        passed = res.exit_code == 0 and not res.timed_out
        if not passed:
            note = "hidden test failed: " + (res.stderr.strip().splitlines() or ["?"])[-1][:60]
    return TaskResult(
        task.id,
        task.name,
        task.difficulty,
        status,
        passed,
        len(events),
        attempts,
        tokens,
        Decimal(run["spent_usd"]),
        float(run["latency"] or 0.0),
        note or reason,
    )


async def run_evals(
    pool: asyncpg.Pool,
    database_url: str,
    *,
    real: bool = False,
    workers: int = 3,
    per_run_usd: Decimal = Decimal("0.05"),
    cap_usd: Decimal = Decimal("1.00"),
    timeout: float = 900.0,
    only: list[str] | None = None,
    suite: str = "standard",
) -> list[TaskResult]:
    tasks = [t for t in load_tasks(suite) if only is None or t.id in only]
    check_real_mode(real, per_run_usd, len(tasks), cap_usd)
    runner = DockerRunner()
    runner.ensure_image()
    tmp = Path(tempfile.mkdtemp(prefix="phoenix-eval-"))
    inj = Injector(database_url, tmp / "logs", 30.0, tmp / "ws", real_llm=real)
    try:
        for _ in range(workers):
            inj.spawn_worker()
        run_ids = {t.id: await submit_run(pool, spec_for(t, real), per_run_usd) for t in tasks}
        await _wait_terminal(pool, list(run_ids.values()), timeout)
        return [await _grade(pool, runner, t, run_ids[t.id], tmp) for t in tasks]
    finally:
        inj.shutdown()


def model_name(real: bool) -> str:
    if not real:
        return "mock (scripted; validates the pipeline, not a model)"
    return f"{provider_kind()}: {default_model()}"


def dumps(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    return json.dumps({"summary": summary, "tasks": rows}, indent=2)
