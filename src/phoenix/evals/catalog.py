"""Eval task loading and the mock scripts that stand in for a model.

Each task directory holds task.json, solution.py (reference), test.py (HIDDEN test: the agent
never sees it; it is copied in only when grading) and, for negative controls,
wrong_solution.py. The mock script for a task fails `fail_first` times (a broken draft, then
run it, see the error), then writes the final code: the reference, or for negative controls a
plausible-but-wrong version that passes its own smoke check and fails the hidden test."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phoenix.llm.mock import finish, run, write

TASKS_DIR = Path(__file__).resolve().parent / "tasks"
CHALLENGE_DIR = Path(__file__).resolve().parent / "challenge"
# standard: 15 classic problems (a strong model may saturate it). challenge: 10 novel, precisely
# specified problems whose hidden tests are checked against deliberate bugs (mutants).
SUITES = {"standard": TASKS_DIR, "challenge": CHALLENGE_DIR}
CHECK = (
    "import solution\nprint('imported ok:', [n for n in dir(solution) if not n.startswith('_')])\n"
)


@dataclass(frozen=True)
class EvalTask:
    id: str
    name: str
    difficulty: str
    prompt: str
    dir: Path
    fail_first: int
    outcome: str  # "pass": mock writes the reference; "wrong": a plausible wrong solution

    def hidden_test(self) -> str:
        return (self.dir / "test.py").read_text(encoding="utf8")

    def reference(self) -> str:
        return (self.dir / "solution.py").read_text(encoding="utf8")

    def final_solution(self) -> str:
        name = "wrong_solution.py" if self.outcome == "wrong" else "solution.py"
        return (self.dir / name).read_text(encoding="utf8")

    def mock_script(self) -> list[dict[str, Any]]:
        ref = self.reference()
        drafts = [
            "import missing_module_xyz\n" + ref,  # ModuleNotFoundError
            ref + "\nassert False, 'self-check failed'\n",  # AssertionError
        ]
        script: list[dict[str, Any]] = [write("check.py", CHECK)]
        for draft in drafts[: self.fail_first]:
            script += [write("solution.py", draft), run("check.py")]
        script += [write("solution.py", self.final_solution()), run("check.py"), finish("done")]
        return script


def load_tasks(suite: str = "standard") -> list[EvalTask]:
    tasks = []
    for d in sorted(SUITES[suite].iterdir()):
        if not (d / "task.json").exists():
            continue
        meta = json.loads((d / "task.json").read_text(encoding="utf8"))
        tasks.append(
            EvalTask(
                meta["id"],
                meta["name"],
                meta["difficulty"],
                meta["prompt"],
                d,
                int(meta["mock"]["fail_first"]),
                meta["mock"]["outcome"],
            )
        )
    return tasks
