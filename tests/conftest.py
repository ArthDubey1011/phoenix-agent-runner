import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from phoenix.engine import EngineContext
from phoenix.sandbox.policy import SandboxPolicy
from phoenix.sandbox.runner import SandboxResult

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """Fresh schema per test so tests cannot leak state into each other."""
    from phoenix.store import apply_migrations

    url = os.environ["PHOENIX_TEST_DATABASE_URL"]
    p = await asyncpg.create_pool(url, min_size=1, max_size=30)
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await apply_migrations(conn, MIGRATIONS)
    yield p
    await p.close()


class LocalSandbox:
    """Test double: runs the script with the host Python. Only ever fed scripted mock
    actions from this repo, never model output. Not shipped in src/ on purpose."""

    def run(
        self, workspace: Path, cmd: list[str], policy: SandboxPolicy | None = None
    ) -> SandboxResult:
        started = time.monotonic()
        p = subprocess.run(
            [sys.executable, *cmd[1:]], cwd=workspace, capture_output=True, text=True, timeout=20
        )
        ms = int((time.monotonic() - started) * 1000)
        return SandboxResult(p.returncode, p.stdout, p.stderr, False, False, ms)


@pytest.fixture
def ctx(tmp_path: Path) -> EngineContext:
    return EngineContext(sandbox=LocalSandbox(), workspace_root=tmp_path / "ws")
