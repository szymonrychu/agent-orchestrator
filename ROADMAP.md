# agent-orchestrator roadmap

Human-owned requirements.

- Bench experiment: `orch bench spec.yaml` runs the arms `{replicas, peers}` (1 replica with peers off, 3 replicas with peers off, 3 replicas with peers on) through the public API. It answers whether round-robin dispatch, multiple agents and inter-agent messaging make agents faster, more robust and more correct. It records wall time, failed turns, tokens and verify pass rate as JSONL plus a summary table.
