"""`python -m phoenix timeline <run_id>`: a run's event log as a readable timeline, with
worker takeovers and repeated LLM calls called out."""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import asyncpg

from phoenix.store import Event, read_events


def _summary(e: Event) -> str:
    p = e.payload
    action = p.get("action")
    if action is None:
        if "reason" in p:
            return p["reason"]
        return f"i={p['i']}" if "i" in p else ""
    tool, args = action.get("tool"), action.get("args", {})
    obs: dict[str, Any] = p.get("observation", {})
    if tool == "write_file":
        return f"write_file {args.get('path')}" + (
            f" ERROR {obs['error']}" if "error" in obs else ""
        )
    if tool == "run_code":
        tail = ""
        if obs.get("exit_code"):
            lines = (obs.get("stderr") or "").strip().splitlines()
            tail = f" | {lines[-1][:50]}" if lines else ""
        return f"run_code {args.get('path')} -> exit {obs.get('exit_code')}{tail}"
    return f"{tool} {args.get('summary', '')}".strip()


async def build_timeline(pool: asyncpg.Pool, run_id: uuid.UUID) -> str:
    run = await pool.fetchrow(
        "SELECT status, budget_usd, spent_usd, created_at, finished_at FROM runs WHERE id=$1",
        run_id,
    )
    if run is None:
        return f"run {run_id} not found"
    task = await pool.fetchrow("SELECT attempt, status FROM tasks WHERE run_id=$1", run_id)
    events = await read_events(pool, run_id)
    times = {
        r["seq"]: r["created_at"]
        for r in await pool.fetch("SELECT seq, created_at FROM events WHERE run_id=$1", run_id)
    }
    calls = Counter(
        r["step_index"]
        for r in await pool.fetch("SELECT step_index FROM provider_calls WHERE run_id=$1", run_id)
    )
    t0 = run["created_at"]
    lines = [
        f"run {run_id}  status={run['status']}  spent=${run['spent_usd']} of ${run['budget_usd']}",
        f"task: status={task['status'] if task else '-'} attempts={task['attempt'] if task else 0}",
        "",
    ]
    prev: tuple[str, int] | None = None
    prev_time = None
    for e in events:
        meta = e.payload.get("meta", {})
        who = (meta.get("worker", "?"), meta.get("attempt", 0))
        at = times[e.seq]
        if prev is not None and who != prev:
            gap = (at - prev_time).total_seconds() if prev_time else 0.0
            lines.append(
                f"  -- takeover: {prev[0]}#{prev[1]} -> {who[0]}#{who[1]} "
                f"({gap:.2f}s since the last committed step) --"
            )
        step = int(e.idem_key.rsplit(":", 1)[1])
        cost = e.payload.get("cost_usd")
        asked = f"  [LLM asked {calls[step]}x]" if calls[step] > 1 else ""
        lines.append(
            f"  +{(at - t0).total_seconds():6.2f}s  seq={e.seq:<3} {e.type:<16} "
            f"{who[0]}#{who[1]:<2} {'$' + cost if cost else '':<10} {_summary(e)}{asked}"
        )
        prev, prev_time = who, at
    if run["finished_at"]:
        lines.append(f"\nfinished after {(run['finished_at'] - t0).total_seconds():.2f}s")
    return "\n".join(lines)
