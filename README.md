# Phoenix: crash-proof sandboxed agent runner

Phoenix runs LLM coding agents (write code, run it, read the error, fix it) so that a run
survives worker crashes, pauses and killed sandboxes, and every step is billed at most once.
State lives in PostgreSQL as an append-only event log; workers are plain processes that claim
work with leases; agent-written code runs in a locked-down Docker container.

**Read this first, honestly:**

- This is a **multi-worker system coordinated through PostgreSQL**, not a distributed-consensus
  system. Postgres is a single point of failure.
- **Docker is not a perfect security boundary.** See the [threat model](#threat-model).
- The mock eval numbers below come from a **scripted provider** and test the pipeline, not any
  model. One real-model run exists (Gemini, free tier; see the eval section, with its caveats).
  The Anthropic provider has **never been run against a live endpoint** (no key was available).
- Developed on Windows 11 + Docker Desktop + Python 3.13 against a local Postgres 17; all
  benchmarks were measured there. I also ran the full suite once on Ubuntu 24.04 (WSL2, native
  ext4 filesystem, Docker via Docker Desktop's WSL integration) with Python 3.12 and an embedded
  Postgres: lint clean and all 176 tests passed, including the Docker sandbox security tests, the
  slow chaos tests and the fencing-removal proof, with real Linux signals. A mixed `chaos --seed 1
  --runs 30` run with real `docker kill` faults also passed all 4 invariants there. That run
  confirmed the workspace permission fix is required: without the `chmod`, the sandbox's uid
  65534 gets `PermissionError` on a workspace owned by another uid. The GitHub Action (Ubuntu,
  Python 3.12, Postgres 16, real Docker) has now run: its first run caught a genuine flake, where
  Docker can report `OOMKilled=false` right after the kernel OOM-killed a container (the exit
  event beats the OOM event), which the sandbox runner now re-checks for; the next run passed
  180 tests (the 2 timing-dependent chaos tests are skipped in CI).
- The HTTP API is deliberately minimal (submit, status, events, timeline). It has an optional
  shared API key and no users, roles or rate limiting: keep it on localhost or behind real auth.

## Architecture

```
   CLI  (submit | status | timeline | worker | chaos | eval)  +  HTTP API (FastAPI)
                        |
                        v
                 PostgreSQL   <---- single source of truth
   runs | events (append-only) | tasks (leases) | llm_cache | provider_calls
                        ^
        claim (SKIP LOCKED) + commit (ONE txn) + heartbeat
      +-----------------+-----------------+
   Worker 1          Worker 2          Reaper (a loop inside every worker,
   (agent loop)      (agent loop)      requeues expired leases)
      |
      v
   Docker sandbox per run_code call
   (no network, read-only FS, mem/CPU/PID limits, timeout)
```

Workers are ordinary local processes, so `kill -9` and suspend/resume are easy to inject.
Only Postgres runs in docker-compose.

### The six ideas

1. **Event sourcing.** A run's state is a replay of its events. History, spend, loop detection
   and even the agent's workspace files are rebuilt from the log on takeover.
2. **Idempotency key** `run_id:step_index` with `UNIQUE (run_id, idem_key)`: a step commits at
   most once, even if two workers race.
3. **Atomic commit.** Fence check + event append + spend update (+ task/run completion on the
   last step) happen in one transaction ([engine.py](src/phoenix/engine.py) `commit_step`).
4. **Lease + heartbeat.** A dead worker's lease expires; the reaper requeues the task and
   another worker claims it (`FOR UPDATE SKIP LOCKED`).
5. **Fencing token.** `tasks.attempt` increments on every claim. A "zombie" that wakes up after
   losing its lease cannot commit, because the commit transaction checks its attempt.
6. **Honest crash window.** If a worker dies after the model answers but before the commit, the
   answer is in `llm_cache` and is reused. If it dies in the few microseconds between the
   provider returning and the cache insert, that one call is repeated; `provider_calls` records
   the attempt so the chaos report counts it as an explicit crash-window retry instead of
   hiding it.

## Quickstart

```bash
cp .env.example .env               # then: export the variables (DATABASE_URL etc.)
make up                            # Postgres 16 on :5433, runs migrations
make worker                        # in 2 terminals
python -m phoenix submit --steps 20 --step-delay 0.2 --count 3
python -m phoenix timeline <run_id>
make api                           # HTTP API on 127.0.0.1:8000 (see below)
make chaos SEED=1 RUNS=30          # seeded fault injection + 4 invariants
make eval                          # 15-task eval, mock provider by default
make bench                         # scripts that produce every number in this README
make test                          # unit + integration (security tests need Docker)
```

`make` is not installed on stock Windows; each target is one `python -m phoenix ...` command
shown in the [Makefile](Makefile).

### HTTP API

`python -m phoenix api [--host 127.0.0.1] [--port 8000]` (also needs a worker running):

```bash
curl -X POST localhost:8000/runs -H "X-API-Key: $PHOENIX_API_KEY" -H "Content-Type: application/json"      -d '{"kind": "toy", "steps": 5}'                       # -> 202 {"run_id": "..."}
curl localhost:8000/runs/<id>            # status, spend, event count, task attempts
curl localhost:8000/runs/<id>/events     # the raw event log
curl localhost:8000/runs/<id>/timeline   # the same view as `python -m phoenix timeline`
```

Agent runs take `{"kind": "agent", "prompt": "...", "max_steps": 20, "budget_usd": "0.05"}`. The
key header is only required when `PHOENIX_API_KEY` is set; `/health` is always open. I ran the
real server with a real worker process end to end once (auth, submit, status, timeline, budget
cap); the rest is covered by tests through an in-process ASGI transport.

The real LLM is opt-in. Tests never call a real model.

- **Gemini (free tier possible):** get a key from Google AI Studio, then (PowerShell shown)
  ```powershell
  $env:PHOENIX_REAL_LLM = "1"; $env:PHOENIX_PROVIDER = "gemini"
  $env:GEMINI_API_KEY = "<your key>"        # never commit it; .env is gitignored
  python -m phoenix eval --real --workers 1 --only 01 02   # smoke test first
  python -m phoenix eval --real --workers 1 --out bench/results/eval-gemini.json
  ```
  The default model is `gemini-3.6-flash`. Google retires model names (`gemini-2.5-flash` already
  returned 404 for a new key), so override with `PHOENIX_MODEL` if needed.
  Free tiers rate-limit; the provider retries 429/5xx honoring `Retry-After`, and `--workers 1`
  keeps the request rate low. Free calls are recorded at $0, so the dollar budget cap cannot
  stop a run; `max_steps` (default 20) and loop detection still do.
- **Anthropic (paid):** `PHOENIX_PROVIDER=anthropic` (the default) with `ANTHROPIC_API_KEY`.
  `eval --real` also refuses if 15 x `--budget` exceeds `--max-total-usd`.

The OpenAI-compatible provider ([openai_compat.py](src/phoenix/llm/openai_compat.py)) has been
run against the live Gemini endpoint, which surfaced four real problems, all fixed: the default
model name was retired (404 for new keys), Gemini 3 requires its opaque `thought_signature` to be
echoed back with every tool call, hidden "thinking" tokens were not in `completion_tokens`, and
the free tier allows only **20 requests per day per model** (an unretryable 429 that now fails
fast). It is also covered by unit tests against a fake HTTP transport.

## What was measured

All numbers are printed by scripts in [bench/](bench/) and saved in
[bench/results/](bench/results/). Environment: Windows 11, Docker Desktop, Python 3.13, local
Postgres 17, 3 workers, 2 s lease unless noted. These are small runs on one laptop: treat them
as "it works and here is the order of magnitude", not as performance claims.

**Chaos** (`bench/chaos_bench.py`): 5 seeds x 30 runs, mixed workload (about two thirds
mock-agent runs in real containers, the rest toy runs).

| | |
|---|---|
| Seeds passing all 4 invariants | 5 of 5 |
| Runs | 150 (all reached a terminal status) |
| Faults that hit a target | 62 (24 `kill -9` worker, 22 worker pause past the lease, 16 `docker kill` sandbox) |
| Runs that needed a lease takeover | 29 |
| Provider calls / repeated (run, step) calls | 721 / 0 |

Zero repeated calls means no crash-window retry *occurred* in these runs; it does not prove
the window cannot happen (see idea 6).

**Recovery** (`bench/recovery_bench.py`): kill -9 the lease holder mid-run, 8 trials, 2 s
lease. Gap between the dead worker's last committed step and the takeover worker's first:
min 2.27 s, median 2.30 s, max 2.40 s. So recovery costs about the lease length plus ~0.3 s.
All 8 runs finished with a clean log (20 events, no gaps or duplicates).

**Sandbox overhead** (`bench/sandbox_bench.py`): `python main.py` printing one line, 20 trials.
Wall time including create, start, collect output and remove: median 396 ms, p95 559 ms
(with 20 samples the p95 is effectively the max). Container start to exit: median 238 ms.

**Eval, mock provider** (`bench/eval_bench.py`): 13 of 15 tasks pass (easy 5/5, medium 5/5,
hard 3/5), average 1.87 code runs per task, 12,900 tokens, $0.0301 at mock pricing. Latency is
submit to finish and includes queueing behind 3 workers: mean 4.2 s, p50 3.7 s, max 7.8 s.
**This is not a model score.** Every task's "model" is a script that fails 0-2 times and then
writes the reference solution, except two deliberate negative controls (tasks 12 and 14)
whose scripted answer is plausible but wrong, to show the hidden tests can fail. The real
pass rate needs `--real`; see the next paragraph.

**Eval, real model** (`python -m phoenix eval --real --workers 1`, Gemini free tier, model
`gemini-3.1-flash-lite`, one run, saved in `bench/results/eval-gemini.json`): **15 of 15 tasks
passed the hidden tests** (easy 5/5, medium 5/5, hard 5/5), 1.13 code runs per task on average,
32,719 tokens including hidden thinking tokens, $0 on the free tier. Read it with care:

- **It is at the ceiling, so it cannot rank models.** These are classic short interview-style
  problems a current model has almost certainly seen; a 100% here says the pipeline works with a
  real model, not that the model is strong. Harder or novel tasks would be needed to separate models.
- **One run, one model, no repeats**, so no variance estimate. The hidden tests are my own small
  test sets, not exhaustive.
- **Latency is not a model or system speed.** The mean of 273 s and max of 495 s are dominated by
  my client-side pacing (13 s between requests to stay under the free tier's per-minute limit)
  and by tasks queueing behind a single worker.
- Cost is $0 only because the free tier was used; the dollar budget cap cannot bind at $0, so
  `max_steps` and loop detection are the effective limits.
- A first attempt on `gemini-3.6-flash` produced no result: its 20-per-day free quota was
  exhausted by earlier smoke tests. The eval now reports `INCOMPLETE` and saves nothing when
  runs hit a timeout or a provider error, so that failure cannot masquerade as a 0% pass rate.

**Eval, real model, challenge suite** (`python bench/eval_bench.py --real --workers 1 --suite
challenge --timeout 3000`, same model, saved in `bench/results/eval-gemini-challenge.json`):
**10 of 10 passed**, 1.6 code runs per task, 73,303 tokens, $0. This is a second, harder suite of 10
novel, precisely specified tasks (C01-C10). Its hidden tests are checked against 20 deliberate
bugs (2 mutants per task, all caught; see [test_challenge_tasks.py](tests/unit/test_challenge_tasks.py)),
and a hygiene test ensures a hidden test never depends on a name the prompt does not mention.
What to take from it, and what not to:

- **Still 100% for one model, so it still does not rank models.** It does make the model work
  harder: 1.6 runs per task versus 1.13 on the standard suite.
- **Effort varied a lot between runs of the same suite, and I ran it three times.** Run 1 scored
  9/10, but the single failure was a bug in my C09 hidden test (it imported a constant the prompt
  never mentions); the model's recorded answer passes the fixed test. Run 2 stopped as
  `INCOMPLETE`: nine tasks passed and C10 was still working (18 steps, 65k tokens) when the runner's
  fixed 15-minute wait expired, which is why `--timeout` now exists. Run 3, above, is the only
  complete result. Token use for the semver task alone was 28k, 45k and 4.5k across the three
  runs. One run per configuration is not a stable estimate.
- Same caveats as before: my own small test sets, one model, and latency dominated by request
  pacing rather than by the system.

**Tests:** 182 tests (unit, integration, security, chaos). 180 run in CI; 2 timing-dependent
end-to-end chaos tests are marked `slow`.

### Fencing is proven, not assumed

`PHOENIX_UNSAFE_NO_FENCING=1` (chaos demo only) removes the commit-time fence and the worker's
abort-on-lost-lease. With it, `python -m phoenix chaos --seed 1 --runs 30 --workload toy
--unsafe-no-fencing` FAILS invariant 2: a stale attempt writes events after a newer attempt,
or events appear after the final one. It failed on seeds 1, 2 and 3 when I tried them. A
deterministic in-process version (a zombie commit after takeover, across 5 seeds) is in
[tests/chaos/test_fencing_removal.py](tests/chaos/test_fencing_removal.py).

Note that the original wording of invariant 2 (no seq gaps, no duplicate keys) cannot detect a
zombie, because its writes are idempotent no-ops or extra events with valid seq numbers. The
invariant here also requires writer attempts never to go backwards and no event after the
final one.

### The chaos invariants

After each scenario (seeded plan, faults injected while K runs are in flight):

1. **Termination**: every run reaches a terminal status.
2. **Log integrity**: per run, seq is 1..n, step keys are contiguous, attempts never regress,
   nothing follows the final event.
3. **No double billing**: at most one provider call per (run, step); a repeat by a later
   attempt is listed as a crash-window retry, a repeat within one attempt fails.
4. **Spend consistency**: `runs.spent_usd` equals the sum of costs recorded in the run's events.

**Reproducibility:** one seeded RNG draws the whole plan up front (workload mix, fault times,
kinds, targets, pause lengths), so `--seed N` injects the same faults at the same offsets.
Process timing is the OS's, so a run is replayable, not bit-for-bit identical.

## Threat model

**Assets:** the host machine, the database, other runs' data, the API key, your money.

**Attacker:** code written by an LLM that may be wrong, adversarial, or steered by a
prompt-injected task. It can run arbitrary Python inside the sandbox.

| Threat | Mitigation (defaults are the most restrictive) | Test |
|---|---|---|
| Network exfiltration / calling home | `network_mode=none` | connect to IP and DNS both fail |
| Filesystem tampering | read-only root FS; only `/workspace` (bind mount) and a 16 MB `noexec` `/tmp` are writable | writes to `/etc`, `/usr`, `/`, `/root` denied; binaries in `/tmp` cannot run |
| Fork bomb | `pids_limit=64` | fork loop stops at the limit; nothing left behind |
| Memory exhaustion | 128 MB, swap = memory | OOM-killed, exit 137, `oom_killed=true` |
| CPU hog / infinite loop | 0.5 CPU, 10 s timeout then kill | killed at timeout |
| Output flood | streamed and capped at 64 KiB per stream, daemon log rotation bounds disk | truncated, `truncated=true` |
| Privilege escalation | uid 65534, all capabilities dropped, `no-new-privileges`, Docker's default seccomp profile | uid != 0, `CapEff == 0` |
| Path traversal by the agent's file tools | path allow-list + resolve check ([agent.py](src/phoenix/agent.py) `safe_join`), hypothesis property test | never escapes the workspace |
| Hidden eval tests leaking to the agent | tests are copied in only at grading time under a name the agent cannot write | asserted in the eval unit tests |
| A stale worker corrupting a run | fencing token in the commit transaction; per-attempt workspace directories | chaos + dedicated tests |

**What this does not protect against**, and you should know:

- **Docker shares the host kernel.** A kernel or runtime vulnerability is a container escape.
  For untrusted code from the internet use gVisor (user-space kernel) or Firecracker
  (microVM) instead. I did not implement either.
- **The worker can talk to the Docker daemon**, which is root-equivalent on the host. Anyone
  who compromises a worker process owns the machine.
- **No disk quota on `/workspace`.** Code can fill the workspace volume. Only `/tmp` and
  output are size-limited.
- No custom seccomp/AppArmor profile beyond Docker's defaults, and no protection against
  timing or resource side channels between containers.
- The `python:3.12-slim` image is pulled by tag, not pinned by digest.
- The HTTP API is unauthenticated unless `PHOENIX_API_KEY` is set (one shared key, constant-time
  compared), binds to 127.0.0.1 by default, and has no per-user isolation or rate limiting.
  Submissions are capped (per-run budget `PHOENIX_API_MAX_BUDGET_USD`, default $0.10; at most 50
  agent steps) and cannot carry a mock script, because an agent run can spend real LLM money.

## Limitations

- Postgres is a single point of failure; there is no replication or failover story.
- A tool that ran but whose step was not committed runs again after a crash. Sandbox tools only
  touch the (per-attempt) workspace, so this is harmless here, but external side effects would
  need their own idempotency.
- Workspaces live on the worker's local disk and are rebuilt from events on takeover; large
  binary files written by the agent are not supported well (they live in the event payload).
- The budget cap is checked between LLM calls, so a run can overshoot by up to one call.
- Loop detection is deliberately simple: three identical consecutive actions.
- A run that keeps crashing its worker is retried up to 5 times, then failed; there is no
  dead-letter queue or alerting.
- Python-only tasks. Eval tasks are 15 small function-writing problems.
- The chaos harness found **no bugs in the product code** during development. Its own bugs
  (docker faults that never hit a container) and one gap in the specified invariants (fencing
  removal was undetectable) were found and fixed.

## Future work

Web UI, Grafana/OpenTelemetry, Redis or another queue, multi-agent graphs, a CI regression gate
on eval pass-rate, gVisor/Firecracker isolation, workspace disk quotas, a fuller HTTP API (auth, rate limits, streaming),
image-digest pinning, a dead-letter queue, and evals across several models and repeated runs
to get a variance estimate.

## Repo map

| Path | What |
|---|---|
| [src/phoenix/store.py](src/phoenix/store.py) | event store: idempotent append, per-run seq, migrations |
| [src/phoenix/leases.py](src/phoenix/leases.py) | claim, heartbeat, reaper, fencing |
| [src/phoenix/engine.py](src/phoenix/engine.py) | atomic step commit, replay, worker loop, error handling |
| [src/phoenix/agent.py](src/phoenix/agent.py), [budget.py](src/phoenix/budget.py) | agent loop, tools, workspace rebuild, limits |
| [src/phoenix/llm/](src/phoenix/llm/) | provider interface, mock, Gemini/OpenAI-compatible, Anthropic (both gated), cache + call log |
| [src/phoenix/api.py](src/phoenix/api.py) | HTTP API: submit, status, events, timeline |
| [src/phoenix/sandbox/](src/phoenix/sandbox/) | Docker runner and policy |
| [src/phoenix/chaos/](src/phoenix/chaos/) | injector, invariants, scenarios |
| [src/phoenix/evals/](src/phoenix/evals/) | 15 standard + 10 challenge tasks with hidden tests, runner, report |
| [bench/](bench/) | scripts behind every number above, plus `demo.py` |
| [tests/](tests/) | unit, integration, security (needs Docker), chaos |

## 90-second demo

1. `make up`, then `python bench/demo.py`: starts 2 workers, submits 10 agent runs, `kill -9`s
   the lease holder, prints the timeline of a run that had a takeover and the count of
   repeated LLM calls.
2. `make chaos SEED=1 RUNS=30`: 4 invariants pass.
3. `python -m phoenix chaos --seed 1 --runs 30 --workload toy --unsafe-no-fencing`: fencing is
   removed, invariant 2 fails. Drop the flag to restore.

Example takeover timeline from `bench/demo.py` (a real run; note step 3 was served from the LLM
cache, so the takeover did not re-ask the model):

```
run 9905f95e-...  status=succeeded  spent=$0.002450 of $1.0000
task: status=done attempts=2

  +  0.72s  seq=1   agent_step       w1#1  $0.00035   write_file main.py
  +  1.18s  seq=2   agent_step       w1#1  $0.00035   run_code main.py -> exit 1 | ZeroDivisionError: division by zero
  -- takeover: w1#1 -> w2#2 (2.38s since the last committed step) --
  +  3.56s  seq=3   agent_step       w2#2  $0.000350  write_file main.py
  ...
  +  4.37s  seq=7   finish           w2#2  $0.00035   finish fixed after two failures
```
