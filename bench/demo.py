"""Demo steps 1-3: start 2 workers, submit 10 agent runs, `kill -9` one worker, show a takeover
in the timeline, and show that no LLM call was repeated. (Steps 4-5 are `make chaos` and
`make chaos SEED=1 RUNS=30 UNSAFE=1`, see README.)

    python bench/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import asyncpg

from common import database_url
from phoenix.chaos.injector import Injector
from phoenix.chaos.invariants import TERMINAL
from phoenix.engine import submit_run
from phoenix.store import apply_migrations, create_pool
from phoenix.timeline import build_timeline

SPEC = {"kind": "agent", "prompt": "print hello", "mock_scenario": "fail_twice_then_fix"}


async def main() -> None:
    dsn = database_url()
    conn = await asyncpg.connect(dsn)
    await apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
    await conn.close()
    pool = await create_pool(dsn)
    tmp = Path(tempfile.mkdtemp(prefix="phoenix-demo-"))
    inj = Injector(dsn, tmp / "logs", 2.0, tmp / "ws")
    try:
        print("1. starting 2 workers, submitting 10 runs")
        inj.spawn_worker()
        inj.spawn_worker()
        run_ids = [await submit_run(pool, SPEC) for _ in range(10)]
        while True:  # wait until a worker is mid-run, with 2+ steps committed
            held = await pool.fetch(
                "SELECT lease_owner FROM tasks t WHERE status='leased' AND run_id = ANY($1) "
                "AND (SELECT count(*) FROM events e WHERE e.run_id = t.run_id) >= 2",
                run_ids,
            )
            if held:
                break
            await asyncio.sleep(0.05)
        victim = next(w for w in inj.workers if w.name == held[0]["lease_owner"])
        print(f"2. kill -9 {victim.name} while it holds a lease")
        inj.kill(victim)
        inj.spawn_worker()  # a replacement, so two workers keep going
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            rows = await pool.fetch("SELECT status FROM runs WHERE id = ANY($1)", run_ids)
            if all(r["status"] in TERMINAL for r in rows):
                break
            await asyncio.sleep(0.3)
        statuses = [r["status"] for r in rows]
        print(f"   runs: {statuses.count('succeeded')}/10 succeeded")
        taken = await pool.fetch(
            "SELECT run_id FROM tasks WHERE run_id = ANY($1) AND attempt > 1", run_ids
        )
        print(f"   runs that needed a takeover: {len(taken)}")
        if taken:
            print("\n" + await build_timeline(pool, taken[0]["run_id"]) + "\n")
        calls = await pool.fetchval(
            "SELECT count(*) FROM provider_calls WHERE run_id = ANY($1)", run_ids
        )
        distinct = await pool.fetchval(
            "SELECT count(*) FROM (SELECT DISTINCT run_id, step_index FROM provider_calls "
            "WHERE run_id = ANY($1)) s",
            run_ids,
        )
        print(
            f"3. LLM calls: {calls}, distinct (run, step): {distinct}, repeated: {calls - distinct}"
        )
    finally:
        inj.shutdown()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
