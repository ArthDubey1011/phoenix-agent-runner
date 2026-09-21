"""Challenge suite: the hidden tests must accept the reference and reject deliberate bugs
(mutants), so a passing model score means something. No Docker or database needed."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from phoenix.evals.catalog import EvalTask, load_tasks

TASKS = load_tasks("challenge")


def passes(test: str, solution: str, tmp_path: Path) -> bool:
    (tmp_path / "solution.py").write_text(solution, encoding="utf8")
    (tmp_path / "test.py").write_text(test, encoding="utf8")
    p = subprocess.run(
        [sys.executable, "test.py"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    return p.returncode == 0


def test_ten_challenge_tasks_and_standard_suite_untouched() -> None:
    assert [t.id for t in TASKS] == [f"C{i:02d}" for i in range(1, 11)]
    assert {t.difficulty for t in TASKS} == {"challenge"}
    assert len(load_tasks("standard")) == 15


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_passes_stub_fails_and_every_mutant_is_caught(
    task: EvalTask, tmp_path: Path
) -> None:
    assert passes(task.hidden_test(), task.reference(), tmp_path)
    assert not passes(task.hidden_test(), "raise NotImplementedError\n", tmp_path)
    mutants = json.loads((task.dir / "mutants.json").read_text(encoding="utf8"))
    assert len(mutants) >= 2
    for old, new in mutants:
        assert old in task.reference(), f"stale mutant {old!r}"
        mutated = task.reference().replace(old, new, 1)
        assert not passes(task.hidden_test(), mutated, tmp_path), f"mutant survived: {old!r}"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_mock_script_and_no_leak(task: EvalTask) -> None:
    script = task.mock_script()
    assert script[-1]["tool"] == "finish" and len(script) <= 20
    blob = str(script) + task.prompt
    assert "PASS" not in blob and "hidden" not in blob.lower()
