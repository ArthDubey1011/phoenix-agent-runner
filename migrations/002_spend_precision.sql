-- Per-call LLM costs are fractions of a cent; NUMERIC(10,4) would round them and break the
-- "spent_usd == sum of recorded call costs" invariant.
ALTER TABLE runs ALTER COLUMN spent_usd TYPE NUMERIC(12,6);
