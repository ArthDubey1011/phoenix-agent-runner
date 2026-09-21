"""Run untrusted code in a locked-down Docker container.

Docker is a namespace/cgroup boundary, not a hard security boundary: a kernel or runtime
escape defeats it. gVisor or Firecracker would be the stronger choice (see README threat model).
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import docker
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import Container
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from phoenix.sandbox.policy import SandboxPolicy

LABEL = "phoenix.sandbox"
OOM_RECHECKS = 10  # x 0.1 s: a bounded wait, paid only by exit-137 containers
OOM_RECHECK_INTERVAL_S = 0.1


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool
    duration_ms: int
    truncated: bool = False


class Sandbox(Protocol):
    """What the agent needs from a sandbox; DockerRunner is the real implementation."""

    def run(
        self, workspace: Path, cmd: list[str], policy: SandboxPolicy | None = None
    ) -> SandboxResult: ...


class DockerRunner:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self.client = client or docker.from_env()

    def ensure_image(self, image: str = SandboxPolicy.image) -> None:
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def run(
        self, workspace: Path, cmd: list[str], policy: SandboxPolicy | None = None
    ) -> SandboxResult:
        policy = policy or SandboxPolicy()
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        # LOOSENED on purpose: the container runs as uid 65534 (nobody), which on a Linux host
        # cannot write to a directory owned by the caller. Only this one workspace directory is
        # made world-writable (Docker Desktop on Windows/macOS ignores the mode).
        workspace.chmod(0o777)
        container = self.client.containers.create(
            policy.image,
            cmd,
            name=f"phoenix-sbx-{uuid.uuid4().hex[:12]}",
            labels={LABEL: "1"},
            working_dir="/workspace",
            user=policy.user,
            network_mode="bridge" if policy.network else "none",
            read_only=True,  # root FS immutable; only /workspace and /tmp are writable
            tmpfs={"/tmp": f"rw,noexec,nosuid,size={policy.tmpfs_mb}m"},
            volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
            mem_limit=f"{policy.memory_mb}m",
            memswap_limit=f"{policy.memory_mb}m",
            nano_cpus=int(policy.cpus * 1e9),
            pids_limit=policy.pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            # Bound daemon-side log storage so a stdout flood cannot fill the disk.
            log_config={
                "type": "json-file",
                "config": {
                    "max-size": str(max(policy.max_output_bytes * 4, 10_000_000)),
                    "max-file": "1",
                },
            },
            detach=True,
        )
        started = time.monotonic()
        timed_out = False
        try:
            container.start()
            with ThreadPoolExecutor(max_workers=2) as pool:
                # Read output live and keep only the head, so a flood costs no memory here
                # and is not lost to daemon-side log rotation.
                out_f = pool.submit(self._capture, container, policy.max_output_bytes, True)
                err_f = pool.submit(self._capture, container, policy.max_output_bytes, False)
                try:
                    exit_code = int(container.wait(timeout=policy.timeout_s)["StatusCode"])
                except (ReadTimeout, RequestsConnectionError):
                    timed_out = True
                    self._kill(container)
                    exit_code = int(container.wait()["StatusCode"])
                duration_ms = int((time.monotonic() - started) * 1000)
                out, out_cut = out_f.result()
                err, err_cut = err_f.result()
            oom = self._oom_killed(container, exit_code, timed_out)
            return SandboxResult(
                exit_code, out, err, timed_out, oom, duration_ms, out_cut or err_cut
            )
        finally:
            self._remove(container)

    @staticmethod
    def _oom_killed(container: Container, exit_code: int, timed_out: bool) -> bool:
        """Docker's OOMKilled flag. On Linux the container-exit event can be processed before
        the OOM event that sets the flag, so a single read right after exit can say False for a
        container the kernel just OOM-killed (seen on GitHub's runners). For a SIGKILL exit (137)
        that we did not cause with a timeout, re-read briefly until the flag shows up."""
        container.reload()
        oom = bool(container.attrs["State"].get("OOMKilled"))
        if oom or exit_code != 137 or timed_out:
            return oom
        for _ in range(OOM_RECHECKS):
            time.sleep(OOM_RECHECK_INTERVAL_S)
            container.reload()
            if container.attrs["State"].get("OOMKilled"):
                return True
        return False

    @staticmethod
    def _capture(container: Container, limit: int, stdout: bool) -> tuple[str, bool]:
        buf = bytearray()
        total = 0
        for chunk in container.logs(stdout=stdout, stderr=not stdout, stream=True, follow=True):
            total += len(chunk)  # keep draining so the container is never blocked on output
            if len(buf) < limit:
                buf += chunk[: limit - len(buf)]
        truncated = total > limit
        return bytes(buf).decode("utf8", "replace"), truncated

    @staticmethod
    def _kill(container: Container) -> None:
        try:
            container.kill()
        except NotFound:
            pass
        except docker.errors.APIError:
            pass  # already exited between the timeout and the kill

    @staticmethod
    def _remove(container: Container) -> None:
        try:
            container.remove(force=True)
        except NotFound:
            pass

    def leftover_containers(self) -> list[str]:
        return [c.name for c in self.client.containers.list(all=True, filters={"label": LABEL})]

    def remove_leftovers(self) -> None:
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            self._remove(c)
