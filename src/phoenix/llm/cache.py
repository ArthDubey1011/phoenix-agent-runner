"""llm_cache + provider_calls.

Crash window: `provider_calls` is written BEFORE the provider is invoked (so every
invocation is counted) and the response is cached right after it returns. A worker that
dies after the answer but before the commit finds the cached answer on retry and does not
call again. A worker that dies between the provider returning and the cache insert (a few
microseconds of Python plus one INSERT) repeats the call; that shows up as a second
provider_calls row for the step, which the chaos report counts as a crash-window retry."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from phoenix.llm.base import LLMProvider, LLMResponse, cache_key


async def complete_cached(
    pool: asyncpg.Pool,
    provider: LLMProvider,
    run_id: uuid.UUID,
    step_index: int,
    messages: list[dict[str, Any]],
    attempt: int = 0,
    worker_id: str | None = None,
) -> LLMResponse:
    key = cache_key(provider.name, run_id, step_index, messages)
    row = await pool.fetchrow(
        "SELECT response::text AS response, input_tokens, output_tokens, cost_usd "
        "FROM llm_cache WHERE key=$1",
        key,
    )
    if row is not None:
        return LLMResponse(
            json.loads(row["response"]), row["input_tokens"], row["output_tokens"], row["cost_usd"]
        )
    await pool.execute(
        "INSERT INTO provider_calls(run_id, step_index, cache_key, attempt, worker_id) "
        "VALUES ($1,$2,$3,$4,$5)",
        run_id,
        step_index,
        key,
        attempt,
        worker_id,
    )
    resp = await provider.complete(messages)
    await pool.execute(
        "INSERT INTO llm_cache(key, response, input_tokens, output_tokens, cost_usd) "
        "VALUES ($1,$2::jsonb,$3,$4,$5) ON CONFLICT (key) DO NOTHING",
        key,
        json.dumps(resp.action),
        resp.input_tokens,
        resp.output_tokens,
        Decimal(resp.cost_usd),
    )
    return resp
