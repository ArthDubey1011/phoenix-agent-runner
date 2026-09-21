"""Provider interface. Messages are provider-neutral dicts:
{"role": "user", "content": str} | {"role": "assistant", "action": {...}}
| {"role": "user", "observation": {...}}. Providers translate them."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMResponse:
    action: dict[str, Any]  # {"tool": str, "args": dict}
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class LLMProvider(Protocol):
    name: str

    async def complete(self, messages: list[dict[str, Any]]) -> LLMResponse: ...


def token_cost(
    input_tokens: int, output_tokens: int, usd_per_mtok_in: str, usd_per_mtok_out: str
) -> Decimal:
    return (
        Decimal(input_tokens) * Decimal(usd_per_mtok_in)
        + Decimal(output_tokens) * Decimal(usd_per_mtok_out)
    ) / Decimal(1_000_000)


def cache_key(provider: str, run_id: uuid.UUID, step_index: int, messages: list[Any]) -> str:
    """Scoped to (run, step): the cache only closes the crash window, it never shares
    answers between runs."""
    blob = json.dumps([provider, str(run_id), step_index, messages], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()
