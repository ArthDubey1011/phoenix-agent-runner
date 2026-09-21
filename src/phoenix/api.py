"""HTTP API: submit runs and read their status, events and timeline.

Deliberately small and conservative. A submitted agent run can spend real LLM money, so:
submissions are validated and capped (budget, steps), callers cannot supply mock scripts, and
if PHOENIX_API_KEY is set every request needs it in the X-API-Key header. `python -m phoenix
api` binds to localhost by default; put it behind real auth before exposing it."""

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from phoenix.engine import submit_run
from phoenix.llm.mock import SCENARIOS
from phoenix.store import create_pool, read_events
from phoenix.timeline import build_timeline

MAX_STEPS_CAP = 50
DEFAULT_MAX_BUDGET = Decimal("0.10")


class SubmitRequest(BaseModel):
    kind: Literal["toy", "agent"]
    # agent runs
    prompt: str | None = Field(default=None, max_length=8000)
    mock_scenario: str | None = None  # a named built-in scenario; never an arbitrary script
    max_steps: int = Field(default=20, ge=1, le=MAX_STEPS_CAP)
    # toy runs
    steps: int = Field(default=20, ge=1, le=200)
    step_delay: float = Field(default=0.0, ge=0.0, le=2.0)
    budget_usd: Decimal = Field(default=Decimal("0.05"), gt=0)

    @model_validator(mode="after")
    def _check(self) -> SubmitRequest:
        if self.kind == "agent":
            if not self.prompt:
                raise ValueError("agent runs need a prompt")
            if self.mock_scenario is not None and self.mock_scenario not in SCENARIOS:
                raise ValueError(f"unknown mock_scenario; choose from {sorted(SCENARIOS)}")
        return self

    def to_spec(self) -> dict[str, Any]:
        if self.kind == "toy":
            return {"kind": "toy", "steps": self.steps, "step_delay": self.step_delay}
        spec: dict[str, Any] = {"kind": "agent", "prompt": self.prompt, "max_steps": self.max_steps}
        if self.mock_scenario:
            spec["mock_scenario"] = self.mock_scenario
        return spec


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool  # type: ignore[no-any-return]


def create_app(pool: asyncpg.Pool | None = None) -> FastAPI:
    """Pass a pool in tests; otherwise one is created from DATABASE_URL at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = pool is None
        app.state.pool = pool or await create_pool(os.environ["DATABASE_URL"])
        try:
            yield
        finally:
            if owned:
                await app.state.pool.close()

    app = FastAPI(title="Phoenix", lifespan=lifespan)
    if pool is not None:
        app.state.pool = pool

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        expected = os.environ.get("PHOENIX_API_KEY")
        if expected and not hmac.compare_digest(x_api_key or "", expected):
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    auth = [Depends(require_key)]

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        await _pool(request).fetchval("SELECT 1")
        return {"status": "ok"}

    @app.post("/runs", status_code=202, dependencies=auth)
    async def submit(body: SubmitRequest, request: Request) -> dict[str, str]:
        cap = Decimal(os.environ.get("PHOENIX_API_MAX_BUDGET_USD", str(DEFAULT_MAX_BUDGET)))
        if body.budget_usd > cap:
            raise HTTPException(status_code=422, detail=f"budget_usd exceeds the cap of {cap}")
        run_id = await submit_run(_pool(request), body.to_spec(), body.budget_usd)
        return {"run_id": str(run_id)}

    async def _run_or_404(request: Request, run_id: uuid.UUID) -> asyncpg.Record:
        row = await _pool(request).fetchrow(
            "SELECT r.status, r.budget_usd, r.spent_usd, r.created_at, r.finished_at, "
            "t.status AS task_status, t.attempt, "
            "(SELECT count(*) FROM events e WHERE e.run_id = r.id) AS events "
            "FROM runs r LEFT JOIN tasks t ON t.run_id = r.id WHERE r.id = $1",
            run_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return row

    @app.get("/runs/{run_id}", dependencies=auth)
    async def status(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
        r = await _run_or_404(request, run_id)
        return {
            "run_id": str(run_id),
            "status": r["status"],
            "budget_usd": str(r["budget_usd"]),
            "spent_usd": str(r["spent_usd"]),
            "events": r["events"],
            "task": {"status": r["task_status"], "attempts": r["attempt"]},
            "created_at": r["created_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        }

    @app.get("/runs/{run_id}/events", dependencies=auth)
    async def events(run_id: uuid.UUID, request: Request) -> list[dict[str, Any]]:
        await _run_or_404(request, run_id)
        return [
            {"seq": e.seq, "type": e.type, "payload": e.payload}
            for e in await read_events(_pool(request), run_id)
        ]

    @app.get("/runs/{run_id}/timeline", response_class=PlainTextResponse, dependencies=auth)
    async def timeline(run_id: uuid.UUID, request: Request) -> str:
        await _run_or_404(request, run_id)
        return await build_timeline(_pool(request), run_id)

    return app
