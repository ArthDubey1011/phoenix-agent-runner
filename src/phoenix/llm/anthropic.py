"""Real provider. Only constructed when PHOENIX_REAL_LLM=1; tests never touch it."""

from __future__ import annotations

import json
import os
from typing import Any

from phoenix.llm.base import LLMResponse, token_cost

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# USD per million tokens for the default model (Haiku 4.5). Update if you change the model.
USD_PER_MTOK_IN = "1"
USD_PER_MTOK_OUT = "5"

SYSTEM = (
    "You are a coding agent. Solve the task by calling exactly one tool per turn: write_file "
    "to create or overwrite files in the workspace, run_code to run a Python file in a network-"
    "less sandbox and see its output, finish when the task is done and verified."
)

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_code",
        "description": "Run a Python file from the workspace in the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the task complete.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = 0
    for m in messages:
        if m["role"] == "assistant" and m["action"]["tool"] == "invalid":
            out.append({"role": "assistant", "content": m["action"]["args"].get("text") or "-"})
        elif m["role"] == "assistant":
            out.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{n}",
                            "name": m["action"]["tool"],
                            "input": m["action"]["args"],
                        }
                    ],
                }
            )
        elif (
            "observation" in m
            and out[-1]["role"] == "assistant"
            and isinstance(out[-1]["content"], str)
        ):  # follows a text-only reply
            out.append({"role": "user", "content": json.dumps(m["observation"])})
            n += 1
        elif "observation" in m:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"toolu_{n}",
                            "content": json.dumps(m["observation"]),
                        }
                    ],
                }
            )
            n += 1
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


class AnthropicProvider:
    def __init__(self, model: str | None = None) -> None:
        if os.environ.get("PHOENIX_REAL_LLM") != "1":
            raise RuntimeError("Real LLM calls are disabled; set PHOENIX_REAL_LLM=1 to enable")
        import anthropic  # imported lazily so the dependency is optional

        self.model = model or os.environ.get("PHOENIX_MODEL", DEFAULT_MODEL)
        self.name = f"anthropic:{self.model}"
        self.client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env

    async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOL_SPECS,  # type: ignore[arg-type]
            tool_choice={"type": "any"},
            messages=to_anthropic_messages(messages),  # type: ignore[arg-type]
        )
        action: dict[str, Any] = {"tool": "invalid", "args": {}}
        for block in resp.content:
            if block.type == "tool_use":
                action = {"tool": block.name, "args": dict(block.input)}  # type: ignore[arg-type]
                break
        cost = token_cost(
            resp.usage.input_tokens, resp.usage.output_tokens, USD_PER_MTOK_IN, USD_PER_MTOK_OUT
        )
        return LLMResponse(action, resp.usage.input_tokens, resp.usage.output_tokens, cost)
