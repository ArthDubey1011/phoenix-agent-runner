-- Which lease attempt made the call. Chaos invariant #3 uses it to tell a legitimate
-- crash-window retry (same step, later attempt) from real double billing (same attempt twice).
ALTER TABLE provider_calls ADD COLUMN attempt INT NOT NULL DEFAULT 0;
ALTER TABLE provider_calls ADD COLUMN worker_id TEXT;
