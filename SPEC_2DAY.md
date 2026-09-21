# PHOENIX (2-Day Edition): Crash-Proof Sandboxed Agent Runner

> Save this as `SPEC.md` in a new repo. Save section 9 as `CLAUDE.md`.
> Tell Claude Code: "Read SPEC.md and CLAUDE.md. Do Phase 1 only." Then continue one phase at a time using the prompts in section 8.

---

## 1. What you will have at the end (tangible deliverables)

1. A **Postgres-backed durable engine**: every agent step is saved as an event; a crashed run resumes from its last step.
2. **2+ worker processes** that claim work with leases and heartbeats. You can `kill -9` one and another finishes the job.
3. A **Docker sandbox** that runs agent-written code with no network, memory/CPU/PID limits, a timeout, and a read-only filesystem, backed by a security test suite.
4. An **agent loop**: write code -> run it in the sandbox -> read the error -> fix it (max attempts, budget cap, loop detection). Works with a fake LLM (free, for tests) and a real one (behind a flag).
5. A **chaos harness**: `make chaos SEED=1 RUNS=30` randomly kills workers/containers and pauses workers, then checks 4 invariants and prints a report.
6. A **15-task eval suite**: `make eval` prints pass-rate, attempts, tokens, cost, and latency.
7. A **README** with an architecture diagram, threat model, limitations, and your real measured numbers, plus a CLI timeline view: `python -m phoenix timeline <run_id>`.

**Explicitly NOT included (do not build in 2 days):** web UI, Grafana, OpenTelemetry, Redis, multi-agent graphs, CI regression gate. Mention them in the README under "Future work."

**Be honest in the README:** this is a *multi-worker* system coordinated through PostgreSQL, not a distributed-consensus system. Docker is not a perfect security boundary.

---

## 2. Architecture

```
  CLI / API (FastAPI: submit, status, timeline)
                     |
                     v
              PostgreSQL  <------ single source of truth
   runs | events (append-only) | tasks (leases) | llm_cache | provider_calls
                     ^
      claim (SKIP LOCKED) + commit (1 txn) + heartbeat
     +---------------+---------------+
  Worker 1        Worker 2        Reaper (loop inside each worker)
  (agent loop)    (agent loop)    requeues expired leases
     |
     v
  Docker sandbox per tool call (no network, limits, timeout)
```

Workers are **plain local processes** (so `kill -9` and `SIGSTOP` are easy). Only Postgres runs in docker-compose.

### The 6 ideas you must be able to explain
1. **Event sourcing:** run state = replay of its events.
2. **Idempotency key** `run_id:step_index` with a UNIQUE constraint: a step commits at most once even if two workers race.
3. **Atomic commit:** append event + complete task + update spend in ONE transaction.
4. **Lease + heartbeat:** a dead worker's lease expires, and another worker takes over.
5. **Fencing token (`attempt`):** a "zombie" worker that wakes up after losing its lease cannot commit.
6. **Honest crash window:** if a worker dies after the LLM answers but before the commit, one call may repeat. `llm_cache` shrinks this window. Document it.

---

## 3. Tech stack (keep it small)

Python 3.12, asyncio, FastAPI, asyncpg, PostgreSQL 16 (docker-compose), `docker` Python SDK, pytest + pytest-asyncio, hypothesis (one property test), ruff, GitHub Actions (tests only).

---

## 4. Repo layout

```
phoenix/
  SPEC.md  CLAUDE.md  README.md  Makefile  docker-compose.yml  .env.example
  migrations/001_init.sql
  src/phoenix/
    __main__.py          # CLI: submit, status, timeline, worker
    api.py
    store.py             # events, txn helpers
    leases.py            # claim, heartbeat, fencing, reaper
    engine.py            # durable step executor + replay
    budget.py            # cost cap, max steps, loop detection
    agent.py             # the loop + tools
    llm/{base,mock,anthropic,cache}.py
    sandbox/{runner,policy}.py
    chaos/{injector,invariants,scenarios}.py
    evals/{runner,report}.py  evals/tasks/01..15/
  tests/{unit,integration,security,chaos}/
  bench/                 # scripts that produce README numbers
```

---

## 5. Data model

