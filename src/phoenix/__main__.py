"""CLI: migrate, submit, status, timeline, api, worker, chaos, eval."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg

from phoenix.chaos.scenarios import ChaosConfig, run_chaos
from phoenix.engine import EngineContext, run_worker, submit_run
from phoenix.envfile import load_dotenv
from phoenix.evals.report import render_table, results_json, summarize
from phoenix.evals.runner import EvalConfigError, dumps, model_name, run_evals
from phoenix.store import apply_migrations, create_pool
from phoenix.timeline import build_timeline

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


async def _migrate() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        applied = await apply_migrations(conn, MIGRATIONS)
    finally:
        await conn.close()
    print(f"applied: {', '.join(applied) if applied else 'nothing (up to date)'}")


async def _submit(args: argparse.Namespace) -> None:
    pool = await create_pool(os.environ["DATABASE_URL"])
    try:
        spec = {"kind": "toy", "steps": args.steps, "step_delay": args.step_delay}
        for _ in range(args.count):
            print(await submit_run(pool, spec))
    finally:
        await pool.close()


async def _status(run_id: uuid.UUID) -> None:
    pool = await create_pool(os.environ["DATABASE_URL"])
    try:
        row = await pool.fetchrow(
            "SELECT r.status, r.spent_usd, "
            "(SELECT count(*) FROM events e WHERE e.run_id=r.id) AS n "
            "FROM runs r WHERE r.id=$1",
            run_id,
        )
    finally:
        await pool.close()
    print(
        "not found"
        if row is None
        else f"{row['status']} events={row['n']} spent={row['spent_usd']}"
    )


async def _timeline(run_id: uuid.UUID) -> None:
    pool = await create_pool(os.environ["DATABASE_URL"])
    try:
        print(await build_timeline(pool, run_id))
    finally:
        await pool.close()


async def _chaos(args: argparse.Namespace) -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await apply_migrations(conn, MIGRATIONS)
    finally:
        await conn.close()
    pool = await create_pool(dsn)
    try:
        cfg = ChaosConfig(
            seed=args.seed,
            runs=args.runs,
            workers=args.workers,
            lease_seconds=args.lease,
            workload=args.workload,
            faults=args.faults,
            timeout_seconds=args.timeout,
            unsafe_no_fencing=args.unsafe_no_fencing,
            database_url=dsn,
        )
        report = await run_chaos(cfg, pool)
    finally:
        await pool.close()
    print(report.render())
    return 0 if report.passed else 1


async def _eval(args: argparse.Namespace) -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await apply_migrations(conn, MIGRATIONS)
    finally:
        await conn.close()
    pool = await create_pool(dsn)
    try:
        results = await run_evals(
            pool,
            dsn,
            real=args.real,
            workers=args.workers,
            per_run_usd=Decimal(args.budget),
            cap_usd=Decimal(args.max_total_usd),
            only=args.only,
            suite=args.suite,
            timeout=args.timeout,
        )
    except EvalConfigError as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    finally:
        await pool.close()
    summary = summarize(results, "real" if args.real else "mock", model_name(args.real))
    print(render_table(results))
    print()
    print(json.dumps(summary, indent=2))
    if summary["unfinished"]:
        print(
            f"\nINCOMPLETE: {summary['unfinished']} of {summary['tasks']} tasks hit a timeout or a "
            "platform/provider error (e.g. a quota), so the pass rate above is NOT a result. "
            "Nothing was written to --out.",
            file=sys.stderr,
        )
        return 3
    if args.out:  # full per-task detail goes to the file
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(dumps(summary, results_json(results)) + "\n", encoding="utf8")
    return 0


async def _worker(args: argparse.Namespace) -> None:
    pool = await create_pool(os.environ["DATABASE_URL"], max_size=5)
    try:
        ctx = EngineContext(
            workspace_root=Path(os.environ.get("PHOENIX_WORKSPACE_ROOT", "workspaces"))
        )
        await run_worker(pool, args.id, lease_seconds=args.lease, poll_interval=args.poll, ctx=ctx)
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # ./.env fills in variables the shell did not set
    parser = argparse.ArgumentParser(prog="phoenix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    p_submit = sub.add_parser("submit", help="submit toy runs")
    p_submit.add_argument("--steps", type=int, default=20)
    p_submit.add_argument("--step-delay", type=float, default=0.0)
    p_submit.add_argument("--count", type=int, default=1)
    p_status = sub.add_parser("status", help="show a run's status")
    p_status.add_argument("run_id", type=uuid.UUID)
    p_tl = sub.add_parser("timeline", help="show a run's event timeline")
    p_tl.add_argument("run_id", type=uuid.UUID)
    p_api = sub.add_parser("api", help="serve the HTTP API")
    p_api.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p_api.add_argument("--port", type=int, default=8000)
    p_worker = sub.add_parser("worker", help="run a worker process")
    p_worker.add_argument("--id", default=f"worker-{os.getpid()}")
    p_worker.add_argument("--lease", type=float, default=5.0)
    p_worker.add_argument("--poll", type=float, default=0.2)
    p_chaos = sub.add_parser("chaos", help="run the seeded chaos harness")
    p_chaos.add_argument("--seed", type=int, required=True)
    p_chaos.add_argument("--runs", type=int, default=30)
    p_chaos.add_argument("--workers", type=int, default=3)
    p_chaos.add_argument("--lease", type=float, default=2.0)
    p_chaos.add_argument("--workload", choices=["toy", "agent", "mixed"], default="mixed")
    p_chaos.add_argument("--faults", type=int, default=None)
    p_chaos.add_argument("--timeout", type=float, default=300.0)
    p_chaos.add_argument(
        "--unsafe-no-fencing",
        action="store_true",
        help="chaos demo only: disable fencing and abort-on-lost-lease",
    )
    p_eval = sub.add_parser("eval", help="run the 15-task eval suite")
    p_eval.add_argument("--real", action="store_true", help="use the real LLM (costs money)")
    p_eval.add_argument("--workers", type=int, default=3)
    p_eval.add_argument("--budget", default="0.05", help="per-run budget in USD")
    p_eval.add_argument("--max-total-usd", default="1.00", help="global cap in USD")
    p_eval.add_argument("--suite", choices=["standard", "challenge"], default="standard")
    p_eval.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="seconds to wait for all runs (paced free tiers need more)",
    )
    p_eval.add_argument("--only", nargs="*", help="task ids, e.g. 01 02 (or C01 C02)")
    p_eval.add_argument("--out", help="also write the JSON here")
    args = parser.parse_args(argv)
    if args.cmd == "migrate":
        asyncio.run(_migrate())
    elif args.cmd == "submit":
        asyncio.run(_submit(args))
    elif args.cmd == "status":
        asyncio.run(_status(args.run_id))
    elif args.cmd == "api":
        import uvicorn

        from phoenix.api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    elif args.cmd == "timeline":
        asyncio.run(_timeline(args.run_id))
    elif args.cmd == "eval":
        return asyncio.run(_eval(args))
    elif args.cmd == "chaos":
        return asyncio.run(_chaos(args))
    elif args.cmd == "worker":
        try:
            asyncio.run(_worker(args))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
