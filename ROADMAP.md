# agent-orchestrator roadmap

Human-owned requirements.

- Bench experiment: `orch bench spec.yaml` runs the arms `{replicas, peers}` (for example 1/off, 3/off, 3/on) through the public API to test whether round-robin, multiple agents and inter-agent messaging make agents faster, more robust and more correct. It records wall time, failed turns, tokens and verify pass rate as JSONL plus a summary table.
