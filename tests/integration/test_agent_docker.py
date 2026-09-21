"""End to end with the real Docker sandbox (still MockProvider: no LLM calls)."""

from pathlib import Path

import asyncpg
import docker
import pytest

from phoenix.engine import EngineContext, execute_task, submit_run
from phoenix.leases import claim_task
from phoenix.sandbox.runner import DockerRunner
from phoenix.store import read_events


def _daemon_up() -> bool:
    try:
        docker.from_env().ping()
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _daemon_up(), reason="Docker daemon not available")
async def test_agent_runs_code_in_real_sandbox(pool: asyncpg.Pool, tmp_path: Path) -> None:
    runner = DockerRunner()
    runner.ensure_image()
    ctx = EngineContext(sandbox=runner, workspace_root=tmp_path / "ws")
    spec = {"kind": "agent", "prompt": "hello", "mock_scenario": "fail_twice_then_fix"}
    run_id = await submit_run(pool, spec)
    claim = await claim_task(pool, "w1", 120)
    assert claim is not None
    await execute_task(pool, claim, ctx)
    events = await read_events(pool, run_id)
    assert events[-1].type == "finish"
    assert events[5].payload["observation"] == {
        "exit_code": 0,
        "stdout": "hello\n",
        "stderr": "",
        "timed_out": False,
        "oom_killed": False,
    }
    assert runner.leftover_containers() == []
