"""The eval runner end to end: real worker processes, real Docker sandbox, hidden tests.
Subset of tasks to keep it quick. Mock provider only."""

import os

import asyncpg
import docker
import pytest

from phoenix.evals.runner import run_evals


def _daemon_up() -> bool:
    try:
        docker.from_env().ping()
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _daemon_up(), reason="Docker daemon not available")
async def test_runner_grades_with_hidden_tests(pool: asyncpg.Pool) -> None:
    # 01: easy, first try. 06: medium, one failed run then fixed. 12: negative control
    results = await run_evals(
        pool, os.environ["PHOENIX_TEST_DATABASE_URL"], workers=2, only=["01", "06", "12"]
    )
    by_id = {r.id: r for r in results}
    assert [by_id[i].passed for i in ("01", "06", "12")] == [True, True, False]
    assert all(r.run_status == "succeeded" for r in results)  # the RUN succeeded even for 12:
    assert "hidden test failed" in by_id["12"].note  # ...the hidden test is what caught it
    assert (by_id["01"].attempts, by_id["06"].attempts, by_id["12"].attempts) == (1, 2, 2)
    assert by_id["06"].tokens == 900 and by_id["06"].cost_usd == 6 * by_id["01"].cost_usd / 4
