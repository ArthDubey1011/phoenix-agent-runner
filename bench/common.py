"""Shared helpers for the bench scripts. Every number quoted in the README is printed by one
of these scripts and saved under bench/results/."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

RESULTS = Path(__file__).resolve().parent / "results"


def database_url() -> str:
    from phoenix.envfile import load_dotenv

    load_dotenv()  # ./.env fills in whatever the shell did not set
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("set DATABASE_URL (see .env.example)")
    return url


def environment() -> dict[str, str]:
    return {"python": platform.python_version(), "os": platform.platform()}


def save(name: str, data: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data = {"environment": environment(), **data}
    text = json.dumps(data, indent=2)
    (RESULTS / f"{name}.json").write_text(text + "\n", encoding="utf8")
    print(text)


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    idx = max(0, min(len(sorted_values) - 1, round(q * len(sorted_values) + 0.5) - 1))
    return sorted_values[idx]
