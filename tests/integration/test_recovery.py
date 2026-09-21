"""Real worker processes: kill -9 recovery and the suspended-zombie case.

Uses psutil so it also works on Windows (kill() = hard terminate, suspend()/resume() stand in
for SIGSTOP/SIGCONT)."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path

import asyncpg
import psutil
import pytest

from phoenix.engine import submit_run
from phoenix.store import read_events

ROOT = Path(__file__).resolve().parents[2]
LEASE = 2.0
STEPS = 20


class WorkerProc:
    def __init__(self, name: str, log: Path) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": os.environ["PHOENIX_TEST_DATABASE_URL"],
            "PYTHONPATH": str(ROOT / "src"),
        }
        self.name = name
        self.log = log
        self._fh = log.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "phoenix", "worker", "--id", name, "--lease", str(LEASE)],
            env=env,
            stderr=self._fh,
            stdout=subprocess.DEVNULL,
        )

    def text(self) -> str:
        return self.log.read_text()

    def stop(self) -> None:
        if self.proc.poll() is None:
            psutil.Process(self.proc.pid).resume()  # a stopped process cannot be killed cleanly
            self.proc.kill()
        self.proc.wait()
        self._fh.close()


@pytest.fixture
def workers(tmp_path: Path) -> Iterator[dict[str, WorkerProc]]:
    procs = {n: WorkerProc(n, tmp_path / f"{n}.log") for n in ("A", "B")}
    yield procs
    for p in procs.values():
        p.stop()


async def _wait(pred: Callable[[], Awaitable[bool]], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _start_run(pool: asyncpg.Pool) -> uuid.UUID:
    spec = {"kind": "toy", "steps": STEPS, "step_delay": 0.2}
    return await submit_run(pool, spec)


async def _progress(pool: asyncpg.Pool, run_id: uuid.UUID) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM events WHERE run_id=$1", run_id))


async def _holder(pool: asyncpg.Pool, run_id: uuid.UUID) -> str:
    return str(await pool.fetchval("SELECT lease_owner FROM tasks WHERE run_id=$1", run_id))


async def _finished(pool: asyncpg.Pool, run_id: uuid.UUID) -> bool:
    return await pool.fetchval("SELECT status FROM runs WHERE id=$1", run_id) == "succeeded"


async def test_kill_9_worker_mid_run_other_worker_finishes(
    pool: asyncpg.Pool, workers: dict[str, WorkerProc]
) -> None:
    run_id = await _start_run(pool)
    await _wait(lambda: _ok(_progress(pool, run_id), lambda n: n >= 5), 15, "5 steps")
    victim = await _holder(pool, run_id)
    workers[victim].proc.kill()  # hard kill: no cleanup, lease left dangling
    await _wait(lambda: _finished(pool, run_id), 30, "run to finish")

    events = await read_events(pool, run_id)
    assert [e.seq for e in events] == list(range(1, STEPS + 1))
    assert [e.payload["i"] for e in events] == list(range(STEPS))
    assert len({e.idem_key for e in events}) == STEPS
    assert {e.payload["worker"] for e in events} == {"A", "B"}  # both contributed
    assert await pool.fetchval("SELECT attempt FROM tasks WHERE run_id=$1", run_id) == 2
    assert await pool.fetchval("SELECT spent_usd FROM runs WHERE id=$1", run_id) == Decimal(
        "0.0020"
    )


async def test_suspended_zombie_wakes_up_and_is_fenced(
    pool: asyncpg.Pool, workers: dict[str, WorkerProc]
) -> None:
    run_id = await _start_run(pool)
    await _wait(lambda: _ok(_progress(pool, run_id), lambda n: n >= 3), 15, "3 steps")
    zombie = await _holder(pool, run_id)
    other = "B" if zombie == "A" else "A"
    psutil.Process(workers[zombie].proc.pid).suspend()  # SIGSTOP equivalent
    await _wait(lambda: _finished(pool, run_id), 30, "other worker to finish the run")

    psutil.Process(workers[zombie].proc.pid).resume()  # SIGCONT: zombie wakes with a stale lease
    await _wait(lambda: _ok(_text(workers[zombie]), lambda t: "LEASE_LOST" in t), 15, "fencing")

    events = await read_events(pool, run_id)
    assert [e.seq for e in events] == list(range(1, STEPS + 1))
    assert [e.payload["i"] for e in events] == list(range(STEPS))
    takeover = [e for e in events if e.payload["attempt"] == 2]
    assert takeover and {e.payload["worker"] for e in takeover} == {other}
    assert "DONE" not in workers[zombie].text()  # the zombie never believed it finished


async def _text(w: WorkerProc) -> str:
    return w.text()


async def _ok[T](awaitable: Awaitable[T], pred: Callable[[T], bool]) -> bool:
    return bool(pred(await awaitable))
