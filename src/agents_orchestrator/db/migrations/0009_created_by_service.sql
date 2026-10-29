-- Who created a session: the name of the authenticated service token (ORCH_SERVICE_TOKENS) when a
-- service created it on the service port, NULL for a session created by a user on the main port
-- and for every session that existed before this column. The api is the only writer and sets it
-- once, at insert. The orchestrator reads it to keep the chat tools and the chat relay away from
-- sessions the trigger services (agents-webhook, agents-schedule) start from attacker-controlled
-- text. Additive and idempotent like 0006.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS created_by_service text;
