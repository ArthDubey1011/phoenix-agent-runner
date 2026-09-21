"""Fencing is verified two ways: with it on, a stale commit is rejected and every invariant
holds; with it removed, the same seeded zombie scenario makes the invariants FAIL."""

import asyncpg
import pytest

from phoenix.chaos.invariants import check_all
from phoenix.chaos.scenarios import zombie_replay

SEEDS = [1, 2, 3, 4, 5]


@pytest.mark.parametrize("seed", SEEDS)
async def test_with_fencing_zombie_commits_are_rejected_and_invariants_hold(
    pool: asyncpg.Pool, seed: int
) -> None:
    out = await zombie_replay(pool, seed)
    assert out["accepted"] == 0 and out["rejected"] >= 1
    results = await check_all(pool, [out["run_id"]])
    assert all(r.passed for r in results), [r.violations for r in results]


@pytest.mark.parametrize("seed", SEEDS)
async def test_chaos_fails_when_fencing_is_removed(
    pool: asyncpg.Pool, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOENIX_UNSAFE_NO_FENCING", "1")
    out = await zombie_replay(pool, seed)
    assert out["accepted"] >= 1  # the zombie's writes landed
    results = await check_all(pool, [out["run_id"]])
    integrity = results[1]
    assert not integrity.passed and "stale attempt" in integrity.violations[0]
