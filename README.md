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

## Helm chart

The chart lives in `chart/agent-orchestrator` and is published to `oci://harbor.szymonrichert.pl/charts`. It installs into the one namespace the orchestrator manages; agent pods run in the release namespace.

```sh
helm install agent-orchestrator oci://harbor.szymonrichert.pl/charts/agent-orchestrator \
  --namespace agents --create-namespace \
  --set agentImage=harbor.szymonrichert.pl/containers/claude-code-wrapper:1.0.0 \
  --set postgres.url=postgresql://orch:secret@postgres:5432/orch \
  --set masterKey.value="$(openssl rand -hex 32)" \
  --set claude.token="$CLAUDE_CODE_OAUTH_TOKEN"
```

### Postgres

A Postgres connection is required. Set exactly one of these; none, or more than one, fails the render:

1. `postgres.url`: a full URI, stored in the chart Secret `<fullname>-database` (key `uri`).
2. `postgres.host`, `user`, `database` (with `password`, `port` default 5432, `sslmode`): composed into `postgresql://<user>:<password>@<host>:<port>/<database>[?sslmode=]` with user and password URL-escaped, stored in `<fullname>-database`.
3. `postgres.existingSecret` (with `existingSecretKey`, default `uri`): the Secret is referenced directly and no database Secret is created.
4. `cnpg.enabled=true`: the optional CloudNativePG feature. The chart renders a `postgresql.cnpg.io/v1` Cluster `<fullname>-db` (`instances`, `imageName`, `database`, `owner`, `storage`, `walStorage`, `resources`) and reads the connection URI from the operator's `<fullname>-db-app` Secret, key `uri`. A non-empty `cnpg.backup` is copied verbatim into `spec.backup`. The CNPG operator must already be installed; it is off by default.

### Secrets

| Secret | Key | Created when | Override |
|---|---|---|---|
| `<fullname>-database` | `uri` | `postgres.url` or the host parts are set | `postgres.existingSecret` |
| `<fullname>-master-key` | `master-key` | `masterKey.value` is set | `masterKey.existingSecret` + `existingSecretKey` |
| `<fullname>-claude` | `CLAUDE_CODE_OAUTH_TOKEN` | `claude.token` is set | `claude.existingSecret` + `existingSecretKey` |

The master key and the Claude token each need a value or an `existingSecret`; with neither the render fails. The agent pods in the release namespace read the Claude Secret, so `existingSecret` must live there.

### Settings ConfigMap

`<fullname>-settings` carries `ORCH_NAMESPACE` (the release namespace), `ORCH_AGENT_IMAGE` (`agentImage`, required) and `ORCH_CLAUDE_SECRET_NAME` (`claude.existingSecret` or `<fullname>-claude`). Every entry of `env` is added verbatim; each key must start with `ORCH_` or the render fails naming it. No secret value is ever written to the ConfigMap; the database URL and master key reach the containers through `secretKeyRef`.

### RBAC

`rbac.create` (default `true`) renders a namespaced Role `<fullname>-reconciler` in the release namespace and a RoleBinding to the ServiceAccount `<fullname>-reconciler` only. No ClusterRole or ClusterRoleBinding is ever rendered. The Role grants exactly:

| Resource | Verbs |
|---|---|
| `apps` statefulsets; services, configmaps, persistentvolumeclaims | get, list, watch, create, patch, delete |
| secrets | create, patch, delete (never get, list or watch) |
| pods | get, list, watch, delete |
| `batch` jobs | get, list, watch, create, delete |
| pods/log | get |

There is no `pods/exec` and no wildcard. With `rbac.create=false` neither the Role nor the RoleBinding renders.

### Ingress

`ingress.enabled` is `false` by default. When enabled, `ingress.className` (empty by default), `ingress.annotations` (`{}` by default), `ingress.hosts[].paths[]` and `ingress.tls` come from values and route to the api Service port. The chart ships no homelab defaults; set them in your own values.

Example only, a LAN-restricted ingress:

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/whitelist-source-range: "192.168.0.0/16"
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: orchestrator.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: orchestrator-tls
      hosts:
        - orchestrator.example.com
