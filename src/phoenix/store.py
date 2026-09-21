"""Append-only event store. Run state is a replay of its events."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg


class RunNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class AppendResult:
    seq: int
    inserted: bool  # False => idem_key already committed; nothing was written


@dataclass(frozen=True)
class Event:
    run_id: uuid.UUID
    seq: int
    type: str
    payload: dict[str, Any]
    idem_key: str


def step_key(run_id: uuid.UUID, step_index: int) -> str:
    """Idempotency key: a step commits at most once per run."""
    return f"{run_id}:{step_index}"


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    # No jsonb codec: JSON is encoded/decoded explicitly (json.dumps + ::jsonb, ::text) so a
    # connection's behaviour never depends on hidden per-connection state.
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)


async def apply_migrations(conn: asyncpg.Connection, directory: Path) -> list[str]:
    """Apply *.sql files in name order, once each, tracked in schema_migrations."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        async with conn.transaction():
            # Advisory lock so two processes migrating at once do not race.
            await conn.execute("SELECT pg_advisory_xact_lock(727001)")
            done = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name=$1", path.name)
            if done:
                continue
            await conn.execute(path.read_text(encoding="utf8"))
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", path.name)
            applied.append(path.name)
    return applied


async def insert_run(
    conn: asyncpg.Connection, task_spec: dict[str, Any], budget_usd: str | Decimal
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO runs(id, task_spec, status, budget_usd) VALUES ($1, $2::jsonb, 'pending', $3)",
        run_id,
        json.dumps(task_spec),
        Decimal(budget_usd),
    )
    return run_id


async def create_run(
    pool: asyncpg.Pool, task_spec: dict[str, Any], budget_usd: str | Decimal
) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await insert_run(conn, task_spec, budget_usd)


async def append_event(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    idem_key: str,
    type: str,  # noqa: A002
    payload: dict[str, Any],
) -> AppendResult:
    """Idempotently append an event with the next per-run seq.

    Must run inside the caller's transaction when it is part of a step commit; if the
    connection is not in one, a transaction is opened here. Writers to the same run are
    serialized by a row lock on `runs`, which makes seq gapless and the idem_key check
    race-free. The UNIQUE(run_id, idem_key) constraint remains as the backstop.
    """
    if conn.is_in_transaction():
        return await _append(conn, run_id, idem_key, type, payload)
    async with conn.transaction():
        return await _append(conn, run_id, idem_key, type, payload)


async def _append(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    idem_key: str,
    type: str,  # noqa: A002
    payload: dict[str, Any],
) -> AppendResult:
    locked = await conn.fetchval("SELECT 1 FROM runs WHERE id=$1 FOR UPDATE", run_id)
    if locked is None:
        raise RunNotFoundError(str(run_id))
    existing = await conn.fetchval(
        "SELECT seq FROM events WHERE run_id=$1 AND idem_key=$2", run_id, idem_key
    )
    if existing is not None:
        return AppendResult(seq=existing, inserted=False)
    seq = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=$1", run_id
    )
    await conn.execute(
        "INSERT INTO events(run_id, seq, type, payload, idem_key) VALUES ($1,$2,$3,$4::jsonb,$5)",
        run_id,
        seq,
        type,
        json.dumps(payload),
        idem_key,
    )
    return AppendResult(seq=seq, inserted=True)


async def read_events(pool: asyncpg.Pool, run_id: uuid.UUID) -> list[Event]:
    rows = await pool.fetch(
        "SELECT run_id, seq, type, payload::text AS payload, idem_key FROM events "
        "WHERE run_id=$1 ORDER BY seq",
        run_id,
    )
    return [
        Event(r["run_id"], r["seq"], r["type"], json.loads(r["payload"]), r["idem_key"])
        for r in rows
    ]
