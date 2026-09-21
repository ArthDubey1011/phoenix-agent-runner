"""Run an eval and save the result under bench/results/. Mock numbers validate the pipeline
(submit -> workers -> sandbox -> hidden tests); they are not a model measurement.

    python bench/eval_bench.py                          # standard suite, mock provider
    python bench/eval_bench.py --suite challenge        # challenge suite, mock provider
    python bench/eval_bench.py --real --workers 1 [--suite challenge]   # real model (costs quota)
"""

from __future__ import annotations

import sys

from common import RESULTS, database_url
from phoenix.__main__ import main

if __name__ == "__main__":
    database_url()
    args = sys.argv[1:]
    suite = args[args.index("--suite") + 1] if "--suite" in args else "standard"
    mode = "gemini" if "--real" in args else "mock"
    name = f"eval-{mode}" + ("" if suite == "standard" else f"-{suite}")
    sys.exit(main(["eval", "--out", str(RESULTS / f"{name}.json"), *args]))
