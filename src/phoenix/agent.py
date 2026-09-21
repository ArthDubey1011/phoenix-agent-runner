"""The agent loop: write code -> run it in the sandbox -> read the error -> fix it.

One step = one LLM call + one tool execution = one event = one atomic commit. Everything the
loop needs (history, spend, workspace contents) is rebuilt from the replayed events, so a
worker that takes over mid-run continues exactly where the dead one stopped."""

from __future__ import annotations

import asyncio
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from phoenix.budget import DEFAULT_MAX_STEPS, check_limits
from phoenix.engine import StepOutcome
from phoenix.leases import Claim
from phoenix.llm.base import LLMProvider
from phoenix.llm.cache import complete_cached
from phoenix.sandbox.runner import Sandbox
from phoenix.store import Event

OBSERVATION_CHARS = 2000  # cap what is fed back to the model (and stored in the event)
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


class UnsafePathError(ValueError):
    pass


def safe_join(root: Path, rel: str) -> Path:
    """Resolve an agent-supplied path inside `root` or raise. Rejects absolute paths,
    `..`, backslashes and odd characters; the final check also catches symlink tricks."""
    if not isinstance(rel, str) or not _PATH_RE.match(rel) or ".." in Path(rel).parts:
        raise UnsafePathError(f"unsafe path: {rel!r}")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise UnsafePathError(f"path escapes workspace: {rel!r}")
    return target


def materialize_workspace(root: Path, events: list[Event]) -> None:
    """Rebuild the workspace from write_file events (event sourcing applies to files too)."""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    for e in events:
        action = e.payload.get("action", {})
        if action.get("tool") == "write_file" and "error" not in e.payload.get("observation", {}):
            target = safe_join(root, action["args"]["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action["args"]["content"], encoding="utf8")


def build_messages(spec: dict[str, Any], events: list[Event]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": spec["prompt"]}]
    for e in events:
        if "action" in e.payload:
            messages.append({"role": "assistant", "action": e.payload["action"]})
            messages.append({"role": "user", "observation": e.payload.get("observation", {})})
    return messages


def _clip(text: str) -> str:
    return text if len(text) <= OBSERVATION_CHARS else text[:OBSERVATION_CHARS] + "...[truncated]"


class AgentWorkflow:
    def __init__(
        self,
        spec: dict[str, Any],
        pool: asyncpg.Pool,
        provider: LLMProvider,
        sandbox: Sandbox,
        workspace_root: Path,
    ) -> None:
        self.spec = spec
        self.pool = pool
        self.provider = provider
        self.sandbox = sandbox
        self.workspace_root = workspace_root
        self.max_steps = int(spec.get("max_steps", DEFAULT_MAX_STEPS))
        self._workspace: Path | None = None

    def workspace(self, claim: Claim) -> Path:
        # One directory per attempt: a zombie still writing into its old attempt's directory
        # cannot touch the new owner's files.
        return self.workspace_root / str(claim.run_id) / f"a{claim.attempt}"

    async def next_step(self, claim: Claim, step_index: int, events: list[Event]) -> StepOutcome:
        if self._workspace is None:
            self._workspace = self.workspace(claim)
            materialize_workspace(self._workspace, events)
        budget = await self.pool.fetchval("SELECT budget_usd FROM runs WHERE id=$1", claim.run_id)
        stop = check_limits(events, Decimal(budget), self.max_steps)
        if stop is not None:
            return StepOutcome(
                type=stop.status,
                payload={"reason": stop.reason},
                final=True,
                final_status=stop.status,
            )

        messages = build_messages(self.spec, events)
        resp = await complete_cached(
            self.pool,
            self.provider,
            claim.run_id,
            step_index,
            messages,
            claim.attempt,
            claim.worker_id,
        )
        action = resp.action
        observation = await self._execute(self._workspace, action)
        payload = {
            "action": action,
            "observation": observation,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cost_usd": str(resp.cost_usd),
        }
        finished = action.get("tool") == "finish"
        return StepOutcome(
            type="finish" if finished else "agent_step",
            payload=payload,
            cost_usd=resp.cost_usd,
            final=finished,
        )

    async def _execute(self, workspace: Path, action: dict[str, Any]) -> dict[str, Any]:
        tool, args = action.get("tool"), action.get("args", {})
        try:
            if tool == "write_file":
                target = safe_join(workspace, args["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(args["content"]), encoding="utf8")
                return {"ok": True}
            if tool == "run_code":
                path = safe_join(workspace, args["path"])
                res = await asyncio.to_thread(
                    self.sandbox.run,
                    workspace,
                    ["python", str(path.relative_to(workspace.resolve()))],
                )
                return {
                    "exit_code": res.exit_code,
                    "stdout": _clip(res.stdout),
                    "stderr": _clip(res.stderr),
                    "timed_out": res.timed_out,
                    "oom_killed": res.oom_killed,
                }
            if tool == "finish":
                return {"summary": str(args.get("summary", ""))}
            if tool == "invalid":
                return {"error": "no tool call. Call exactly one of: write_file, run_code, finish"}
            return {"error": f"unknown tool {tool!r}"}
        except (UnsafePathError, KeyError, TypeError) as exc:
            return {"error": str(exc)}
