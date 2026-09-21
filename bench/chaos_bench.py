"""Run the chaos harness over several seeds and aggregate. Source of the README's chaos
numbers: runs, faults that hit a target, takeovers, duplicate LLM calls, invariant results.

    python bench/chaos_bench.py [--seeds 1 2 3 4 5] [--runs 30]
"""

from __future__ import annotations

import argparse
import asyncio

from common import database_url, save
from phoenix.chaos.scenarios import ChaosConfig, run_chaos
from phoenix.store import apply_migrations, create_pool


async def main(seeds: list[int], runs: int) -> None:
    from pathlib import Path

    import asyncpg

    dsn = database_url()
    conn = await asyncpg.connect(dsn)
    await apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
    await conn.close()
    pool = await create_pool(dsn)
    per_seed = []
    try:
        for seed in seeds:
            report = await run_chaos(
                ChaosConfig(seed=seed, runs=runs, database_url=dsn, workload="mixed"), pool
            )
            calls = await pool.fetchval(
                "SELECT count(*) FROM provider_calls WHERE run_id = ANY($1)", report.run_ids
            )
            distinct = await pool.fetchval(
                "SELECT count(*) FROM (SELECT DISTINCT run_id, step_index FROM provider_calls "
                "WHERE run_id = ANY($1)) s",
                report.run_ids,
            )
            applied = report.applied
            per_seed.append(
                {
                    "seed": seed,
                    "passed": report.passed,
                    "duration_s": round(report.duration_s, 1),
                    "statuses": report.statuses,
                    "faults_scheduled": len(report.schedule),
                    "faults_hit": report.fired,
                    "kill_9": sum("kill -9" in a for a in applied),
                    "suspend": sum(" suspend " in a for a in applied),
                    "docker_kill": sum("docker kill phoenix-sbx" in a for a in applied),
                    "takeovers": report.takeovers,
                    "provider_calls": int(calls),
                    "duplicate_llm_calls": int(calls) - int(distinct),
                    "invariants": {i.name: i.passed for i in report.invariants},
                }
            )
            print(f"seed {seed}: {'PASS' if report.passed else 'FAIL'}", flush=True)
    finally:
        await pool.close()
    total = lambda k: sum(s[k] for s in per_seed)  # noqa: E731
    save(
        "chaos",
        {
            "config": {"seeds": seeds, "runs_per_seed": runs, "workload": "mixed", "workers": 3},
            "seeds_passed": sum(s["passed"] for s in per_seed),
            "total_runs": runs * len(seeds),
            "faults_hit": total("faults_hit"),
            "kill_9": total("kill_9"),
            "suspend": total("suspend"),
            "docker_kill": total("docker_kill"),
            "takeovers": total("takeovers"),
            "provider_calls": total("provider_calls"),
            "duplicate_llm_calls": total("duplicate_llm_calls"),
            "per_seed": per_seed,
        },
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--runs", type=int, default=30)
    a = ap.parse_args()
    asyncio.run(main(a.seeds, a.runs))
