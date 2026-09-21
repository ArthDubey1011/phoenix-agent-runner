"""Minimal .env loader for the CLI (no dependency). Real environment variables always win, and
empty values are ignored, so a copied .env.example with blank keys changes nothing."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | str = ".env") -> list[str]:
    """Load KEY=VALUE lines into os.environ; returns the names it set."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
