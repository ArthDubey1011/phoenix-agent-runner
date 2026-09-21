"""Run limits: cost cap, max steps, loop detection. All decided from replayed events, so
they survive crashes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from phoenix.store import Event

DEFAULT_MAX_STEPS = 20
LOOP_WINDOW = 3  # this many identical consecutive actions = stuck


@dataclass(frozen=True)
class Termination:
    status: str  # failed | budget_exceeded
    reason: str


def spent_from_events(events: list[Event]) -> Decimal:
    return sum((Decimal(e.payload.get("cost_usd", "0")) for e in events), Decimal(0))


def _fingerprint(action: dict[str, Any]) -> str:
    # "_"-prefixed keys are provider metadata (e.g. thought signatures) that differ every call
    public = {k: v for k, v in action.items() if not k.startswith("_")}
    return json.dumps(public, sort_keys=True)


def detect_loop(events: list[Event], window: int = LOOP_WINDOW) -> bool:
    actions = [e.payload["action"] for e in events if "action" in e.payload]
    if len(actions) < window:
        return False
    return len({_fingerprint(a) for a in actions[-window:]}) == 1


def check_limits(
    events: list[Event], budget_usd: Decimal, max_steps: int = DEFAULT_MAX_STEPS
) -> Termination | None:
    """Checked before every LLM call. The cap is enforced between calls, so a run can
    overshoot by at most the cost of one call."""
    if spent_from_events(events) >= budget_usd:
        return Termination("budget_exceeded", "budget cap reached")
    if len(events) >= max_steps:
        return Termination("failed", f"max_steps ({max_steps}) reached")
    if detect_loop(events):
        return Termination("failed", f"loop detected: {LOOP_WINDOW} identical actions in a row")
    return None