```sql
CREATE TABLE runs (
  id UUID PRIMARY KEY, task_spec JSONB NOT NULL,
  status TEXT NOT NULL,                 -- pending|running|succeeded|failed|budget_exceeded
  budget_usd NUMERIC(10,4) NOT NULL, spent_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ);

CREATE TABLE events (
  run_id UUID NOT NULL REFERENCES runs(id), seq BIGINT NOT NULL,
  type TEXT NOT NULL, payload JSONB NOT NULL, idem_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq), UNIQUE (run_id, idem_key));

CREATE TABLE tasks (
  id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES runs(id),
  status TEXT NOT NULL,                 -- queued|leased|done
  lease_owner TEXT, lease_expires_at TIMESTAMPTZ,
  attempt INT NOT NULL DEFAULT 0,       -- fencing token
  available_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE llm_cache (
  key TEXT PRIMARY KEY, response JSONB NOT NULL,
  input_tokens INT, output_tokens INT, cost_usd NUMERIC(10,6));

-- every real/mock provider invocation, used by chaos invariant #3
CREATE TABLE provider_calls (
  id BIGSERIAL PRIMARY KEY, run_id UUID, step_index INT, cache_key TEXT,
  called_at TIMESTAMPTZ NOT NULL DEFAULT now());
```

---

## 6. Two-day schedule (hour budgets are guides, not promises)

### DAY 1: Make it durable and safe

| Block | Phase | Done when |
|---|---|---|
| 1 h | **P1 Scaffold + event store** | `make up` starts Postgres; migration runs; test: same step committed twice -> 1 event; 20 concurrent writers on one step -> exactly 1 event |
| 2-3 h | **P2 Workers, leases, recovery** | Start 2 workers, submit a 20-step toy run, `kill -9` the worker mid-run, the other finishes; no gaps/duplicates. SIGSTOP zombie test: paused worker resumes after lease expiry and its commit is REJECTED |
| 2-3 h | **P3 Sandbox** | Security tests pass: fork bomb contained, infinite loop killed at timeout, memory hog OOM-killed, network call fails, write outside workspace fails, huge stdout truncated. Returns `exit_code, stdout, stderr, timed_out, oom_killed, duration_ms` |

**Day 1 cutline:** if P3 runs long, ship the sandbox with timeout + no-network + memory + read-only FS and list the rest under "Future work."

### DAY 2: Make it smart, then break it

| Block | Phase | Done when |
|---|---|---|
| 2-3 h | **P4 Agent loop** | MockProvider scripted "fail twice, then fix" task succeeds. Budget cap and loop detection each have a passing test. Real provider works behind `PHOENIX_REAL_LLM=1`. Workspace files persist across resume |
| 2 h | **P5 Chaos harness** | `make chaos SEED=1 RUNS=30` passes 4 invariants and prints a report. Removing the fencing check makes it FAIL (keep as a test) |
| 1-2 h | **P6 Mini eval** | 15 tasks; `make eval` prints table + summary JSON |
| 1-2 h | **P7 Ship** | README with real numbers; CLI `timeline`; basic GitHub Action running tests; demo recorded/rehearsed |

**Day 2 cutline (in order of what to drop):** GitHub Action -> CLI timeline polish -> eval to 10 tasks -> chaos to 2 fault types. **Never drop:** fencing test, sandbox security tests, the "chaos catches removed fencing" proof.

**Reserve 60-90 minutes at the end** to read `engine.py`, `leases.py`, and `chaos/invariants.py` and rehearse the answers in section 10.

---

## 7. Chaos invariants (exactly these 4)

After each scenario (seeded RNG, faults injected at random times while K runs are in flight):
1. **Termination:** every run reaches a terminal status.
2. **Log integrity:** per run, `seq` has no gaps and no duplicate `idem_key`.
3. **No double billing:** for each `(run_id, step_index)` there is at most 1 row in `provider_calls`, except steps recorded as "crash-window retries" (log those explicitly and report the count).
4. **Spend consistency:** `runs.spent_usd` equals the sum of recorded call costs.

Fault types: `kill -9` a worker, `docker kill` a sandbox container, `SIGSTOP`/`SIGCONT` a worker past its lease.
Fencing is verified by its own dedicated test (Phase 2) and by the "remove fencing -> chaos fails" test.

---

## 8. Prompts to paste into Claude Code (in order)

**P1:** "Read SPEC.md and CLAUDE.md. Implement Phase 1: repo scaffold, docker-compose for Postgres, migration, event store with idempotent append and per-run seq. Write the concurrency tests first, show them failing, then make them pass. Keep output short."

**P2:** "Implement Phase 2: task claiming with FOR UPDATE SKIP LOCKED, leases, heartbeats, reaper, fencing via attempt. Add a toy 20-step workflow, a `make worker` target, and tests for kill -9 recovery and the SIGSTOP zombie case. Report any test you could not make pass."

