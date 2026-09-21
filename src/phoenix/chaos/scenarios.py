"""Chaos scenarios. All randomness comes from one seeded RNG that draws the whole plan
(workload mix and fault schedule) up front, so `--seed N` always injects the same faults at
the same offsets. Process timing is still up to the OS, so a run is *replayable*, not
bit-for-bit identical; the printed schedule is what you re-run."""

from __future__ import annotations

import asyncio
import random
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from phoenix.chaos.injector import Injector, pick
from phoenix.chaos.invariants import TERMINAL, InvariantResult, check_all
from phoenix.engine import StepOutcome, commit_step, submit_run
from phoenix.leases import LeaseLostError, claim_task, reap_expired

FAULT_KILL = "kill_worker"
FAULT_SUSPEND = "suspend_worker"
FAULT_DOCKER = "docker_kill"
DOCKER_WAIT_S = 5.0


@dataclass(frozen=True)
class ChaosConfig:
    seed: int
    runs: int = 30
    workers: int = 3
    lease_seconds: float = 2.0
    workload: str = "mixed"  # toy | agent | mixed
    faults: int | None = None  # default: about one per two runs
    timeout_seconds: float = 300.0
    unsafe_no_fencing: bool = False  # chaos-only: prove the suite notices missing fencing
    database_url: str = ""


@dataclass(frozen=True)
class Fault:
    at: float  # seconds after start
    kind: str
    u: float  # uniform draw that picks the target
    duration: float  # suspend length (seconds); 0 otherwise


@dataclass
class ChaosReport:
    config: ChaosConfig
    duration_s: float
    schedule: list[Fault]
    applied: list[str]
    statuses: dict[str, int]
    invariants: list[InvariantResult]
    timed_out: bool = False
    fired: int = 0  # scheduled faults that actually hit a target
    takeovers: int = 0  # runs whose task needed more than one attempt
    run_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(i.passed for i in self.invariants)

    def render(self) -> str:
        c = self.config
        lines = [
            f"CHAOS REPORT  seed={c.seed} runs={c.runs} workers={c.workers} "
            f"lease={c.lease_seconds}s workload={c.workload} duration={self.duration_s:.1f}s",
        ]
        if c.unsafe_no_fencing:
            lines.append("!! UNSAFE MODE: fencing + abort-on-lost-lease are DISABLED !!")
        lines.append(f"faults: {self.fired} hit a target, {len(self.schedule)} scheduled")
        lines += [f"  {a}" for a in self.applied] or ["  (none)"]
        lines.append(f"runs that needed a takeover (attempt > 1): {self.takeovers}")
        lines.append(
            "run statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(self.statuses.items()))
        )
        if self.timed_out:
            lines.append(f"TIMED OUT after {c.timeout_seconds:.0f}s with runs still in flight")
        lines.append("invariants:")
        for n, inv in enumerate(self.invariants, 1):
            lines.append(f"  {n}. {inv.name:<18} {'PASS' if inv.passed else 'FAIL'}  {inv.summary}")
            lines += [f"       - {v}" for v in inv.violations]
            lines += [f"       ~ retry: {r}" for r in inv.retries[:5]]
        lines.append("RESULT: " + ("PASS" if self.passed else "FAIL"))
        return "\n".join(lines)


