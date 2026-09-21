"""Provider for any OpenAI-compatible chat-completions endpoint. Defaults target Google's
Gemini OpenAI-compatibility endpoint (free tier available, needs GEMINI_API_KEY).

Only constructed when PHOENIX_REAL_LLM=1; tests never touch the network (they inject a
fake httpx transport)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from phoenix.llm.anthropic import SYSTEM, TOOL_SPECS
from phoenix.llm.base import LLMResponse, token_cost

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# Model names get retired (gemini-2.5-flash already was, for new users): override with
# PHOENIX_MODEL, and list what your key can use via GET <base>/models.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
MAX_BACKOFF_S = 60.0

TOOLS = [
    {
        "type": "function",
        "function": {k: t[k] for k in ("name", "description")} | {"parameters": t["input_schema"]},
    }
    for t in TOOL_SPECS
]


def retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Server-suggested wait: Retry-After header, else Google's retryDelay in the error body,
    else exponential backoff."""
    header = resp.headers.get("retry-after", "")
    if header.replace(".", "", 1).isdigit():
        return float(header)
    match = re.search(r'"retryDelay":\s*"([0-9.]+)s"', resp.text)
    if match:
        return float(match.group(1)) + 1.0  # small margin so the window has really reset
    return 2.0**attempt


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]
    n = 0
    for m in messages:
        if m["role"] == "assistant":
            action = m["action"]
            if action["tool"] == "invalid":  # the model replied with text instead of a tool call
                out.append({"role": "assistant", "content": action["args"].get("text", "") or "-"})
                continue
            out.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{n}",
                            "type": "function",
                            "function": {
                                "name": action["tool"],
                                "arguments": json.dumps(action["args"]),
                            },
                            # Gemini 3 requires its opaque thought_signature echoed back
                            **({"extra_content": action["_ext"]} if action.get("_ext") else {}),
                        }
                    ],
                }
            )
        elif "observation" in m:
            prev = out[-1]
            if prev["role"] == "assistant" and "tool_calls" in prev:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{n}",
                        "content": json.dumps(m["observation"]),
                    }
                )
            else:  # follows a text-only reply: tell the model what went wrong
                out.append({"role": "user", "content": json.dumps(m["observation"])})
            n += 1
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


def parse_action(message: dict[str, Any]) -> dict[str, Any]:
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        action: dict[str, Any] = {
            "tool": fn.get("name", "invalid"),
            "args": args if isinstance(args, dict) else {},
        }
        if call.get("extra_content"):  # e.g. Gemini's thought_signature; kept, never inspected
            action["_ext"] = call["extra_content"]
        return action
    return {"tool": "invalid", "args": {"text": (message.get("content") or "")[:500]}}


class QuotaExhaustedError(RuntimeError):
    """A per-day quota is used up: retrying cannot help until the quota resets."""

    fatal = True


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str = GEMINI_BASE_URL,
        api_key_env: str = "GEMINI_API_KEY",
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if os.environ.get("PHOENIX_REAL_LLM") != "1":
            raise RuntimeError("Real LLM calls are disabled; set PHOENIX_REAL_LLM=1 to enable")
        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(f"{api_key_env} is not set")
        self.model = model or os.environ.get("PHOENIX_MODEL", DEFAULT_GEMINI_MODEL)
        self.name = f"openai-compat:{self.model}"
        # Free tiers cost nothing; set these to price a paid endpoint (USD per million tokens).
        self.usd_in = os.environ.get("PHOENIX_USD_PER_MTOK_IN", "0")
        self.usd_out = os.environ.get("PHOENIX_USD_PER_MTOK_OUT", "0")
        # e.g. 13 for a 5-requests-per-minute free tier; 0 disables pacing
        self.min_interval = float(os.environ.get("PHOENIX_MIN_REQUEST_INTERVAL_S", "0"))
        self._pace_lock = asyncio.Lock()
        self._next_ok = 0.0
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=90.0,
            transport=transport,
        )

    async def _pace(self) -> None:
        """Keep at least min_interval between requests (free tiers allow only a few per minute)."""
        if self.min_interval <= 0:
            return
        async with self._pace_lock:
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok = time.monotonic() + self.min_interval

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(MAX_ATTEMPTS):
            await self._pace()
            resp = await self.client.post("/chat/completions", json=body)
            if resp.status_code == 429 and "PerDay" in resp.text:
                # The body's "retry in 15s" hint is misleading for a daily quota.
                raise QuotaExhaustedError(
                    f"daily quota exhausted for {self.model}: {resp.text[:300]}"
                )
            if resp.status_code not in RETRY_STATUS or attempt == MAX_ATTEMPTS - 1:
                if resp.is_error:  # keep the provider's explanation, a bare status hides it
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} from LLM endpoint: {resp.text[:400]}",
                        request=resp.request,
                        response=resp,
                    )
                return dict(resp.json())
            await asyncio.sleep(min(retry_delay(resp, attempt), MAX_BACKOFF_S))
        raise RuntimeError("unreachable")

    async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        data = await self._post(
            {
                "model": self.model,
                "messages": to_openai_messages(messages),
                "tools": TOOLS,
                "tool_choice": os.environ.get("PHOENIX_TOOL_CHOICE", "auto"),
                "max_tokens": 2048,
            }
        )
        action = parse_action(data["choices"][0]["message"])
        usage = data.get("usage") or {}
        tin = int(usage.get("prompt_tokens", 0))
        # completion_tokens can exclude the model's hidden "thinking" tokens, which are still
        # billed and still cost time; total - prompt captures them.
        tout = int(usage.get("total_tokens") or 0) - tin
        if tout <= 0:
            tout = int(usage.get("completion_tokens", 0))
        cost = token_cost(tin, tout, self.usd_in, self.usd_out)  # 0 unless priced via env
        return LLMResponse(action, tin, tout, cost)
