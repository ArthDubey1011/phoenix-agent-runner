"""Sandbox security suite. Every test here needs a running Docker daemon (skipped otherwise)."""

from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from phoenix.sandbox.policy import SandboxPolicy
from phoenix.sandbox.runner import LABEL, DockerRunner, SandboxResult


def _daemon_up() -> bool:
    try:
        docker.from_env().ping()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _daemon_up(), reason="Docker daemon not available")


@pytest.fixture(scope="module")
def runner() -> DockerRunner:
    r = DockerRunner()
    r.ensure_image()
    return r


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path


def py(runner: DockerRunner, ws: Path, code: str, **policy: object) -> SandboxResult:
    (ws / "main.py").write_text(code)
    return runner.run(ws, ["python", "main.py"], SandboxPolicy(**policy))  # type: ignore[arg-type]


def test_happy_path_returns_all_fields(runner: DockerRunner, ws: Path) -> None:
    r = py(runner, ws, "import sys; print('out'); print('err', file=sys.stderr)")
    assert (r.exit_code, r.stdout.strip(), r.stderr.strip()) == (0, "out", "err")
    assert not r.timed_out and not r.oom_killed and r.duration_ms > 0


def test_nonzero_exit_and_traceback_captured(runner: DockerRunner, ws: Path) -> None:
    r = py(runner, ws, "raise ValueError('boom')")
    assert r.exit_code == 1 and "ValueError: boom" in r.stderr


def test_infinite_loop_killed_at_timeout(runner: DockerRunner, ws: Path) -> None:
    r = py(runner, ws, "while True: pass", timeout_s=2)
    assert r.timed_out and r.exit_code != 0
    assert 1500 <= r.duration_ms < 8000


def test_memory_hog_is_oom_killed(runner: DockerRunner, ws: Path) -> None:
    r = py(
        runner,
        ws,
        "b = []\nwhile True: b.append(bytearray(10 * 1024 * 1024))",
        memory_mb=64,
        timeout_s=20,
    )
    assert r.oom_killed and not r.timed_out and r.exit_code == 137


def test_fork_bomb_is_contained_by_pid_limit(runner: DockerRunner, ws: Path) -> None:
    code = (
        "import os, time\n"
        "n = 0\n"
        "try:\n"
        "    while True:\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(30); os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    print('blocked after', n)\n"
    )
    r = py(runner, ws, code, pids_limit=32, timeout_s=10)
    assert "blocked after" in r.stdout
    assert 0 < int(r.stdout.split()[-1]) <= 32
    assert not r.timed_out
    assert runner.leftover_containers() == []  # children died with the container


def test_network_is_unreachable(runner: DockerRunner, ws: Path) -> None:
    code = (
        "import socket\n"
        "for host in ('1.1.1.1', 'example.com'):\n"
        "    try:\n"
        "        socket.create_connection((host, 80), timeout=3)\n"
        "        print('CONNECTED', host)\n"
        "    except OSError:\n"
        "        print('blocked', host)\n"
    )
    r = py(runner, ws, code)
    assert "CONNECTED" not in r.stdout
    assert r.stdout.count("blocked") == 2


def test_cannot_write_outside_workspace(runner: DockerRunner, ws: Path) -> None:
    code = (
        "for p in ('/etc/pwn', '/usr/pwn', '/pwn', '/root/pwn'):\n"
        "    try:\n"
        "        open(p, 'w').write('x'); print('WROTE', p)\n"
        "    except OSError:\n"
        "        print('denied', p)\n"
    )
    r = py(runner, ws, code)
    assert "WROTE" not in r.stdout and r.stdout.count("denied") == 4


def test_workspace_and_tmp_are_writable_and_workspace_persists(
    runner: DockerRunner, ws: Path
) -> None:
    r = py(runner, ws, "open('out.txt','w').write('hi'); open('/tmp/t','w').write('x')")
    assert r.exit_code == 0
    assert (ws / "out.txt").read_text() == "hi"  # persists on the host across runs


def test_tmp_cannot_execute_binaries(runner: DockerRunner, ws: Path) -> None:
    code = (
        "import shutil, subprocess, os\n"
        "shutil.copy('/bin/true', '/tmp/true'); os.chmod('/tmp/true', 0o755)\n"
        "try:\n"
        "    subprocess.run(['/tmp/true'], check=True); print('EXECUTED')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    r = py(runner, ws, code)
    assert "EXECUTED" not in r.stdout and "blocked" in r.stdout


def test_huge_stdout_is_truncated(runner: DockerRunner, ws: Path) -> None:
    r = py(
        runner,
        ws,
        "import sys\nfor _ in range(50): sys.stdout.write('x' * 100_000)",
        max_output_bytes=10_000,
    )
    assert r.truncated and len(r.stdout) <= 10_000 + 200
    assert r.exit_code == 0


def test_runs_unprivileged_with_no_capabilities(runner: DockerRunner, ws: Path) -> None:
    code = (
        "import os\n"
        "print(os.getuid())\n"
        "print([l for l in open('/proc/self/status') if l.startswith('CapEff')][0].split()[1])\n"
    )
    r = py(runner, ws, code)
    uid, cap_eff = r.stdout.split()
    assert int(uid) != 0 and int(cap_eff, 16) == 0


def test_containers_are_removed_after_every_run(runner: DockerRunner, ws: Path) -> None:
    py(runner, ws, "print(1)")
    py(runner, ws, "while True: pass", timeout_s=1)
    assert runner.leftover_containers() == []


def test_label_marks_sandbox_containers_for_chaos(runner: DockerRunner) -> None:
    assert LABEL == "phoenix.sandbox"


@pytest.fixture(autouse=True)
def _no_leaks(runner: DockerRunner) -> Iterator[None]:
    yield
    runner.remove_leftovers()
