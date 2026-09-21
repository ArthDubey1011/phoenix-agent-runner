"""Gemini/OpenAI-compatible provider, tested against a fake HTTP transport (no network)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from phoenix.llm import openai_compat
from phoenix.llm.factory import provider_kind, required_key_env
from phoenix.llm.openai_compat import OpenAICompatProvider, parse_action, to_openai_messages


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOENIX_REAL_LLM", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("PHOENIX_USD_PER_MTOK_IN", raising=False)
    monkeypatch.delenv("PHOENIX_USD_PER_MTOK_OUT", raising=False)


def tool_reply(name: str, args: object, prompt: int = 10, completion: int = 5) -> dict[str, Any]:
    call = {"id": "x", "type": "function", "function": {"name": name, "arguments": args}}
    return {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def provider(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatProvider:
    return OpenAICompatProvider(transport=httpx.MockTransport(handler))


async def test_parses_tool_call_and_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=tool_reply("run_code", json.dumps({"path": "a.py"})))

    resp = await provider(handler).complete([{"role": "user", "content": "task"}])
    assert resp.action == {"tool": "run_code", "args": {"path": "a.py"}}
    assert (resp.input_tokens, resp.output_tokens, resp.cost_usd) == (10, 5, 0)  # free tier
    assert "total_tokens" not in tool_reply("finish", {})["usage"]
    req = seen[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer test-key"
    body = json.loads(req.content)
    assert {t["function"]["name"] for t in body["tools"]} == {"write_file", "run_code", "finish"}
    assert body["messages"][0]["role"] == "system" and body["tool_choice"] == "auto"


async def test_text_only_reply_becomes_invalid_action() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        msg = {"choices": [{"message": {"role": "assistant", "content": "I think..."}}]}
        return httpx.Response(200, json=msg)

    resp = await provider(handler).complete([{"role": "user", "content": "t"}])
    assert resp.action == {"tool": "invalid", "args": {"text": "I think..."}}
    assert resp.input_tokens == 0  # missing usage does not crash


async def test_retries_429_then_succeeds_honoring_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(openai_compat.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=tool_reply("finish", {"summary": "ok"}))

    resp = await provider(handler).complete([{"role": "user", "content": "t"}])
    assert resp.action["tool"] == "finish" and calls["n"] == 3
    assert sleeps == [7.0, 2.0]  # Retry-After first, then exponential backoff


async def test_gives_up_after_max_attempts_and_non_retryable_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(s: float) -> None:
        return None

    monkeypatch.setattr(openai_compat.asyncio, "sleep", no_sleep)
    calls = {"n": 0}

    def always_429(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    with pytest.raises(httpx.HTTPStatusError):
        await provider(always_429).complete([{"role": "user", "content": "t"}])
    assert calls["n"] == openai_compat.MAX_ATTEMPTS

    calls["n"] = 0

    def bad_request(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(httpx.HTTPStatusError):
        await provider(bad_request).complete([{"role": "user", "content": "t"}])
    assert calls["n"] == 1


async def test_cost_when_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOENIX_USD_PER_MTOK_IN", "1")
    monkeypatch.setenv("PHOENIX_USD_PER_MTOK_OUT", "5")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tool_reply("finish", {}, 1_000_000, 1_000_000))

    resp = await provider(handler).complete([{"role": "user", "content": "t"}])
    assert str(resp.cost_usd) == "6"


def message(name: str, args: object) -> dict[str, Any]:
    return dict(tool_reply(name, args)["choices"][0]["message"])


def test_argument_parsing_edge_cases() -> None:
    parsed = parse_action(message("write_file", '{"path":"a","content":"b"}'))
    assert parsed["args"] == {"path": "a", "content": "b"}
    assert parse_action(message("finish", "not json"))["args"] == {}
    assert parse_action(message("finish", ""))["args"] == {}


def test_message_translation_tool_round_trip_and_text_reply() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "action": {"tool": "write_file", "args": {"path": "a", "content": "b"}},
        },
        {"role": "user", "observation": {"ok": True}},
        {"role": "assistant", "action": {"tool": "invalid", "args": {"text": "hmm"}}},
        {"role": "user", "observation": {"error": "no tool call"}},
    ]
    out = to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert out[2]["tool_calls"][0]["id"] == out[3]["tool_call_id"]
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"])["path"] == "a"
    assert out[4]["content"] == "hmm" and "no tool call" in out[5]["content"]


def test_disabled_without_flag_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOENIX_REAL_LLM")
    with pytest.raises(RuntimeError, match="disabled"):
        OpenAICompatProvider()
    monkeypatch.setenv("PHOENIX_REAL_LLM", "1")
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        OpenAICompatProvider()


def test_factory_selects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOENIX_PROVIDER", raising=False)
    assert provider_kind() == "anthropic" and required_key_env() == "ANTHROPIC_API_KEY"
    monkeypatch.setenv("PHOENIX_PROVIDER", "gemini")
    assert required_key_env() == "GEMINI_API_KEY"
    monkeypatch.setenv("PHOENIX_PROVIDER", "nope")
    with pytest.raises(RuntimeError, match="unknown PHOENIX_PROVIDER"):
        provider_kind()


async def test_thinking_tokens_are_counted_and_errors_keep_the_body() -> None:
    def thinking(req: httpx.Request) -> httpx.Response:
        body = tool_reply("finish", {}, prompt=225, completion=320)
        body["usage"]["total_tokens"] = 1066  # 225 prompt + 320 visible + 521 hidden thinking
        return httpx.Response(200, json=body)

    resp = await provider(thinking).complete([{"role": "user", "content": "t"}])
    assert (resp.input_tokens, resp.output_tokens) == (225, 841)

    def gone(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model is no longer available")

    with pytest.raises(httpx.HTTPStatusError, match="no longer available"):
        await provider(gone).complete([{"role": "user", "content": "t"}])


def test_thought_signature_is_captured_and_replayed_but_ignored_by_loop_detection() -> None:
    from phoenix.budget import detect_loop
    from phoenix.store import Event

    ext = {"google": {"thought_signature": "SIG1"}}
    call = {
        "function": {"name": "run_code", "arguments": '{"path":"a.py"}'},
        "extra_content": ext,
    }
    action = parse_action({"tool_calls": [call]})
    assert action == {"tool": "run_code", "args": {"path": "a.py"}, "_ext": ext}

    out = to_openai_messages(
        [
            {"role": "user", "content": "t"},
            {"role": "assistant", "action": action},
            {"role": "user", "observation": {"exit_code": 0}},
        ]
    )
    assert out[2]["tool_calls"][0]["extra_content"] == ext  # echoed back to the model

    import uuid

    rid = uuid.uuid4()
    same_call_new_signature = [
        Event(rid, i, "agent_step", {"action": {**action, "_ext": {"sig": i}}}, f"{rid}:{i}")
        for i in range(3)
    ]
    assert detect_loop(same_call_new_signature)  # signatures differ, the action does not


async def test_retry_delay_from_google_body_and_request_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(openai_compat.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("PHOENIX_MIN_REQUEST_INTERVAL_S", "13")
    calls = {"n": 0}
    quota = '[{"error": {"details": [{"retryDelay": "37s"}]}}]'

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text=quota)
        return httpx.Response(200, json=tool_reply("finish", {}))

    p = provider(handler)
    await p.complete([{"role": "user", "content": "t"}])
    assert 38.0 in sleeps  # 37s from the body + 1s margin
    await p.complete([{"role": "user", "content": "t"}])
    assert any(0 < s <= 13 for s in sleeps)  # the pacer spaced the next request out


async def test_daily_quota_fails_fast_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    from phoenix.llm.openai_compat import QuotaExhaustedError

    async def no_sleep(s: float) -> None:
        raise AssertionError("must not wait on a daily quota")

    monkeypatch.setattr(openai_compat.asyncio, "sleep", no_sleep)
    calls = {"n": 0}
    body = '{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "retryDelay": "15s"}'

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text=body)

    with pytest.raises(QuotaExhaustedError, match="daily quota"):
        await provider(handler).complete([{"role": "user", "content": "t"}])
    assert calls["n"] == 1
