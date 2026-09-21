"""HTTP API tests through an in-process ASGI transport (no network, MockProvider only)."""

import asyncpg
import httpx
import pytest

from phoenix.api import create_app
from phoenix.engine import EngineContext, StepOutcome, commit_step, execute_task
from phoenix.leases import claim_task


def client(pool: asyncpg.Pool) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(pool))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health(pool: asyncpg.Pool) -> None:
    async with client(pool) as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_submit_status_events_and_timeline_for_a_toy_run(pool: asyncpg.Pool) -> None:
    async with client(pool) as c:
        r = await c.post("/runs", json={"kind": "toy", "steps": 3})
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        s = (await c.get(f"/runs/{run_id}")).json()
        assert (s["status"], s["events"], s["task"]) == (
            "pending",
            0,
            {"status": "queued", "attempts": 0},
        )

        claim = await claim_task(pool, "w1", 60)
        assert claim is not None and str(claim.run_id) == run_id
        for i in range(3):
            out = StepOutcome("toy_step", {"i": i, "cost_usd": "0.0001"}, final=i == 2)
            await commit_step(pool, claim, i, out)

        s = (await c.get(f"/runs/{run_id}")).json()
        assert s["status"] == "succeeded" and s["events"] == 3 and s["finished_at"]
        ev = (await c.get(f"/runs/{run_id}/events")).json()
        assert [e["seq"] for e in ev] == [1, 2, 3] and ev[0]["type"] == "toy_step"
        tl = await c.get(f"/runs/{run_id}/timeline")
        assert tl.headers["content-type"].startswith("text/plain")
        assert "status=succeeded" in tl.text and tl.text.count("seq=") == 3


async def test_agent_run_with_mock_scenario_end_to_end(
    pool: asyncpg.Pool, ctx: EngineContext
) -> None:
    body = {"kind": "agent", "prompt": "print hello", "mock_scenario": "fail_twice_then_fix"}
    async with client(pool) as c:
        run_id = (await c.post("/runs", json=body)).json()["run_id"]
        claim = await claim_task(pool, "w1", 60)
        assert claim is not None
        await execute_task(pool, claim, ctx)
        s = (await c.get(f"/runs/{run_id}")).json()
        assert s["status"] == "succeeded" and s["events"] == 7
        assert "ZeroDivisionError" in (await c.get(f"/runs/{run_id}/timeline")).text


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "toy", "steps": 0},
        {"kind": "toy", "steps": 500},
        {"kind": "agent"},  # no prompt
        {"kind": "agent", "prompt": "x", "mock_scenario": "not_a_scenario"},
        {"kind": "agent", "prompt": "x", "max_steps": 10_000},
        {"kind": "agent", "prompt": "x" * 9000},
        {"kind": "toy", "budget_usd": "0"},
        {"kind": "toy", "budget_usd": "5.00"},  # above the default $0.10 cap
        {"kind": "agent", "prompt": "x", "mock_script": [{"tool": "finish", "args": {}}]},
        {"kind": "other"},
    ],
)
async def test_invalid_or_over_budget_submissions_are_rejected(
    pool: asyncpg.Pool, body: dict[str, object]
) -> None:
    async with client(pool) as c:
        r = await c.post("/runs", json=body)
    # an unknown extra field (mock_script) is ignored, so that one must not become a script
    if "mock_script" in body:
        assert r.status_code == 202
        spec = await pool.fetchval("SELECT task_spec::text FROM runs LIMIT 1")
        assert "mock_script" not in spec
    else:
        assert r.status_code == 422
        assert await pool.fetchval("SELECT count(*) FROM runs") == 0


async def test_budget_cap_is_configurable(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOENIX_API_MAX_BUDGET_USD", "1.00")
    async with client(pool) as c:
        assert (
            await c.post("/runs", json={"kind": "toy", "budget_usd": "0.75"})
        ).status_code == 202
        assert (
            await c.post("/runs", json={"kind": "toy", "budget_usd": "1.50"})
        ).status_code == 422


async def test_unknown_run_and_bad_id(pool: asyncpg.Pool) -> None:
    async with client(pool) as c:
        missing = "00000000-0000-0000-0000-000000000000"
        for path in ("", "/events", "/timeline"):
            assert (await c.get(f"/runs/{missing}{path}")).status_code == 404
        assert (await c.get("/runs/not-a-uuid")).status_code == 422


async def test_api_key_is_enforced_when_configured(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOENIX_API_KEY", "s3cret")
    async with client(pool) as c:
        assert (await c.post("/runs", json={"kind": "toy"})).status_code == 401
        bad = await c.post("/runs", json={"kind": "toy"}, headers={"X-API-Key": "nope"})
        assert bad.status_code == 401
        ok = await c.post("/runs", json={"kind": "toy"}, headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 202
        run_id = ok.json()["run_id"]
        assert (await c.get(f"/runs/{run_id}")).status_code == 401
        assert (await c.get(f"/runs/{run_id}", headers={"X-API-Key": "s3cret"})).status_code == 200
        assert (await c.get("/health")).status_code == 200  # health stays open for probes
