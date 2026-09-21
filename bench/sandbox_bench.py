"""Sandbox overhead: wall-clock time to run `print(1)` in a fresh locked-down container
(create + start + run + collect output + remove).

    python bench/sandbox_bench.py [--trials 20]
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from common import percentile, save
from phoenix.sandbox.policy import SandboxPolicy
from phoenix.sandbox.runner import DockerRunner


def main(trials: int) -> None:
    runner = DockerRunner()
    runner.ensure_image()
    ws = Path(tempfile.mkdtemp(prefix="phoenix-sbx-bench-"))
    (ws / "main.py").write_text("print(1)\n")
    policy = SandboxPolicy()
    runner.run(ws, ["python", "main.py"], policy)  # warm-up, not measured
    wall, inner = [], []
    for _ in range(trials):
        t = time.perf_counter()
        res = runner.run(ws, ["python", "main.py"], policy)
        wall.append((time.perf_counter() - t) * 1000)
        inner.append(float(res.duration_ms))
        assert res.exit_code == 0 and res.stdout.strip() == "1"
    wall.sort()
    inner.sort()
    save(
        "sandbox",
        {
            "config": {"trials": trials, "image": policy.image, "command": "python main.py"},
            "wall_ms": {
                "median": round(statistics.median(wall)),
                "p95": round(percentile(wall, 0.95)),
                "max": round(wall[-1]),
            },
            "container_start_to_exit_ms": {
                "median": round(statistics.median(inner)),
                "p95": round(percentile(inner, 0.95)),
            },
        },
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    main(ap.parse_args().trials)