```
## Architecture

One repo and one image; the entrypoint is `orch <cmd>`. The processes coordinate only through Postgres.

| Process | Command | Role |
|---|---|---|
| api | `orch api` | FastAPI REST and SSE, serves the SPA; never calls wrappers or Kubernetes |
| dispatcher | `orch dispatcher` | claims messages and commands, calls wrapper turns, interrupt and compact, verifies ambiguous sends |
| ingester | `orch ingester` | one SSE subscription per agent; writes events, agent status and turns |
| reconciler | `orch reconciler` | renders and applies Kubernetes objects, observes pods, bootstraps sessions, runs checks, executes restart, runs the pre-discard hook |
| migrate | `orch migrate` | initContainer, applies SQL migrations |
| bench | `orch bench` | experiment CLI, talks only to the public API, not deployed |

### Single-writer interpretation of "no shared state"

The rule "services talk only through the queue or HTTP APIs, with no shared state" is read as single-writer: one writer per table. The queue tables (`messages`, `commands`, `checks`) are the only multi-writer tables, and only through state-machine-guarded UPDATEs (`WHERE status = '<expected>'`). Every other table has exactly one writing process:

| Table | Writer |
|---|---|
| `clusters` | api; the reconciler writes only `status` and `observed_generation` and deletes the row when a discard completes |
| `agents` | reconciler; `last_dispatched_at` is written by the dispatch claim |
| `agent_status` | ingester |
| `messages` | api inserts and cancels while queued; the dispatcher does the rest |
| `turns` | ingester |
| `events` | ingester |
| `commands` | api inserts; the dispatcher claims interrupt and compact and inserts `rebootstrap`; the reconciler claims restart and rebootstrap |
| `checks` | api inserts; the reconciler does the rest |

### NOTIFY channels and polling fallbacks

NOTIFY payloads are wake-up hints that carry ids only. Every loop also polls, because a NOTIFY is lost while a listener is disconnected.

| Channel | Payload | Sent by | Heard by |
|---|---|---|---|
| `cluster_changed` | `{cluster_id}` | api (clusters repository) | reconciler, status stream |
| `agent_free` | `{cluster_id, agent_id}` | ingester | dispatcher |
| `events` | `{cluster_id, max_id}` | ingester | events stream |
| `status` | `{cluster_id}` | ingester, dispatcher | status stream |

Polling fallbacks: dispatcher `ORCH_DISPATCH_POLL_SECONDS` (2 s), reconciler `ORCH_RECONCILE_POLL_SECONDS` (10 s), ingester stream refresh `ORCH_INGEST_REFRESH_SECONDS` (5 s); the api SSE streams re-read on a 5 s poll when no NOTIFY arrives.

## Data model

Plain SQL migrations (`src/agent_orchestrator/db/migrations`), psycopg 3, no ORM. Events live as long as their cluster and are deleted by cascade when a discard deletes the cluster row.

| Table | Holds |
|---|---|
| `clusters` | id, unique slug, `spec` (jsonb ClusterSpec), `generation` / `observed_generation` (the reconcile queue), `desired` (`active`, `deleting`), `status` |
| `agents` | one row per pod: ordinal, pod name, url, readiness, session id, `last_dispatched_at`, `gone_at` |
| `agent_status` | wrapper state (`unknown`, `booting`, `idle`, `busy`, `dead`), `free_since`, current turn, context tokens and percentage, last seq, peers |
| `messages` | queue: text, status (`queued`, `dispatching`, `verifying`, `sent`, `failed`, `cancelled`), target and chosen agent, turn id, attempts, error, `not_before`, `ambiguous_at` |
| `turns` | one row per wrapper turn: origin, start, completion, status, final text, usage |
| `events` | every stored wrapper event; UNIQUE(agent_id, session_id, seq); the global bigserial id is the SSE id and the page cursor |
| `commands` | queue: `interrupt`, `compact`, `restart`, `rebootstrap` with status and result |
| `checks` | queue: a shell command run as a Kubernetes Job; exit code and log tail |

## API

Base path `/api/v1`. JSON in and out. There is no authentication; access is restricted by the source allowlist below. Errors always have the shape `{"error": {"code": "<code>", "message": "<text>"}}`. The machine-readable contract is `openapi.json` at the repo root (`uv run python -m agent_orchestrator.api.openapi > openapi.json`), also served at `/api/v1/openapi.json`; a test fails when it drifts.

Versioning: `/api/v1` is additive only. New routes, new optional request fields and new response fields may appear; existing routes, fields and error codes are never removed or changed in meaning. A breaking change is a new `/api/v2`.

Source allowlist: the api checks the direct socket peer against `ORCH_ALLOWED_CIDRS` (default `192.168.1.0/24`). `Forwarded`, `X-Forwarded-For` and `X-Real-IP` are never read. A caller outside the list gets `403 forbidden`. `/healthz` and `/readyz` are exempt.

Error codes: `bad_request` (400), `forbidden` (403), `not_found` (404), `conflict` (409, slug exists), `cluster_deleting` (409), `cluster_limit` (409, `ORCH_MAX_CLUSTERS` reached), `not_queued` (409), `not_failed` (409), `agent_gone` (409), `invalid_request` (422), `target_gone` (422), `internal` (500, 503 on `/readyz`).

### `GET /api/v1/meta`

Defaults and limits for forms: `defaults` (model, effort, replicas, auto_compact_pct, pvc_size, storage_class, max_attempts), `agent_image`, `agent_images` (the image allowlist), `models`, `caps` (`max_replicas`, `max_pvc_size`, `max_clusters`, from `ORCH_MAX_REPLICAS`, `ORCH_MAX_PVC_SIZE`, `ORCH_MAX_CLUSTERS`) and `discard_hook` (`none`, `command`, `url`). Response 200.

### `GET /api/v1/clusters`

All clusters as a list of cluster views. Response 200.

### `POST /api/v1/clusters`

Body `{"slug": "<dns label>", "spec": {...}}`. `spec` fields (all optional): `replicas`, `image`, `model`, `effort`, `peers`, `workdir`, `pvc_size`, `resources`, `env`, `any_mcp_config`, `max_attempts`, `auto_compact_pct`, `discard_hook_url`. Response 201 with the cluster view. Errors: 422 `invalid_request` (bad slug, image outside `ORCH_AGENT_IMAGES`, reserved env key, replicas or PVC size above the caps, invalid `any_mcp_config`), 409 `conflict` (slug exists), 409 `cluster_limit`, 403 `forbidden`.

### `GET /api/v1/clusters/{cluster_id}`

One cluster view: `id`, `slug`, `spec`, `desired`, `status`, `generation`, `observed_generation`, `created_at` and `summary` (`state` of `working`, `waiting`, `booting`, `error`, `deleting`; `ready`, `replicas`, `context_pct_max`, `auto_compact_pct`, `queued`, `failed`). Response 200. Errors: 404 `not_found`.

### `PATCH /api/v1/clusters/{cluster_id}`

Body `{"spec": {...}}` replaces the spec and bumps `generation`. Response 200 with the cluster view. Errors: 404 `not_found`, 409 `cluster_deleting`, 422 `invalid_request`.

### `DELETE /api/v1/clusters/{cluster_id}`

Discards the cluster: after the optional history-export hook, deletes every Kubernetes object, the PVC and the chat history. Takes no query parameters. Response 202 with the cluster view (`desired: deleting`). Errors: 404 `not_found`, 422 `invalid_request` (any query parameter).

### `GET /api/v1/clusters/{cluster_id}/agents`

List of agent views: `id`, `ordinal`, `pod_name`, `ready`, `state`, `free_since`, `current_turn_id`, `context` (`used_tokens`, `window_tokens`, `used_percentage`, or null), `peers`, `gone`. Response 200. Errors: 404 `not_found`.

### `POST /api/v1/clusters/{cluster_id}/messages`

Body `{"text": "<non-empty>", "target_agent_id": "<uuid, optional>"}`. Queues a message for round-robin dispatch (or for one agent). Response 202 `{"message_id": <int>, "status": "queued"}`. Errors: 404 `not_found`, 409 `cluster_deleting`, 422 `invalid_request` (empty text, target not in this cluster), 422 `target_gone`.

### `GET /api/v1/clusters/{cluster_id}/messages`

Query `status` (comma-separated, from `queued`, `dispatching`, `verifying`, `sent`, `failed`, `cancelled`) and `limit` (1-500, default 100). Response 200: list of message views (`id`, `cluster_id`, `text`, `status`, `target_agent_id`, `agent_id`, `turn_id`, `attempts`, `max_attempts`, `next_attempt_at`, `error`, `created_at`, `dispatched_at`, `turn`). Errors: 404 `not_found`, 422 `invalid_request`.

### `GET /api/v1/messages/{message_id}`

One message view. Response 200. Errors: 404 `not_found`.

### `DELETE /api/v1/messages/{message_id}`

Cancels a queued message. Response 204. Errors: 404 `not_found`, 409 `not_queued`.

### `POST /api/v1/messages/{message_id}/retry`

Re-queues a failed message. Response 202 `{"message_id": <int>, "status": "queued"}`. Errors: 404 `not_found`, 409 `not_failed`.

### `POST /api/v1/agents/{agent_id}/interrupt`

Queues an interrupt of the agent's current turn. Response 202 `{"command_id": <int>}`. Errors: 404 `not_found`, 409 `agent_gone`.

### `POST /api/v1/agents/{agent_id}/compact`

Queues a context compaction. Response 202 `{"command_id": <int>}`. Errors: 404 `not_found`, 409 `agent_gone`.

### `POST /api/v1/agents/{agent_id}/restart`

Queues a pod restart. Response 202 `{"command_id": <int>}`. Errors: 404 `not_found`, 409 `agent_gone`.

### `GET /api/v1/commands/{command_id}`

Command view: `id`, `agent_id`, `kind`, `status`, `result`, `created_at`, `started_at`, `finished_at`. Response 200. Errors: 404 `not_found`.

### `POST /api/v1/clusters/{cluster_id}/checks`

Body `{"command": "<non-empty shell command>"}`. Runs the command in a Kubernetes Job against the cluster's workspace. Response 202 `{"check_id": <int>}`. Errors: 404 `not_found`, 409 `cluster_deleting`, 422 `invalid_request`.

### `GET /api/v1/checks/{check_id}`

Check view: `id`, `cluster_id`, `command`, `status`, `exit_code`, `log_tail`, `created_at`, `started_at`, `finished_at`. Response 200. Errors: 404 `not_found`.

### `GET /api/v1/clusters/{cluster_id}/events`

Pages stored events. Query `before_id` or `after_id` (mutually exclusive), `limit` (1-500, default 200), `agent_id`, `types` (repeatable or comma-separated). Response 200 `{"events": [...], "has_more_before": bool, "next_after_id": int|null}`; each event has `id`, `cluster_id`, `agent_id`, `session_id`, `seq`, `ts`, `type`, `turn_id`, `data`. Errors: 400 `bad_request` (both cursors), 404 `not_found`.

### `GET /api/v1/clusters/{cluster_id}/events/stream`

SSE stream of the cluster's stored events after a cursor: query `after_id` or the `Last-Event-ID` header; with neither, it starts at the current end. Each frame has `id` = the event id, `event` = the stored event type and `data` = the event JSON. Errors before the stream starts: 400 `bad_request`, 404 `not_found`.

### `GET /api/v1/stream`

SSE stream of live cluster, agent and message state. It opens with a `snapshot`, then sends only deltas. Response 200 `text/event-stream`.

### Events

The `/clusters/{cluster_id}/events/stream` frames are named by the stored wrapper event type (for example `session_state`, `user_message`, `assistant_text`, `tool_call`, `turn_completed`, plus the synthetic `gap` row written when a sequence hole is detected). The `/stream` status stream uses these fixed names:

| Event | Stream | Data |
|---|---|---|
| `snapshot` | `/stream` | `{"clusters": [{"cluster": <cluster view>, "agents": [<agent view>]}]}`, sent once on connect |
| `cluster_status` | `/stream` | `{"cluster": <cluster view>}` when a cluster changed |
| `agent_status` | `/stream` | `{"cluster_id", "agent": <agent view>}` when an agent changed |
| `message_status` | `/stream` | `{"cluster_id", "queued", "failed", "messages": [<changed message views>]}` |
| `cluster_removed` | `/stream` | `{"cluster_id"}` when a discard completed |
