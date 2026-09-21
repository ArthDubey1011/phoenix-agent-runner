"""Docker can report OOMKilled=False right after a container the kernel OOM-killed exits (the
exit event beats the OOM event). The runner must re-check; tested with a fake Docker client so
the race is deterministic and needs no Docker daemon."""

from pathlib import Path
from typing import Any

import pytest

from phoenix.sandbox import runner as runner_mod
from phoenix.sandbox.runner import DockerRunner, SandboxResult


class FakeContainer:
    def __init__(self, exit_code: int, oom_visible_after_reloads: int | None) -> None:
        self.exit_code = exit_code
        self.oom_after = oom_visible_after_reloads
        self.reloads = 0
        self.attrs: dict[str, Any] = {"State": {"OOMKilled": False}}

    def start(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> dict[str, int]:
        return {"StatusCode": self.exit_code}

    def reload(self) -> None:
        self.reloads += 1
        if self.oom_after is not None and self.reloads >= self.oom_after:
            self.attrs["State"]["OOMKilled"] = True

    def logs(self, **kwargs: bool) -> list[bytes]:
        return []

    def kill(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        pass


class FakeClient:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.containers = self

    def create(self, *args: object, **kwargs: object) -> FakeContainer:
        return self.container


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)


def run(container: FakeContainer, tmp_path: Path) -> SandboxResult:
    runner = DockerRunner(FakeClient(container))  # type: ignore[arg-type]
    return runner.run(tmp_path, ["python", "x.py"])


def test_flag_that_appears_late_is_still_reported(tmp_path: Path) -> None:
    c = FakeContainer(exit_code=137, oom_visible_after_reloads=4)  # False on reads 1-3
    res = run(c, tmp_path)
    assert res.oom_killed and res.exit_code == 137 and c.reloads == 4


def test_flag_visible_immediately_needs_no_recheck(tmp_path: Path) -> None:
    c = FakeContainer(exit_code=137, oom_visible_after_reloads=1)
    assert run(c, tmp_path).oom_killed and c.reloads == 1


def test_sigkill_that_was_not_oom_stays_false_after_a_bounded_wait(tmp_path: Path) -> None:
    c = FakeContainer(exit_code=137, oom_visible_after_reloads=None)  # e.g. an external docker kill
    res = run(c, tmp_path)
    assert not res.oom_killed and c.reloads == 1 + runner_mod.OOM_RECHECKS


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_other_exit_codes_never_wait(tmp_path: Path, exit_code: int) -> None:
    c = FakeContainer(exit_code=exit_code, oom_visible_after_reloads=None)
    res = run(c, tmp_path)
    assert not res.oom_killed and c.reloads == 1
