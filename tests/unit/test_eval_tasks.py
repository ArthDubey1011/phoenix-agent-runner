"""Eval suite sanity: tasks are well-formed, hidden tests discriminate, guards hold.
No Docker and no database needed."""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from phoenix.evals.catalog import EvalTask, load_tasks
from phoenix.evals.report import TaskResult, summarize
from phoenix.evals.runner import EvalConfigError, check_real_mode, spec_for

TASKS = load_tasks()


def grade_locally(task: EvalTask, solution: str, tmp_path: Path) -> bool:
    (tmp_path / "solution.py").write_text(solution, encoding="utf8")
    (tmp_path / "test.py").write_text(task.hidden_test(), encoding="utf8")
    p = subprocess.run(
        [sys.executable, "test.py"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    return p.returncode == 0


def test_fifteen_tasks_five_per_tier() -> None:
    assert [t.id for t in TASKS] == [f"{i:02d}" for i in range(1, 16)]
    tiers = [t.difficulty for t in TASKS]
    assert [tiers.count(d) for d in ("easy", "medium", "hard")] == [5, 5, 5]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_passes_and_a_stub_fails_the_hidden_test(task: EvalTask, tmp_path: Path) -> None:
    assert grade_locally(task, task.reference(), tmp_path)
    assert not grade_locally(task, "raise NotImplementedError\n", tmp_path)


def test_negative_controls_are_plausible_but_wrong(tmp_path: Path) -> None:
    controls = [t for t in TASKS if t.outcome == "wrong"]
    assert [t.id for t in controls] == ["12", "14"]
    for t in controls:
        assert not grade_locally(t, t.final_solution(), tmp_path)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_mock_script_shape_and_no_hidden_test_leak(task: EvalTask) -> None:
    script = task.mock_script()
    runs = [a for a in script if a["tool"] == "run_code"]
    assert script[-1]["tool"] == "finish" and len(runs) == task.fail_first + 1 <= 3
    assert len(script) <= 20
    blob = str(script) + task.prompt
    assert "PASS" not in blob and "hidden" not in blob.lower()  # agent never sees the tests


def test_real_spec_has_no_mock_script_and_mock_spec_does() -> None:
    assert "mock_script" not in spec_for(TASKS[0], real=True)
    assert "mock_script" in spec_for(TASKS[0], real=False)


def test_real_mode_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOENIX_REAL_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(EvalConfigError, match="exceeds the global cap"):
        check_real_mode(False, Decimal("0.10"), 15, Decimal("1.00"))  # 15 x 0.10 = 1.50
    with pytest.raises(EvalConfigError, match="PHOENIX_REAL_LLM=1"):
        check_real_mode(True, Decimal("0.05"), 15, Decimal("1.00"))
    monkeypatch.setenv("PHOENIX_REAL_LLM", "1")
    with pytest.raises(EvalConfigError, match="ANTHROPIC_API_KEY"):
        check_real_mode(True, Decimal("0.05"), 15, Decimal("1.00"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    check_real_mode(True, Decimal("0.05"), 15, Decimal("1.00"))  # 15 x 0.05 = 0.75: allowed
    check_real_mode(False, Decimal("0.05"), 15, Decimal("1.00"))  # mock never needs a key


def result(id_: str, diff: str, passed: bool, attempts: int, lat: float) -> TaskResult:
    return TaskResult(id_, "t", diff, "succeeded", passed, 5, attempts, 100, Decimal("0.5"), lat)


def test_summary_math() -> None:
    rs = [
        result("1", "easy", True, 1, 1.0),
        result("2", "medium", True, 3, 2.0),
        result("3", "hard", False, 2, 9.0),
        result("4", "hard", True, 2, 4.0),
    ]
    s = summarize(rs, "mock", "m")
    assert (s["tasks"], s["passed"], s["pass_rate"]) == (4, 3, 0.75)
    assert s["avg_attempts"] == 2.0 and s["total_tokens"] == 400
    assert s["total_cost_usd"] == "2.0"
    assert s["latency_s"] == {"mean": 4.0, "p50": 3.0, "max": 9.0}
    assert s["by_difficulty"]["hard"] == {"tasks": 2, "passed": 1}
