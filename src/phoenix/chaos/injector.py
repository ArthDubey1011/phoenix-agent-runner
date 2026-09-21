"""Fault injection: worker processes (kill -9, SIGSTOP/SIGCONT equivalents via psutil, which
also works on Windows) and sandbox containers (docker kill)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import docker
import psutil

from phoenix.sandbox.runner import LABEL

SRC = Path(__file__).resolve().parents[2]


@dataclass
class Worker:
    name: str
    proc: subprocess.Popen[bytes]
    log: Path
    resume_at: float | None = None  # monotonic time; set while suspended

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class Injector:
    def __init__(
        self,
        database_url: str,
        log_dir: Path,
        lease_seconds: float,
        workspace_root: Path,
        unsafe_no_fencing: bool = False,
        real_llm: bool = False,
    ) -> None:
        self.database_url = database_url
        self.log_dir = log_dir
        self.lease_seconds = lease_seconds
        self.workspace_root = workspace_root
        self.unsafe_no_fencing = unsafe_no_fencing
        self.real_llm = real_llm
        self.workers: list[Worker] = []
        self._n = 0
        log_dir.mkdir(parents=True, exist_ok=True)

    def spawn_worker(self) -> Worker:
        self._n += 1
        name = f"w{self._n}"
        env = {
            **os.environ,
            "DATABASE_URL": self.database_url,
            "PYTHONPATH": str(SRC),
            "PHOENIX_REAL_LLM": "1" if self.real_llm else "0",  # chaos never sets this
            "PHOENIX_WORKSPACE_ROOT": str(self.workspace_root),
        }
        if self.unsafe_no_fencing:
            env["PHOENIX_UNSAFE_NO_FENCING"] = "1"
        log = self.log_dir / f"{name}.log"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "phoenix",
                "worker",
                "--id",
                name,
                "--lease",
                str(self.lease_seconds),
            ],
            env=env,
            stderr=log.open("w"),
            stdout=subprocess.DEVNULL,
        )
        worker = Worker(name, proc, log)
        self.workers.append(worker)
        return worker

    def running(self) -> list[Worker]:
        """Alive and not suspended, in a stable order (so a seeded pick is reproducible)."""
        return [w for w in self.workers if w.alive and w.resume_at is None]

    def kill(self, w: Worker) -> None:
        psutil.Process(w.proc.pid).kill()  # hard kill, no cleanup: the `kill -9` case
        w.proc.wait()

    def suspend(self, w: Worker, until: float) -> None:
        psutil.Process(w.proc.pid).suspend()  # SIGSTOP
        w.resume_at = until

    def resume(self, w: Worker) -> None:
        if w.alive:
            psutil.Process(w.proc.pid).resume()  # SIGCONT
        w.resume_at = None

    def docker_kill(self, pick: float) -> str | None:
        """`docker kill` one running sandbox container; None if there was none to kill."""
        try:
            client = docker.from_env()
            containers = sorted(
                client.containers.list(filters={"label": LABEL, "status": "running"}),
                key=lambda c: str(c.name),
            )
            if not containers:
                return None
            victim = containers[int(pick * len(containers)) % len(containers)]
            victim.kill()
            return str(victim.name)
        except docker.errors.DockerException:
            return None  # raced with the container exiting on its own

    def shutdown(self) -> None:
        for w in self.workers:
            if w.alive:
                self.resume(w)
                psutil.Process(w.proc.pid).kill()
            w.proc.wait()
        try:
            for c in docker.from_env().containers.list(all=True, filters={"label": LABEL}):
                c.remove(force=True)
        except docker.errors.DockerException:
            pass


def pick(u: float, items: list[Worker]) -> Worker | None:
    """Choose by a pre-drawn uniform u in [0,1), so the schedule alone determines the choice."""
    return items[int(u * len(items))] if items else None
