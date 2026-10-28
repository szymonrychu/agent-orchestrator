-- Deploy-safe agent sessions (D3): when the reconciler last resumed the agent's recorded wrapper
-- session, by the database clock. The reconciler is the only writer: a resume sets it, a create
-- or adopt clears it. The ingester re-attaches when it changes and honours a stored
-- session_closed only when this is not later than the close's events.created_at. Additive and
-- idempotent like 0006.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS wrapper_resumed_at timestamptz;
