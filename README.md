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

## Configuration

Every process reads `ORCH_*` variables (the contract `agent-orchestrator-settings-env`, set by the Helm chart). Invalid or missing values raise one error naming every bad variable.

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `ORCH_DATABASE_URL` | required | all | Postgres DSN (required) |
| `ORCH_MASTER_KEY` | unset | api, reconciler | secret key, at least 32 bytes after stripping; only for commands that call require_master_key() |
| `ORCH_MASTER_KEY_FILE` | empty | api, reconciler | file holding the master key; wins over ORCH_MASTER_KEY |
| `ORCH_NAMESPACE` | `agents` | api, reconciler, dispatcher | the one namespace of the orchestrator and every agent cluster |
| `ORCH_AGENT_IMAGE` | empty | api, reconciler | claude-code-wrapper image the agent pods run (required to create clusters) |
| `ORCH_CHECK_IMAGE` | empty | dispatcher | image for check jobs; defaults to agent_image |
| `ORCH_DEFAULT_MODEL` | empty | api | default model for new sessions |
| `ORCH_DEFAULT_EFFORT` | empty | api | default effort |
| `ORCH_MODELS` | empty | api | comma-separated model choices |
| `ORCH_DEFAULT_REPLICAS` | `1` | api | default agents per cluster |
| `ORCH_DEFAULT_AUTO_COMPACT_PCT` | `95` | api | auto-compact threshold percent (gauge tick and CLAUDE_AUTOCOMPACT_PCT_OVERRIDE) |
| `ORCH_PVC_STORAGE_CLASS` | `rook-ceph-rwx` | api, reconciler | RWX storage class |
| `ORCH_PVC_SIZE` | `20Gi` | api, reconciler | workspace PVC size |
| `ORCH_CLAUDE_SECRET_NAME` | `agent-orchestrator-claude` | api, reconciler | Secret holding Claude credentials |
| `ORCH_IMAGE_PULL_SECRET` | `regcred` | api, reconciler | imagePullSecret name; empty means none |
| `ORCH_ANY_MCP_CONFIG_FILE` | empty | api, reconciler | path to default any-mcp config; empty means {} |
| `ORCH_WRAPPER_PORT` | `8080` | dispatcher, ingester | wrapper HTTP port |
| `ORCH_CLUSTER_DOMAIN` | `cluster.local` | dispatcher, ingester, reconciler | cluster DNS domain |
| `ORCH_AGENT_TERMINATION_GRACE_SECONDS` | `90` | api, reconciler | agent pod termination grace |
| `ORCH_HTTP_HOST` | `0.0.0.0` | api | bind host |
| `ORCH_HTTP_PORT` | `8000` | api | bind port |
| `ORCH_STATIC_DIR` | empty | api | built SPA directory; empty means not mounted |
| `ORCH_DISPATCH_POLL_SECONDS` | `2` | dispatcher | dispatcher polling fallback |
| `ORCH_RECONCILE_POLL_SECONDS` | `10` | reconciler | reconciler poll interval |
| `ORCH_INGEST_REFRESH_SECONDS` | `5` | ingester | ingester stream refresh interval |
| `ORCH_SHUTDOWN_GRACE_SECONDS` | `20` | all | graceful shutdown wait |
| `ORCH_LOG_LEVEL` | `INFO` | all | log level |
| `ORCH_ALLOWED_CIDRS` | `192.168.1.0/24` | api | comma-separated direct-peer source allowlist (health routes exempt) |
| `ORCH_AGENT_IMAGES` | empty | api | comma-separated extra ClusterSpec.image allowlist (agent_image is always allowed) |
| `ORCH_MAX_REPLICAS` | `16` | api | cap on replicas per cluster |
| `ORCH_MAX_PVC_SIZE` | `100Gi` | api | cap on PVC size |
| `ORCH_MAX_CLUSTERS` | `20` | api | cap on cluster rows |
| `ORCH_WRAPPER_SEND_TIMEOUT_SECONDS` | `15` | dispatcher, ingester | wrapper send timeout; at most half of dispatching_stale_seconds |
| `ORCH_DISPATCHING_STALE_SECONDS` | `60` | dispatcher, ingester | dispatching row stale cutoff |
| `ORCH_DISPATCHING_HEARTBEAT_SECONDS` | `5` | dispatcher, ingester | dispatching row heartbeat |
| `ORCH_LOCK_HEARTBEAT_SECONDS` | `5` | all | singleton lock-held heartbeat |
| `ORCH_LOCK_TCP_USER_TIMEOUT_SECONDS` | `15` | all | tcp_user_timeout on the lock connection |
| `ORCH_RETRY_BACKOFF_BASE_SECONDS` | `2` | dispatcher, ingester | retry backoff base (> 0) |
| `ORCH_RETRY_BACKOFF_MAX_SECONDS` | `60` | dispatcher, ingester | retry backoff cap (>= base) |
| `ORCH_AMBIGUOUS_SETTLE_SECONDS` | `60` | dispatcher, ingester | settle window before retrying an ambiguous send (>= 2x send timeout) |
| `ORCH_AMBIGUOUS_DEADLINE_SECONDS` | `600` | dispatcher, ingester | deadline after which an ambiguous send fails (> settle) |
| `ORCH_DISCARD_HOOK_COMMAND` | empty | api, reconciler | pre-discard hook command (exclusive with URL) |
| `ORCH_DISCARD_HOOK_URL` | empty | api, reconciler | pre-discard hook http(s) webhook URL |
| `ORCH_DISCARD_HOOK_TIMEOUT_SECONDS` | `120` | api, reconciler | discard hook timeout |
| `ORCH_ANY_MCP_REPO` | `https://github.com/szymonrychu/any-mcp` | api, reconciler | any-mcp git repository |
| `ORCH_ANY_MCP_REF` | `v0.1.0` | api, reconciler | any-mcp pinned release tag vX.Y.Z (never a branch) |
| `ORCH_ON_START_TIMEOUT_SECONDS` | `600` | api, reconciler | onStart hook timeout |
