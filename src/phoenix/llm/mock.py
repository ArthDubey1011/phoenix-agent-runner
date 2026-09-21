"""Deterministic provider for tests and offline evals. The reply is a pure function of the
message history (how many assistant turns so far), so it is identical after a crash/resume."""

from __future__ import annotations

from typing import Any

from phoenix.llm.base import LLMResponse, token_cost

MOCK_IN_TOKENS = 100
MOCK_OUT_TOKENS = 50
# $1 / $5 per million tokens -> 0.00035 USD per mock call
MOCK_COST = token_cost(MOCK_IN_TOKENS, MOCK_OUT_TOKENS, "1", "5")


def write(path: str, content: str) -> dict[str, Any]:
    return {"tool": "write_file", "args": {"path": path, "content": content}}


def run(path: str) -> dict[str, Any]:
    return {"tool": "run_code", "args": {"path": path}}


def finish(summary: str = "done") -> dict[str, Any]:
    return {"tool": "finish", "args": {"summary": summary}}


class MockProvider:
    name = "mock"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.calls = 0  # in-process counter, for test assertions only (not durable state)

    async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        self.calls += 1
        turn = sum(1 for m in messages if m["role"] == "assistant")
        action = self.script[min(turn, len(self.script) - 1)]
        return LLMResponse(action, MOCK_IN_TOKENS, MOCK_OUT_TOKENS, MOCK_COST)


SCENARIOS: dict[str, list[dict[str, Any]]] = {
    # fail twice (ZeroDivisionError, NameError), then fix
    "fail_twice_then_fix": [
        write("main.py", "print(1 / 0)"),
        run("main.py"),
        write("main.py", "print(undefined_name)"),
        run("main.py"),
        write("main.py", "print('hello')"),
        run("main.py"),
        finish("fixed after two failures"),
    ],
    # never converges: identical action forever (loop detection must stop it)
    "stuck_loop": [write("main.py", "print(1 / 0)")] + [run("main.py")] * 50,
    # always makes "progress" with distinct actions and never finishes (budget/max_steps stop it)
    "endless_progress": [write(f"f{i}.py", f"print({i})") for i in range(200)],
    "write_outside": [write("../evil.txt", "x"), finish()],
}


def get_scenario(name: str) -> MockProvider:
    return MockProvider(SCENARIOS[name])
