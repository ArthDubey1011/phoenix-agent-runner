"""A hidden test may only import names from `solution` that the task prompt asks for; otherwise a
correct solution can fail for reasons the model could not know (this happened once, with C09)."""

import ast

import pytest

from phoenix.evals.catalog import EvalTask, load_tasks

ALL = load_tasks("standard") + load_tasks("challenge")


@pytest.mark.parametrize("task", ALL, ids=lambda t: t.id)
def test_hidden_test_imports_only_names_named_in_the_prompt(task: EvalTask) -> None:
    tree = ast.parse(task.hidden_test())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "solution":
            for alias in node.names:
                assert alias.name in task.prompt, (
                    f"{task.id} test imports {alias.name!r}, which the prompt never mentions"
                )
