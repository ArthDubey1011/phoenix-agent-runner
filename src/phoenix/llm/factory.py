"""Choose the real provider. PHOENIX_PROVIDER=gemini | anthropic (default anthropic)."""

from __future__ import annotations

import os

from phoenix.llm.base import LLMProvider

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


def provider_kind() -> str:
    kind = os.environ.get("PHOENIX_PROVIDER", "anthropic").lower()
    if kind not in KEY_ENV:
        raise RuntimeError(f"unknown PHOENIX_PROVIDER {kind!r}; use one of {sorted(KEY_ENV)}")
    return kind


def required_key_env() -> str:
    return KEY_ENV[provider_kind()]


def default_model() -> str:
    from phoenix.llm.anthropic import DEFAULT_MODEL
    from phoenix.llm.openai_compat import DEFAULT_GEMINI_MODEL

    fallback = DEFAULT_GEMINI_MODEL if provider_kind() == "gemini" else DEFAULT_MODEL
    return os.environ.get("PHOENIX_MODEL", fallback)


def real_provider() -> LLMProvider:
    if provider_kind() == "gemini":
        from phoenix.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider()
    from phoenix.llm.anthropic import AnthropicProvider

    return AnthropicProvider()