**P3:** "Implement Phase 3: DockerRunner with the limits in SPEC.md and the full security test suite. Use a slim Python image. Tell me which tests need the Docker daemon."

**P4:** "Implement Phase 4: agent loop (write_file, run_code, finish), MockProvider with scripted scenarios, AnthropicProvider behind PHOENIX_REAL_LLM=1, llm_cache, provider_calls logging, budget cap, loop detection, workspace persistence across resume. Tests must use MockProvider only."

**P5:** "Implement Phase 5: chaos injector, scenario runner with seeded RNG and --seed, the 4 invariants, a printed report. Then temporarily remove the fencing check, show the harness failing, restore it, and keep that as a test."

**P6:** "Implement Phase 6: 15 eval tasks with hidden tests (mix of easy/medium/hard), a runner that submits them through the real platform, and a report with pass-rate, attempts, tokens, cost, latency."

**P7:** "Implement Phase 7: CLI timeline, bench scripts that print the numbers used in the README, README with architecture diagram, threat model, limitations, and future work, and a GitHub Action that runs unit tests. Do not invent any numbers: only use output from bench scripts."

**Tip to save usage:** `/clear` between phases; paste only failing test output; don't ask Claude to re-print large files.

---

## 9. CLAUDE.md (save as a separate file)

```markdown
# Rules for Phoenix
- Read SPEC.md. Work one phase at a time. After each phase: run tests, summarize in <=10 lines, list deviations from SPEC.md.
- Tests never call a real LLM. Use MockProvider. Real calls only when PHOENIX_REAL_LLM=1.
- All state changes go through the event store. No in-memory-only state that a crash would lose.
- A step commit is ONE transaction: append event + complete task + update spend.
- Write the failing test first for anything concurrent. Use seeded randomness so failures reproduce.
- Sandbox defaults are the most restrictive; loosening needs a comment explaining why.
- Secrets come from env; provide .env.example. Never commit keys.
- Typed Python, ruff clean. No TODOs left at the end of a phase.
- Never invent benchmark numbers. Numbers in docs come from scripts in /bench.
- Keep responses short; do not re-print unchanged files.
```

---

## 10. Interview questions you must be able to answer

- Why `SKIP LOCKED` instead of a distributed lock?
- What exactly does the fencing token prevent? Walk through the zombie-worker scenario.
- What is the crash window for LLM calls and how does the cache shrink it?
- Why event sourcing instead of updating a `state` column?
- Why is Docker not a hard security boundary? What would you use for stronger isolation (gVisor, Firecracker)?
- How did you make chaos tests reproducible? (seeded RNG)
- What was the hardest bug the chaos harness found?

---

## 11. Cost note (read this)

Claude Pro / Claude Code does **not** include API credits. Running evals with a real model uses a separate API key billed per token. Tests use MockProvider and cost nothing. For the eval:
- Set a small `budget_usd` per run and a global cap in the eval runner.
- Run the real eval once at the end, not repeatedly.
- If you'd rather not pay at all, run evals with the mock provider only and skip the real pass-rate bullet.

---

## 12. Final resume entry (fill in ONLY measured numbers; delete bullets you did not finish)

**Phoenix: Crash-Proof Sandboxed Agent Execution Platform** | Python, FastAPI, PostgreSQL, Docker, asyncio

- Built an event-sourced execution engine for LLM coding agents with lease-based workers and fencing tokens; runs resume from the last checkpoint after worker crashes, with **[N]** duplicate LLM calls across **[M]** fault-injected runs.
- Designed a Docker sandbox for untrusted generated code (no network, CPU/memory/PID caps, read-only FS, timeouts) with a security test suite covering fork bombs, OOM, infinite loops, and network escape attempts.
- Wrote a seeded chaos-testing harness that kills workers/containers and pauses workers while asserting 4 correctness invariants; it caught **[K]** real concurrency bugs during development.
- Created a 15-task eval suite reporting pass-rate, token cost, and latency; achieved **[X]%** pass-rate at **[Y]** average attempts per task.

Where each number comes from: `make chaos` report (N, M), your written bug list (K), `make eval` report (X, Y).

---

## 13. 90-second demo script

1. `make up`, start 2 workers, submit 10 runs.
2. `kill -9` one worker; show the lease expire and the other worker take over (`timeline` output).
3. Show the run finished with no repeated LLM calls.
4. Run `make chaos SEED=1 RUNS=30`; show 4 invariants passing.
5. Remove the fencing check on camera; show chaos failing; restore it.
