"""Sandbox limits. Defaults are the most restrictive that still run Python; any loosening
must carry a comment explaining why."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "python:3.12-slim"
    timeout_s: float = 10.0
    memory_mb: int = 128  # hard cap; swap is set equal so the container cannot swap past it
    cpus: float = 0.5
    pids_limit: int = 64  # bounds fork bombs
    max_output_bytes: int = 64 * 1024  # per stream, returned to the caller
    tmpfs_mb: int = 16
    network: bool = False  # never loosened by default: generated code gets no network
    user: str = "65534:65534"  # nobody
