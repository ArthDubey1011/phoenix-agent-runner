"""End-to-end: real worker processes, real faults. Toy workload so it needs no Docker."""

import os

import asyncpg
import pytest

from phoenix.chaos.scenarios import ChaosConfig, plan, run_chaos


def cfg(seed: int, **kw: object) -> ChaosConfig:
    base = {
        "seed": seed,
        "runs": 8,
        "workers": 3,
        "lease_seconds": 1.5,
        "workload": "toy",
        "faults": 5,
        "timeout_seconds": 120.0,
        "database_url": os.environ["PHOENIX_TEST_DATABASE_URL"],
    }
    return ChaosConfig(**{**base, **kw})  # type: ignore[arg-type]


def test_plan_is_reproducible_from_the_seed() -> None:
    assert plan(cfg(7)) == plan(cfg(7))
    assert plan(cfg(7)) != plan(cfg(8))


async def test_chaos_run_passes_all_invariants(pool: asyncpg.Pool) -> None:
    report = await run_chaos(cfg(1), pool)
    assert report.passed, report.render()
    assert not report.timed_out and report.statuses == {"succeeded": 8}
    assert any("kill -9" in a or "suspend" in a for a in report.applied)


HEAVY = {"runs": 30, "workload": "toy", "faults": 15, "lease_seconds": 2.0, "timeout_seconds": 90.0}


@pytest.mark.slow
async def test_fenced_control_passes_the_same_config(pool: asyncpg.Pool) -> None:
    report = await run_chaos(cfg(1, **HEAVY), pool)
    assert report.passed, report.render()


@pytest.mark.slow
async def test_chaos_fails_when_fencing_is_removed_end_to_end(pool: asyncpg.Pool) -> None:
    """Real processes, real SIGSTOP-style pauses. The fault schedule is seeded but process
    timing is the OS's, so try a few seeds and require that the harness catches at least one
    (in practice it catches all three)."""
    failures = []
    for seed in (1, 2, 3):
        report = await run_chaos(cfg(seed, unsafe_no_fencing=True, **HEAVY), pool)
        if not report.passed:
            failures.append(report)
            break
    assert failures, "chaos did not notice that fencing was removed"
    assert any(not i.passed and i.name == "log integrity" for i in failures[0].invariants)
