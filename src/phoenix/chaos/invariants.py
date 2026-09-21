"""The 4 chaos invariants, checked from the database after a scenario."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg

TERMINAL = ("succeeded", "failed", "budget_exceeded")
MAX_LISTED = 5


@dataclass
class InvariantResult:
    name: str
    passed: bool
    summary: str
    violations: list[str] = field(default_factory=list)
    retries: list[str] = field(default_factory=list)  # crash-window retries (invariant 3 only)


def _cap(items: list[str]) -> list[str]:
    extra = len(items) - MAX_LISTED
    return items[:MAX_LISTED] + ([f"... and {extra} more"] if extra > 0 else [])


async def termination(pool: asyncpg.Pool, run_ids: list[uuid.UUID]) -> InvariantResult:
    rows = await pool.fetch("SELECT id, status FROM runs WHERE id = ANY($1)", run_ids)
    bad = [f"run {r['id']} stuck in {r['status']!r}" for r in rows if r["status"] not in TERMINAL]
    done = len(rows) - len(bad)
    return InvariantResult(
        "termination",
        not bad and len(rows) == len(run_ids),
        f"{done}/{len(run_ids)} terminal",
        _cap(bad),
    )


async def log_integrity(pool: asyncpg.Pool, run_ids: list[uuid.UUID]) -> InvariantResult:
    """Per run: seq is 1..n with no gaps; step keys are `run:0..n-1` with no duplicates; no
    event follows the final one; and writer attempts never go backwards (a lower attempt
    writing after a higher one is a zombie whose commit fencing should have rejected)."""
    rows = await pool.fetch(
        "SELECT run_id, seq, idem_key, payload::text AS payload FROM events "
        "WHERE run_id = ANY($1) ORDER BY run_id, seq",
        run_ids,
    )
    by_run: dict[uuid.UUID, list[asyncpg.Record]] = defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)
    bad: list[str] = []
    for run_id, evs in by_run.items():
        seqs = [e["seq"] for e in evs]
        if seqs != list(range(1, len(evs) + 1)):
            bad.append(f"run {run_id}: seq gaps/dups {seqs[:12]}")
        keys = [e["idem_key"] for e in evs]
        if len(set(keys)) != len(keys) or keys != [f"{run_id}:{i}" for i in range(len(evs))]:
            bad.append(f"run {run_id}: idem_key duplicates or gaps")
        last_attempt = 0
        finals = 0
        for e in evs:
            meta = json.loads(e["payload"]).get("meta", {})
            if finals:
                bad.append(f"run {run_id}: event seq={e['seq']} written after the final event")
                break
            attempt = meta.get("attempt", 0)
            if attempt < last_attempt:
                bad.append(
                    f"run {run_id}: seq={e['seq']} written by stale attempt {attempt} "
                    f"({meta.get('worker')}) after attempt {last_attempt}"
                )
                break
            last_attempt = max(last_attempt, attempt)
            finals += bool(meta.get("final"))
    return InvariantResult(
        "log integrity", not bad, f"{len(by_run)} logs checked, {len(bad)} violation(s)", _cap(bad)
    )


async def no_double_billing(pool: asyncpg.Pool, run_ids: list[uuid.UUID]) -> InvariantResult:
    """At most one provider call per (run, step). A repeat made by a LATER attempt is a
    crash-window retry (worker died after asking the model, before the answer was cached):
    allowed, listed explicitly. Two calls by the SAME attempt are real double billing."""
    rows = await pool.fetch(
        "SELECT run_id, step_index, attempt FROM provider_calls WHERE run_id = ANY($1) "
        "ORDER BY run_id, step_index, id",
        run_ids,
    )
    groups: dict[tuple[uuid.UUID, int], list[int]] = defaultdict(list)
    for r in rows:
        groups[(r["run_id"], r["step_index"])].append(r["attempt"])
    bad: list[str] = []
    retries: list[str] = []
    for (run_id, step), attempts in groups.items():
        if len(attempts) == 1:
            continue
        if len(set(attempts)) == len(attempts):
            retries.append(f"run {run_id} step {step}: attempts {attempts}")
        else:
            bad.append(f"run {run_id} step {step}: repeated call within attempts {attempts}")
    return InvariantResult(
        "no double billing",
        not bad,
        f"{len(rows)} provider calls, {len(retries)} crash-window retries",
        _cap(bad),
        retries,
    )


async def spend_consistency(pool: asyncpg.Pool, run_ids: list[uuid.UUID]) -> InvariantResult:
    """runs.spent_usd equals the sum of the costs recorded in the run's events."""
    runs = await pool.fetch("SELECT id, spent_usd FROM runs WHERE id = ANY($1)", run_ids)
    events = await pool.fetch(
        "SELECT run_id, payload::text AS payload FROM events WHERE run_id = ANY($1)", run_ids
    )
    totals: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for e in events:
        totals[e["run_id"]] += Decimal(json.loads(e["payload"]).get("cost_usd", "0"))
    bad = [
        f"run {r['id']}: spent_usd={r['spent_usd']} but events sum to {totals[r['id']]}"
        for r in runs
        if r["spent_usd"] != totals[r["id"]]
    ]
    return InvariantResult(
        "spend consistency",
        not bad,
        f"{len(runs)} runs checked, {len(bad)} mismatch(es)",
        _cap(bad),
    )


async def check_all(pool: asyncpg.Pool, run_ids: list[uuid.UUID]) -> list[InvariantResult]:
    return [
        await termination(pool, run_ids),
        await log_integrity(pool, run_ids),
        await no_double_billing(pool, run_ids),
        await spend_consistency(pool, run_ids),
    ]
