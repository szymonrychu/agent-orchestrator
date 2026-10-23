# agent-orchestrator memory

Lessons that outlive a session. Newest first.

- Single-writer reading of "no shared state" (D2): the tatara rule "services talk only through the queue or HTTP APIs, no shared state" conflicts with a Postgres-only queue, so it means one writer per table. Queue tables (messages, commands, checks) are the only multi-writer tables, changed only through `WHERE status = '<expected>'` UPDATEs. Table writers are in README.md `## Architecture`.
- One session per pod: a pod is exactly one claude-code-wrapper session, and a single pod is a cluster of 1. Peer messaging, round-robin and context gauges are per pod, never per multi-session pod.
- Events are stored, not proxied (D5): the ingester keeps one SSE subscription per agent and stores every event (at-least-once delivery, exactly-once storage via UNIQUE(agent_id, session_id, seq)). Browsers and API clients read the stored rows, so history survives wrapper restarts, resume works from a global event id, and the api never holds wrapper tokens.
- openapi.json is generated: after any route or schema change run `uv run python -m agent_orchestrator.api.openapi > openapi.json`; tests/test_readme_api.py fails on drift and on undocumented routes.