def plan(cfg: ChaosConfig) -> tuple[list[dict[str, Any]], list[Fault]]:
    """Draw the whole scenario from the seed: run specs, then the fault schedule."""
    rng = random.Random(cfg.seed)
    specs: list[dict[str, Any]] = []
    for _ in range(cfg.runs):
        agent = cfg.workload == "agent" or (cfg.workload == "mixed" and rng.random() < 0.66)
        if agent:
            specs.append(
                {"kind": "agent", "prompt": "print hello", "mock_scenario": "fail_twice_then_fix"}
            )
        else:
            specs.append(
                {"kind": "toy", "steps": rng.randint(10, 20), "step_delay": rng.uniform(0.05, 0.15)}
            )
    kinds = [FAULT_KILL, FAULT_SUSPEND] + ([FAULT_DOCKER] if cfg.workload != "toy" else [])
    n_faults = cfg.faults if cfg.faults is not None else max(4, cfg.runs // 2)
    faults: list[Fault] = []
    t = 0.0
    for _ in range(n_faults):
        t += rng.uniform(0.5, 2.0)
        kind = rng.choice(kinds)
        duration = rng.uniform(cfg.lease_seconds * 1.3, cfg.lease_seconds * 2.5)
        faults.append(
            Fault(round(t, 2), kind, rng.random(), duration if kind == FAULT_SUSPEND else 0)
        )
    return specs, faults


async def run_chaos(cfg: ChaosConfig, pool: asyncpg.Pool) -> ChaosReport:
    specs, schedule = plan(cfg)
    run_ids = [await submit_run(pool, s, "1.0") for s in specs]
    tmp = Path(tempfile.mkdtemp(prefix="phoenix-chaos-"))
    inj = Injector(
        cfg.database_url, tmp / "logs", cfg.lease_seconds, tmp / "ws", cfg.unsafe_no_fencing
    )
    applied: list[str] = []
    started = time.monotonic()
    timed_out = False
    pending = list(schedule)
    armed: list[tuple[Fault, float]] = []  # docker faults waiting for a container to appear
    fired = 0
    try:
        for _ in range(cfg.workers):
            inj.spawn_worker()
        while True:
            now = time.monotonic() - started
            while pending and pending[0].at <= now:
                f = pending.pop(0)
                if f.kind == FAULT_DOCKER:
                    armed.append((f, now + DOCKER_WAIT_S))  # containers live well under a second
                else:
                    msg = _apply(inj, f, now)
                    applied.append(msg)
                    fired += "no eligible" not in msg
            if armed:  # one armed docker fault per tick keeps the supervisor loop responsive
                f, deadline = armed[0]
                victim = None
                for _ in range(20):  # sandbox containers live well under a second: poll fast
                    victim = inj.docker_kill(f.u)
                    if victim:
                        break
                    await asyncio.sleep(0.05)
                stamp = time.monotonic() - started
                if victim or stamp > deadline:
                    armed.pop(0)
                    applied.append(
                        f"t={stamp:5.1f}s docker kill {victim}"
                        if victim
                        else f"t={stamp:5.1f}s docker kill (no container appeared)"
                    )
                    fired += victim is not None
            for w in list(inj.workers):
                if w.resume_at is not None and time.monotonic() >= w.resume_at:
                    inj.resume(w)
                    applied.append(f"t={now:5.1f}s resume {w.name}")
            inj.workers = [w for w in inj.workers if w.alive]
            while len(inj.workers) < cfg.workers:  # supervisor: replace dead workers
                inj.spawn_worker()
            rows = await pool.fetch("SELECT status FROM runs WHERE id = ANY($1)", run_ids)
            if all(r["status"] in TERMINAL for r in rows):
                break
            if now > cfg.timeout_seconds:
                timed_out = True
                break
            await asyncio.sleep(0.2)
    finally:
        inj.shutdown()
    duration = time.monotonic() - started
    rows = await pool.fetch(
        "SELECT status, count(*) AS n FROM runs WHERE id = ANY($1) GROUP BY status", run_ids
    )
    invariants = await check_all(pool, run_ids)
    takeovers = await pool.fetchval(
        "SELECT count(*) FROM tasks WHERE run_id = ANY($1) AND attempt > 1", run_ids
    )
    return ChaosReport(
        cfg,
        duration,
        schedule,
        applied,
        {r["status"]: r["n"] for r in rows},
        invariants,
        timed_out,
        fired,
        int(takeovers),
        run_ids,
    )


def _apply(inj: Injector, f: Fault, now: float) -> str:
    stamp = f"t={now:5.1f}s"
    target = pick(f.u, inj.running())
    if target is None:
        return f"{stamp} {f.kind} (no eligible worker)"
    if f.kind == FAULT_KILL:
        inj.kill(target)
        return f"{stamp} kill -9 {target.name}"
    inj.suspend(target, time.monotonic() + f.duration)
    return f"{stamp} suspend {target.name} for {f.duration:.1f}s"


async def zombie_replay(pool: asyncpg.Pool, seed: int, steps: int = 12) -> dict[str, Any]:
    """Deterministic in-process zombie: A works, loses its lease, B takes over and works,
    then A wakes up and tries to commit. Returns the run id and how many zombie commits were
    rejected. With fencing on, all are; with it off, they land and invariant #2 catches them."""
    rng = random.Random(seed)
    run_id = await submit_run(pool, {"kind": "toy", "steps": steps}, "1.0")
    cost = Decimal("0.0001")

    def outcome(i: int) -> StepOutcome:
        return StepOutcome("toy_step", {"i": i, "cost_usd": str(cost)}, cost, final=i == steps - 1)

    a = await claim_task(pool, "A", 60)
    assert a is not None
    idx = 0
    for _ in range(rng.randint(1, 4)):
        await commit_step(pool, a, idx, outcome(idx))
        idx += 1
    await pool.execute("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")
    await reap_expired(pool)
    b = await claim_task(pool, "B", 60)
    assert b is not None
    for _ in range(rng.randint(1, 4)):
        await commit_step(pool, b, idx, outcome(idx))
        idx += 1
    rejected = accepted = 0
    for _ in range(rng.randint(1, 3)):  # A wakes up and carries on as if nothing happened
        try:
            await commit_step(pool, a, idx, outcome(idx))
            accepted += 1
            idx += 1
        except LeaseLostError:
            rejected += 1
    while idx < steps:  # B finishes the run
        await commit_step(pool, b, idx, outcome(idx))
        idx += 1
    return {"run_id": run_id, "rejected": rejected, "accepted": accepted}
