# agent-orchestrator

agent-orchestrator is a web UI and headless `/api/v1` that define, run, message and discard clusters of claude-code-wrapper pods in the namespace `agents`. One Python repo and image runs four processes (`orch api`, `orch dispatcher`, `orch ingester`, `orch reconciler`) that coordinate only through a Postgres database. Messages to a cluster go round-robin to the agent free the longest; a React/MUI UI shows live state and context gauges, and the same API serves tatara and magic-trainer. The repo ships its own Helm chart.

## Development

Tasks run through mise:

- `mise run install` - sync dependencies
- `mise run lint` - ruff check and format check
- `mise run fmt` - format and autofix
- `mise run test` - unit tests
- `mise run test:integration` - tests that need a real wrapper image
- `mise run test:e2e` - kind end-to-end tests
