"""Eval results, the printed table, and the summary JSON."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TaskResult:
    id: str
    name: str
    difficulty: str
    run_status: str  # succeeded | failed | budget_exceeded | timeout | error (platform/provider)
    passed: bool  # run succeeded AND the hidden test passed
    steps: int
    attempts: int  # number of run_code executions
    tokens: int  # input + output
    cost_usd: Decimal
    latency_s: float  # submit -> finished, includes time queued behind other runs
    note: str = ""


def summarize(results: list[TaskResult], mode: str, model: str) -> dict[str, Any]:
    n = len(results)
    lat = sorted(r.latency_s for r in results)
    by_diff: dict[str, dict[str, Any]] = {}
    for d in ("easy", "medium", "hard", "challenge"):
        rs = [r for r in results if r.difficulty == d]
        if rs:
            by_diff[d] = {"tasks": len(rs), "passed": sum(r.passed for r in rs)}
    return {
        "mode": mode,  # "mock" numbers validate the pipeline, NOT any model's ability
        "model": model,
        "tasks": n,
        "unfinished": sum(r.run_status in ("timeout", "error") for r in results),
        "passed": sum(r.passed for r in results),
        "pass_rate": round(sum(r.passed for r in results) / n, 4) if n else 0.0,
        "avg_attempts": round(statistics.fmean(r.attempts for r in results), 3) if n else 0.0,
        "total_tokens": sum(r.tokens for r in results),
        "total_cost_usd": str(sum((r.cost_usd for r in results), Decimal(0))),
        "latency_s": {
            "mean": round(statistics.fmean(lat), 2) if n else 0.0,
            "p50": round(statistics.median(lat), 2) if n else 0.0,
            "max": round(lat[-1], 2) if n else 0.0,
        },
        "by_difficulty": by_diff,
    }


def render_table(results: list[TaskResult]) -> str:
    head = (
        f"{'id':<3} {'task':<21} {'diff':<7} {'status':<16} {'pass':<5} {'steps':>5} "
        f"{'runs':>4} {'tokens':>7} {'cost_usd':>9} {'lat_s':>6}"
    )
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r.id:<3} {r.name:<21} {r.difficulty:<7} {r.run_status:<16} "
            f"{'yes' if r.passed else 'NO':<5} {r.steps:>5} {r.attempts:>4} {r.tokens:>7} "
            f"{r.cost_usd:>9.5f} {r.latency_s:>6.1f}" + (f"  {r.note}" if r.note else "")
        )
    return "\n".join(lines)


def results_json(results: list[TaskResult]) -> list[dict[str, Any]]:
    return [{**asdict(r), "cost_usd": str(r.cost_usd)} for r in results]
