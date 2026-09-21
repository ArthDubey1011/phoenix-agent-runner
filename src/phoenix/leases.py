"""Task claiming with leases, heartbeats, a reaper, and fencing via `tasks.attempt`."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import asyncpg


def fencing_enabled() -> bool:
    """Chaos-testing hook ONLY. PHOENIX_UNSAFE_NO_FENCING=1 removes the fencing check and the
    worker's abort-on-lost-lease, so the chaos suite can prove it notices their absence."""
    return os.environ.get("PHOENIX_UNSAFE_NO_FENCING") != "1"


class LeaseLostError(Exception):
    """The caller no longer holds the lease (expired, reaped, or taken by another worker)."""


@dataclass(frozen=True)
class Claim:
    task_id: uuid.UUID
    run_id: uuid.UUID
    worker_id: str
    attempt: int  # fencing token: strictly increases each time the task is (re)claimed


async def enqueue_task(conn: asyncpg.Connection, run_id: uuid.UUID) -> uuid.UUID:
    task_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO tasks(id, run_id, status) VALUES ($1, $2, 'queued')", task_id, run_id
    )
    return task_id


async def claim_task(pool: asyncpg.Pool, worker_id: str, lease_seconds: float) -> Claim | None:
    """Claim one queued task. SKIP LOCKED lets concurrent workers pick different rows
    without blocking on (or double-claiming) each other's candidates."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, run_id FROM tasks WHERE status='queued' AND available_at <= now() "
            "ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1"
        )
        if row is None:
            return None
        attempt = await conn.fetchval(
            "UPDATE tasks SET status='leased', lease_owner=$2, attempt=attempt+1, "
            "lease_expires_at = now() + make_interval(secs => $3) WHERE id=$1 RETURNING attempt",
            row["id"],
            worker_id,
            lease_seconds,
        )
        await conn.execute(
            "UPDATE runs SET status='running' WHERE id=$1 AND status='pending'", row["run_id"]
        )
        return Claim(row["id"], row["run_id"], worker_id, attempt)


async def heartbeat(pool: asyncpg.Pool, claim: Claim, lease_seconds: float) -> bool:
    """Extend the lease. False means it was lost; the holder must stop."""
    updated = await pool.fetchval(
        "UPDATE tasks SET lease_expires_at = now() + make_interval(secs => $4) "
        "WHERE id=$1 AND attempt=$2 AND lease_owner=$3 AND status='leased' "
        "AND lease_expires_at > now() RETURNING 1",
        claim.task_id,
        claim.attempt,
        claim.worker_id,
        lease_seconds,
    )
    return updated is not None


async def reap_expired(pool: asyncpg.Pool) -> int:
    """Requeue tasks whose lease expired. Safe to run from every worker concurrently."""
    rows = await pool.fetch(
        "UPDATE tasks SET status='queued', lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id IN (SELECT id FROM tasks WHERE status='leased' AND lease_expires_at < now() "
        "FOR UPDATE SKIP LOCKED) RETURNING id"
    )
    return len(rows)


async def release_task(pool: asyncpg.Pool, claim: Claim, backoff_seconds: float) -> bool:
    """Give a task back after a worker-side error (fenced: only the current holder can)."""
    row = await pool.fetchval(
        "UPDATE tasks SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
        "available_at = now() + make_interval(secs => $4) "
        "WHERE id=$1 AND attempt=$2 AND lease_owner=$3 AND status='leased' RETURNING 1",
        claim.task_id,
        claim.attempt,
        claim.worker_id,
        backoff_seconds,
    )
    return row is not None


async def assert_lease(conn: asyncpg.Connection, claim: Claim) -> None:
    """Fencing check, called inside the commit transaction. Locks the task row so the
    lease cannot be reaped between this check and the commit. Also rejects an expired
    lease that has not been reaped yet: past expiry the holder may not commit."""
    if not fencing_enabled():
        return
    ok = await conn.fetchval(
        "SELECT 1 FROM tasks WHERE id=$1 AND attempt=$2 AND lease_owner=$3 AND status='leased' "
        "AND lease_expires_at > now() FOR UPDATE",
        claim.task_id,
        claim.attempt,
        claim.worker_id,
    )
    if ok is None:
        raise LeaseLostError(f"task {claim.task_id} attempt {claim.attempt} ({claim.worker_id})")
