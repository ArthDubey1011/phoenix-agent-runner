"""How long does recovery take after `kill -9`? Kill the lease holder mid-run and measure the
gap between the last step it committed and the first step the takeover worker committed.
With a 2 s lease the floor is the lease itself; this shows what is added on top.

    python bench/recovery_bench.py [--trials 8]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import tempfile
import time
from pathlib import Path

import asyncpg

from common import database_url, save
from phoenix.chaos.injector import Injector
from phoenix.chaos.invariants import log_integrity
from phoenix.engine import submit_run
from phoenix.store import apply_migrations, create_pool

LEASE = 2.0
STEPS = 20


async def one_trial(pool: asyncpg.Pool, dsn: str) -> dict[str, float | bool]:
    tmp = Path(tempfile.mkdtemp(prefix="phoenix-recovery-"))
    inj = Injector(dsn, tmp / "logs", LEASE, tmp / "ws")
    try:
        inj.spawn_worker()
        inj.spawn_worker()
        run_id = await submit_run(pool, {"kind": "toy", "steps": STEPS, "step_delay": 0.2})
        while await pool.fetchval("SELECT count(*) FROM events WHERE run_id=$1", run_id) < 5:
            await asyncio.sleep(0.05)
        holder = await pool.fetchval("SELECT lease_owner FROM tasks WHERE run_id=$1", run_id)
        victim = next(w for w in inj.workers if w.name == holder)
        inj.kill(victim)
        deadline = time.monotonic() + 60
        while await pool.fetchval("SELECT status FROM runs WHERE id=$1", run_id) != "succeeded":
            if time.monotonic() > deadline:
                raise RuntimeError("run did not finish within 60s of the kill")
            await asyncio.sleep(0.1)
        gap = await pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM ("
            "min(created_at) FILTER (WHERE (payload#>>'{meta,attempt}')::int = 2) - "
            "max(created_at) FILTER (WHERE (payload#>>'{meta,attempt}')::int = 1))) "
            "FROM events WHERE run_id=$1",
            run_id,
        )
        integrity = await log_integrity(pool, [run_id])
        events = await pool.fetchval("SELECT count(*) FROM events WHERE run_id=$1", run_id)
        return {"gap_s": float(gap), "clean": integrity.passed and events == STEPS}
    finally:
        inj.shutdown()


async def main(trials: int) -> None:
    from common import percentile

    dsn = database_url()
    conn = await asyncpg.connect(dsn)
    await apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
    await conn.close()
    pool = await create_pool(dsn)
    try:
        results = [await one_trial(pool, dsn) for _ in range(trials)]
    finally:
        await pool.close()
    gaps = sorted(float(r["gap_s"]) for r in results)
    save(
        "recovery",
        {
            "config": {"lease_seconds": LEASE, "steps": STEPS, "trials": trials, "workers": 2},
            "all_clean": all(r["clean"] for r in results),
            "recovery_gap_s": {
                "min": round(gaps[0], 2),
                "median": round(statistics.median(gaps), 2),
                "p95": round(percentile(gaps, 0.95), 2),
                "max": round(gaps[-1], 2),
            },
            "trials_detail": [round(g, 2) for g in gaps],
        },
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=8)
    asyncio.run(main(ap.parse_args().trials))
