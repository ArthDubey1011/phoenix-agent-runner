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
