# agent-orchestrator

agent-orchestrator is a web UI and headless `/api/v1` that define, run, message and discard clusters of claude-code-wrapper pods in the namespace `agents`. One Python repo and image runs four processes (`orch api`, `orch dispatcher`, `orch ingester`, `orch reconciler`) that coordinate only through a Postgres database. Messages to a cluster go round-robin to the agent free the longest; a React/MUI UI shows live state and context gauges, and the same API serves tatara and magic-trainer. The repo ships its own Helm chart.

## Development

Tasks run through mise:

- `mise run install` - sync dependencies
- `mise run lint` - ruff check and format check
- `mise run fmt` - format and autofix
- `mise run test` - unit tests
- `mise run test:integration` - tests that need a real wrapper image. When `ORCH_TEST_WRAPPER_IMAGE` is unset the task builds `e2e/agent-test.Dockerfile` as `agent-orchestrator-agent-test:e2e` (the tag `scripts/e2e.sh` uses), exports it, and runs the suite; set the variable to use another image
- `mise run test:e2e` - kind end-to-end tests

## Configuration

Every process reads `ORCH_*` variables (the contract `agent-orchestrator-settings-env`, set by the Helm chart). Invalid or missing values raise one error naming every bad variable.

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `ORCH_DATABASE_URL` | required | all | Postgres DSN (required) |
| `ORCH_MASTER_KEY` | unset | api (file and terminal proxies), reconciler | secret key, at least 32 bytes after stripping; only for commands that call require_master_key() |
| `ORCH_MASTER_KEY_FILE` | empty | api (file and terminal proxies), reconciler | file holding the master key; wins over ORCH_MASTER_KEY |
| `ORCH_AGENT_PORT` | `8001` | api | second listener (same server) serving only `/agent/v1/*`, `/healthz` and `/readyz` to agent pods; the source allowlist is skipped there |
| `ORCH_AGENT_API_URL` | unset | api, reconciler | URL agent pods use to reach the agent port (set by the chart to the agents Service) |
| `ORCH_NAMESPACE` | `agents` | api, reconciler, dispatcher | the one namespace of the orchestrator and every agent cluster |
| `ORCH_AGENT_IMAGE` | empty | api, reconciler | claude-code-wrapper image the agent pods run (required to create clusters) |
| `ORCH_CHECK_IMAGE` | empty | dispatcher | image for check jobs; defaults to agent_image |
| `ORCH_DEFAULT_MODEL` | first of `ORCH_MODELS` | api, reconciler | default model for new sessions (prefilled in the form) |
| `ORCH_DEFAULT_EFFORT` | empty | api, reconciler | default effort; empty means the Claude Code default |
| `ORCH_MODELS` | `claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,claude-fable-5-1` | api | comma-separated model choices; a spec naming another model is rejected |
| `ORCH_EFFORTS` | `low,medium,high,xhigh,max` | api | comma-separated effort choices; a spec naming another effort is rejected (empty effort is allowed) |
| `ORCH_DEFAULT_CPU_REQUEST` | `500m` | api, reconciler | agent container CPU request filled into the stored spec when a cluster is created and the key is empty (Kubernetes quantity) |
| `ORCH_DEFAULT_CPU_LIMIT` | `4` | api, reconciler | agent container CPU limit filled in when a cluster is created and the key is empty |
| `ORCH_DEFAULT_MEMORY_REQUEST` | `1Gi` | api, reconciler | agent container memory request filled in when a cluster is created and the key is empty |
| `ORCH_DEFAULT_MEMORY_LIMIT` | `8Gi` | api, reconciler | agent container memory limit filled in when a cluster is created and the key is empty |
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
| `ORCH_ALLOWED_ORIGINS` | empty | api | comma-separated browser origins (`scheme://host[:port]`, lower-cased, no path, no trailing slash) allowed to open a WebSocket or call the terminal POST and DELETE routes; empty refuses every request that carries an `Origin`. Dev example: `http://localhost:5173` |
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

## Commands

One image, one `orch` entrypoint. Every command reads `ORCH_*` settings (see Configuration), logs JSON lines on stdout at `ORCH_LOG_LEVEL`, and exits 0 on SIGTERM or SIGINT after finishing its current iteration and closing streams and the pool within `ORCH_SHUTDOWN_GRACE_SECONDS`.

| Command | Runs | Settings it needs | Replicas |
|---|---|---|---|
| `orch migrate` | applies pending SQL migrations and exits 0 (a Postgres advisory lock serialises concurrent runs) | `ORCH_DATABASE_URL` | any, one-shot (chart initContainer or hook Job) |
| `orch api` | uvicorn serving `/api/v1`, the UI and `/healthz`, `/readyz` on `ORCH_HTTP_HOST:ORCH_HTTP_PORT`; proxy headers are off so the direct peer is what `ORCH_ALLOWED_CIDRS` sees | `ORCH_DATABASE_URL`, `ORCH_ALLOWED_CIDRS`, `ORCH_STATIC_DIR` (optional) | any number; no Kubernetes client |
| `orch dispatcher` | the round-robin dispatcher loop | `ORCH_DATABASE_URL`, `ORCH_MASTER_KEY` or `ORCH_MASTER_KEY_FILE`, `ORCH_DISPATCH_POLL_SECONDS`, the wrapper and retry settings | any number (SKIP LOCKED queues) |
| `orch ingester` | stores every wrapper event, inside `advisory_singleton(dsn, "ingester")` | `ORCH_DATABASE_URL`, `ORCH_MASTER_KEY` or `ORCH_MASTER_KEY_FILE`, `ORCH_INGEST_REFRESH_SECONDS` | 1 active; extra replicas wait as hot standbys |
| `orch reconciler` | the Kubernetes reconciler with `LightkubeApi`, inside `advisory_singleton(dsn, "reconciler")` | `ORCH_DATABASE_URL`, `ORCH_MASTER_KEY` or `ORCH_MASTER_KEY_FILE`, `ORCH_AGENT_IMAGE`, `ORCH_NAMESPACE`, `ORCH_RECONCILE_POLL_SECONDS`, in-cluster credentials | 1 active; extra replicas wait as hot standbys |

### Singleton failover

`ingester` and `reconciler` each hold a session-level `pg_advisory_lock` (key: a stable 64-bit hash of the name) on a dedicated connection and block until they acquire it, so a second replica waits as a hot standby and takes over when the holder's session ends.

- The lock connection is opened with libpq keepalives (`keepalives=1`, `keepalives_idle`, `keepalives_interval`, `keepalives_count`, all about a third of `ORCH_LOCK_TCP_USER_TIMEOUT_SECONDS`) and `tcp_user_timeout` set to `ORCH_LOCK_TCP_USER_TIMEOUT_SECONDS` times 1000, so a half-open connection (a blackholed node) is detected by the kernel instead of hanging.
- Every `ORCH_LOCK_HEARTBEAT_SECONDS` a heartbeat checks on the lock connection that this backend still holds the advisory lock. A failed, timed-out or negative heartbeat, or a dropped connection, stops the loops and the process exits 3, so Kubernetes restarts it rather than letting it run without the lock. A holder behind a dead link therefore exits within `ORCH_LOCK_TCP_USER_TIMEOUT_SECONDS` plus one heartbeat.

## Images

CI (`.github/workflows/ci.yaml`, job `images`) builds the orchestrator image from the root `Dockerfile` on every push and pull request and smoke-tests it with `docker run --rm <image> --help`. Only a push to `main` logs in to Harbor (secrets `HARBOR_USERNAME`, `HARBOR_PASSWORD`) and pushes it with `regctl`.

- Orchestrator image: `harbor.szymonrichert.pl/containers/agent-orchestrator:<sha7>`, where `<sha7>` is the first seven characters of the commit sha. Tags are immutable; there is no `latest`.
- Agent image: no agent image is built here. Agent pods run the claude-code-wrapper image named by `ORCH_AGENT_IMAGE`; the recommended one is `harbor.szymonrichert.pl/containers/claude-code-wrapper:e9ffa62`. This is the value for the chart's `agentImage` and agents-helmfile, and the commit the dev dependency `claude-code-wrapper` in `pyproject.toml` is pinned to, so config validation tests the wrapper the agents run.
- any-mcp: it reaches agent pods through the rendered onStart hook, not through an image. The `any-mcp` dependency in `pyproject.toml` and `ORCH_ANY_MCP_REF` pin the same release tag (`v0.1.0`).

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

`ingress.denySourceRanges` and `ingress.allowSourceRanges` (both empty by default) render the `nginx.ingress.kubernetes.io/denylist-source-range` and `nginx.ingress.kubernetes.io/whitelist-source-range` annotations (comma-joined) only when non-empty. See [Security](#security) for what to put in them.

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

WebSocket routes (the terminal) need long idle timeouts at the ingress. ingress-nginx closes an idle proxied connection after 60 s by default, so raise `nginx.ingress.kubernetes.io/proxy-read-timeout` and `nginx.ingress.kubernetes.io/proxy-send-timeout` to `"3600"` (as agents-helmfile already does) through `ingress.annotations`. The UI pings every 20 s, well inside that window, so a quiet terminal stays open.

```yaml
ingress:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```
## Security

Agent pods reach `/agent/v1` on the agents Service only, with the cluster token and from their own pod IP. The agent port serves nothing else (404), and `/agent/v1` is 404 on the main port.

Every agent pod receives `ORCH_AGENT_TOKEN` (a per-cluster HMAC of the master key, purpose `agent`), `ORCH_CLUSTER_ID` and, when `ORCH_AGENT_API_URL` is set, `ORCH_AGENT_URL`, as plain env; they are also passed through to the any-mcp stdio child. The token is therefore readable by every process in the pod, Bash and subagents included. The main-agent-only tool gate (`main_agent_only_tools`) is a best-effort guard, not a security boundary, and nothing here claims isolation from subagents. Without `ORCH_AGENT_API_URL` the reconciler logs one warning per cluster and omits `ORCH_AGENT_URL`.

The api trusts only the direct socket peer (`ORCH_ALLOWED_CIDRS`, see API). Behind an ingress that peer is the ingress controller, but not its node LAN IP. ingress-nginx runs hostNetwork, yet on a flannel cluster its proxied connection reaches the api pod from the node's pod-network gateway address: from another node via `flannel.1` as `10.244.N.0`, from the same node via `cni0` as `10.244.N.1`. A LAN client therefore gets `403` if `ORCH_ALLOWED_CIDRS` lists only the LAN CIDR. Set `ORCH_ALLOWED_CIDRS` to each node's podCIDR `.0/31` (covers `.0` and `.1`; agent pods get `.2` and up, so they stay outside), derived from `kubectl get nodes -o jsonpath='{range .items[*]}{.spec.podCIDR}{"\n"}{end}'`, for example `10.244.0.0/31,10.244.1.0/31`. Update the list whenever nodes are added or removed. The allowlist is blind to who the original client was.

The boundary this opens: an agent pod can call the ingress hostname. flannel `ipMasq` SNATs pod-to-LAN traffic to the node IP, a hostNetwork ingress-nginx admits it under a LAN `whitelist-source-range`, and proxies it to the api from the node pod-network gateway address that the allowlist accepts. A pod is then indistinguishable from a LAN client.

Close it at the ingress: set `ingress.denySourceRanges` to the cluster node IPs (pod egress to the LAN is SNATed to node LAN IPs, so denying them denies pods) and `ingress.allowSourceRanges` to the LAN CIDR. A process running on a node itself is denied too. Example:

```yaml
ingress:
  denySourceRanges: ["192.168.1.10/32", "192.168.1.11/32"]  # cluster node IPs
  allowSourceRanges: ["192.168.1.0/24"]                      # the LAN
```

### Origin allowlist

Browsers apply no CORS to WebSocket upgrades, so any web page opened on a LAN machine could otherwise open a terminal shell against the api. The api therefore checks the `Origin` header of every WebSocket upgrade against `ORCH_ALLOWED_ORIGINS`, beside the source allowlist (which is unchanged):

- no `Origin` header (a non-browser client): allowed, the source allowlist still applies;
- `Origin: null` or an origin not in the list: the upgrade is closed with code 1008 before it is accepted;
- an empty list refuses every upgrade that carries an `Origin`, so the setting is mandatory for any deployment that serves the UI: list the UI origin exactly as the browser sends it (`scheme://host[:port]`), for example the ingress origin `https://orchestrator.example.com`.

The POST and DELETE terminal routes apply the same rule and answer `403 forbidden_origin`. For local UI development list the dev server: `ORCH_ALLOWED_ORIGINS=http://localhost:5173`. An entry with a path, a trailing slash, userinfo or a scheme other than http or https fails settings validation naming `ORCH_ALLOWED_ORIGINS`.

## Releasing

The chart is published to `oci://harbor.szymonrichert.pl/charts/agent-orchestrator` with immutable versions; Harbor rejects a repeated version and CI fails first with a clear message. `scripts/chart-version.sh` picks the version from `version` in `chart/agent-orchestrator/Chart.yaml`:

- Every push to `main` (after the `chart` and `images` jobs pass) publishes `<version>-main.<sha7>`, with `appVersion` set to `<sha7>`. agents-helmfile can pin that exact version.
- A tag `vX.Y.Z` publishes `X.Y.Z`. The tag must equal Chart.yaml's `version`, otherwise the job fails. The tagged commit must already have been built by main CI: the job checks that `harbor.szymonrichert.pl/containers/agent-orchestrator:<sha7>` exists before packaging.
- To release: bump `version` in Chart.yaml, merge to `main`, wait for CI, then `git tag vX.Y.Z` on that commit and push the tag.
- Publishing a version that already exists fails with `chart version <v> already published; bump chart/agent-orchestrator/Chart.yaml version`.
- The Harbor robot (`HARBOR_USERNAME`, `HARBOR_PASSWORD`) needs push on both the `containers` and `charts` projects.
## Kubernetes permissions

Only the reconciler talks to Kubernetes. Everything it touches lives in one namespace: the release namespace, `ORCH_NAMESPACE` (default `agents`), which it shares with the orchestrator's own Deployments, Service, settings ConfigMap, Secrets and, with the CNPG feature, the database. It needs a namespaced Role there (D9), rendered by the chart as `templates/role.yaml`:

| Resource | Verbs | Used for |
|---|---|---|
| `apps` statefulsets | get, list, watch, create, patch, delete | the agents of a cluster |
| services, configmaps, persistentvolumeclaims | get, list, watch, create, patch, delete | headless Service, ccw config and onStart script, the RWX workspace |
| secrets | create, patch, delete | the cluster Secret `agent-<slug>-ccw` |
| pods | get, list, watch, delete | observing agents, the restart command |
| `batch` jobs | get, list, watch, create, delete | checks |
| pods/log | get | check log tails |

Secrets are limited to create, patch and delete: there is no get, list or watch on secrets, so the reconciler can never read the master key, the Claude token, the database credentials or any other Secret in the namespace. The Role is bound only to the reconciler's ServiceAccount; the api, dispatcher and ingester run with `automountServiceAccountToken: false`. The Role never grants `pods/exec`, and there is no ClusterRole and no wildcard.

Applies are server-side applies with field manager `agent-orchestrator` and `force`, so the reconciler owns the fields it renders.

### One shared namespace, kept safe (D26)

Clusters are told apart by labels, not namespaces. Every object of cluster `<slug>` carries both `app.kubernetes.io/managed-by=agent-orchestrator` and `agents.szymonrichert.pl/cluster=<slug>`. These guards keep the reconciler off everything else in the namespace:

- Reserved names. Every cluster object is named with the prefix `agent-<slug>` (`agent-<slug>`, `agent-<slug>-workspace`, `agent-<slug>-ccw`, `agent-<slug>-config`). The slugs `orchestrator` and `orchestrator-*` are rejected, so no cluster name can be one of the chart's own `agent-orchestrator-*` names.
- Label guard on apply. Before applying a StatefulSet, Service, ConfigMap or PVC the reconciler reads it. When it exists without both labels it is foreign: nothing of that cluster is applied, and the cluster status records `phase: error` with `foreign_object <kind>/<name>` until the object is removed or renamed.
- Label guard on prune. Every list carries the cluster selector, and only objects carrying both labels are ever deleted. A restart deletes a pod only when it is listed under its cluster's selector.
- Secrets deleted by name. Secrets are never listed or read. The cluster Secret is applied with the other objects and, when the cluster is discarded, deleted by its deterministic name `agent-<slug>-ccw`; a Secret that is already gone is fine.

### Reconcile loop

The reconciler is level-triggered: every `ORCH_RECONCILE_POLL_SECONDS` (10 s), and at once on NOTIFY `cluster_changed`, it converges every cluster row:

1. Render the PVC, Secret, ConfigMap, headless Service and StatefulSet, check the label guard, apply them, and prune labelled objects that are no longer desired.
2. List the cluster's pods and upsert one `agents` row per ordinal, with the per-pod URL `http://<pod>.agent-<slug>.<namespace>.svc.<cluster domain>:<ORCH_WRAPPER_PORT>` and its readiness. An agent whose pod vanished with an ordinal at or above `replicas` is marked gone; one below `replicas` is kept, unready, while its pod is recreated.
3. Write `status {phase, ready, replicas, message}` (`phase` is `pending`, `ready`, `degraded`, `deleting` or `error`) and `observed_generation`, only when something changed.

A deleting cluster first passes the pre-discard gate, then loses every labelled object including its PVC, and its Secret by name. Once no labelled object or pod is left, its row is deleted, which cascades its agents, messages, events, turns, commands and checks. An error in one cluster is written into its status message and never stops the others. Restart commands are claimed in the same pass: the agent's pod is deleted and the StatefulSet recreates it.

## Discard

`DELETE /api/v1/clusters/{id}` marks the cluster `deleting`; the reconciler then removes everything, in this order:

1. **Quiesce** (only when a hook is configured and has not yet delivered): the StatefulSet is scaled to zero and the reconciler waits until no agent pod is left, then gives the ingester `ORCH_DISCARD_DRAIN_SECONDS` (5) to store the last events of their streams. The wait is bounded by `ORCH_DISCARD_QUIESCE_TIMEOUT_SECONDS` (120) from the start of the discard; after that the discard logs a warning and goes on.
2. **Export** the full history as JSONL.
3. **Hook**: deliver the export; a failed hook blocks the deletion and is retried with backoff.
4. **Delete** everything.

A discard deletes the cluster's Kubernetes objects (check Jobs, StatefulSet and its pods, headless Service, ConfigMap, the Secret by name and the RWX PVC with the workspace) and, last, the cluster row, which cascades its whole chat history: agents, messages, turns, commands, checks and events.

### Hook

An optional hook receives the full history export before anything is deleted. None is configured by default. The hook comes from, in order of precedence:

1. `spec.discard_hook_url` of the cluster (a webhook URL, http or https only);
2. `ORCH_DISCARD_HOOK_COMMAND` (a shell command) or `ORCH_DISCARD_HOOK_URL` (a webhook URL), set at most one.

A command comes only from settings, never from the spec: the API has no auth and the reconciler holds the Kubernetes credentials. Both kinds are bounded by `ORCH_DISCARD_HOOK_TIMEOUT_SECONDS` (120).

### Export format

JSONL, one JSON object per line, each with a `kind`. The first line is the `cluster` record (`id`, `slug`, `spec`, `created_at`). Then come the `agent`, `message`, `turn`, `command`, `check` and `event` records of the cluster, each kind in id order (turns in `started_at` order); a record carries the table's columns. A `command` record's own kind column is named `command_kind`. Timestamps are ISO 8601 UTC. The export is streamed from the database with server-side cursors, so a long history is never held in memory. The cluster is already `deleting` and the API refuses new messages, so, with the agents quiesced first, the history includes every event the agents produced.

### Command contract

Run through `sh -c` with the JSONL on stdin, `ORCH_CLUSTER_ID` and `ORCH_CLUSTER_SLUG` in its environment, in its own process group. Exit 0 is success. A non-zero exit is a failure whose reason carries the exit code and the tail of stderr (bounded). On timeout the whole process group is killed.

### Webhook contract

An HTTP `POST` of the JSONL, streamed, with `Content-Type: application/x-ndjson`, `X-Orch-Cluster-Id` and `X-Orch-Cluster-Slug`. Any 2xx is success; any other status, a connection error or a timeout is a failure.

### Delivery and retries

Delivery is at-least-once. Success writes `status.discard_hook_delivered` (database time), after which the export is never sent again, also not after a reconciler restart. A crash between the hook accepting and that write sends the export a second time, so a receiver must tolerate duplicates (the cluster `id` identifies them).

A failing hook holds the discard: nothing is deleted, the cluster stays `deleting`, and the status records `discard_hook_failed` (the reason), `discard_hook_failures` (the count) and `discard_hook_next_at`. The hook is retried on a later reconcile once the database clock passes `discard_hook_next_at`, with the backoff `ORCH_RETRY_BACKOFF_BASE_SECONDS` doubling up to `ORCH_RETRY_BACKOFF_MAX_SECONDS`. One cluster's failing hook never stops the other clusters reconciling.

## Architecture

One repo and one image; the entrypoint is `orch <cmd>`. The processes coordinate only through Postgres.

| Process | Command | Role |
|---|---|---|
| api | `orch api` | FastAPI REST and SSE, serves the SPA; calls wrappers only for the file and terminal proxies in `api/workspace.py` (derived per-cluster tokens, user decision N1), never Kubernetes |
| dispatcher | `orch dispatcher` | claims messages and commands, calls wrapper turns, interrupt and compact, verifies ambiguous sends |
| ingester | `orch ingester` | one SSE subscription per agent; writes events, agent status and turns |
| reconciler | `orch reconciler` | renders and applies Kubernetes objects, observes pods, bootstraps sessions, runs checks, executes restart, runs the pre-discard hook |
| migrate | `orch migrate` | initContainer, applies SQL migrations |
| bench | `orch bench` | experiment CLI, talks only to the public API, not deployed |

### Single-writer interpretation of "no shared state"

The rule "services talk only through the queue or HTTP APIs, with no shared state" is read as single-writer: one writer per table. The queue tables (`messages`, `commands`, `checks`, `leadership_requests`) are the only multi-writer tables, and only through state-machine-guarded UPDATEs (`WHERE status = '<expected>'`). Every other table has exactly one writing process:

| Table | Writer |
|---|---|
| `clusters` | api; the reconciler writes only `status` and `observed_generation` and deletes the row when a discard completes. `preset_id` is also set to null by the `ON DELETE SET NULL` cascade when the api deletes a preset, an api-originated write |
| `presets` | api |
| `agents` | reconciler; `last_dispatched_at` is written by the dispatch claim; `pod_ip` is written by the reconciler |
| `agent_status` | ingester |
| `messages` | api inserts and cancels while queued; the dispatcher does the rest |
| `turns` | ingester |
| `events` | ingester |
| `commands` | api inserts; the dispatcher claims interrupt and compact and inserts `rebootstrap`; the reconciler claims restart and rebootstrap |
| `checks` | api inserts; the reconciler does the rest |
| `leadership_requests` | api inserts; the dispatcher decides rows (`applied` or `rejected`) only through `WHERE status = 'queued'` guarded updates |
| `cluster_leaders` | dispatcher |
| `file_locks` | api |

### NOTIFY channels and polling fallbacks

NOTIFY payloads are wake-up hints that carry ids only. Every loop also polls, because a NOTIFY is lost while a listener is disconnected.

| Channel | Payload | Sent by | Heard by |
|---|---|---|---|
| `cluster_changed` | `{cluster_id}` | api (clusters repository); reconciler on status writes and purge | reconciler, status stream |
| `agent_free` | `{cluster_id, agent_id}` | ingester | dispatcher |
| `events` | `{cluster_id, max_id}` | ingester | events stream |
| `status` | `{cluster_id}` | ingester, dispatcher, reconciler | status stream |

Polling fallbacks: dispatcher `ORCH_DISPATCH_POLL_SECONDS` (2 s), reconciler `ORCH_RECONCILE_POLL_SECONDS` (10 s), ingester stream refresh `ORCH_INGEST_REFRESH_SECONDS` (5 s); the api SSE streams re-read on a 5 s poll when no NOTIFY arrives.

## Delivery and retries

The dispatcher claims the oldest queued message of a cluster, reserves the agent free the longest, and posts it as a wrapper turn carrying `metadata = {orch_message_id, orch_attempt}`. Every send ends in exactly one outcome (D23, D24):

| Outcome | Wrapper answer | Reservation | Message becomes |
|---|---|---|---|
| accepted | 202 with a `turn_id` | kept until a newer `free_since` | `sent` with that `turn_id` |
| busy | 409 | released | `queued` again at once; no attempt counted, no backoff |
| unavailable | 5xx, connect error, an agent with no url | released | `queued` with backoff and one attempt counted, or `failed` (`unavailable`) at max |
| gone | 404, 410 | released; a `rebootstrap` command is enqueued | `queued` with backoff and one attempt counted, or `failed` (`session_gone`) at max |
| config error | 401, 403 | released | `failed` (`config_error`) at once; a resend cannot succeed |
| rejected | 413, 422 and any other 4xx | released | `failed` (`rejected`) at once; a resend cannot succeed |
| ambiguous | read timeout, reset after the request was written, 2xx without a `turn_id` | kept | `verifying` (`ambiguous`); the verifier decides |

Backoff. A counted attempt sets `not_before = now() + least(ORCH_RETRY_BACKOFF_BASE_SECONDS * 2^(attempts - 1), ORCH_RETRY_BACKOFF_MAX_SECONDS)` (defaults 2 s and 60 s: 2, 4, 8, 16 s, and so on up to the 60 s cap). The bound is the cluster's `spec.max_attempts` (default 5, 1 to 10): the attempt that reaches it fails the message. An untargeted head waiting out its backoff holds its cluster's queue, which keeps per-cluster FIFO. A retry goes through the normal claim, so it may land on another agent. Every time comparison uses the database clock `now()`; wrapper timestamps are stored, never compared.

Slow sends. The send timeout `ORCH_WRAPPER_SEND_TIMEOUT_SECONDS` (15 s) is at most half of the stale cutoff `ORCH_DISPATCHING_STALE_SECONDS` (60 s). While a send is in flight the `dispatching` row heartbeats every `ORCH_DISPATCHING_HEARTBEAT_SECONDS` (5 s). A `dispatching` row whose `coalesce(heartbeat_at, dispatched_at)` is older than the cutoff belonged to a lost dispatcher: it moves to `verifying` (`dispatcher_lost`), never straight back to `queued`.

### The `verifying` state

A message in `verifying` has `ambiguous_at = now()` and keeps its agent reserved, because its turn may be running. Every dispatcher pass (each NOTIFY wake and each poll) runs the verifier (`dispatcher/verify.py`) over at most 50 such messages. For each one it asks the original agent's wrapper, with the cluster's derived token, whether a turn carrying this message started:

- It reads the session snapshot. When `current_turn_id` is set, it fetches that turn record. It then fetches the records of the newest 10 turns of the turns list (the list holds summaries only, so each record is read on its own).
- Started: a record whose `metadata.orch_message_id` is this message. The message becomes `sent` with that `turn_id` and the reservation stays. A peer or other turn running on the agent does not match, because the match is on the message id, never on "some turn is running".
- Definitely not started: the wrapper answered the snapshot and every turn lookup, no record carries the id, and the message has been `verifying` for at least the settle window. An attempt is counted (`ambiguous_not_started`) and the message is requeued with backoff, or fails at max. The reservation is cleared only while `agents.last_dispatched_at` still equals this message's `dispatched_at`, so a newer claim on the agent is never undone.
- Unknown: the wrapper is unreachable, the session answers 404 (or any other error), or the message is still inside the settle window. Nothing changes; the next pass checks again.
- Deadline: no definite answer within the deadline fails the message with `ambiguous`. Only the user's explicit Retry (`POST /api/v1/messages/{message_id}/retry`) sends it again, with a fresh attempt budget.

Each decision is one guarded queue update (`WHERE status = 'verifying'`), so two dispatcher replicas verifying the same message resolve it once.

### Settle window and deadline

| Setting | Default | Rule |
|---|---|---|
| `ORCH_AMBIGUOUS_SETTLE_SECONDS` | `60` | at least twice the send timeout; no not-started decision before it |
| `ORCH_AMBIGUOUS_DEADLINE_SECONDS` | `600` | greater than the settle window; no definite answer by then fails the message as `ambiguous` |

Both are compared only with the message's age on the database clock, `now() - ambiguous_at`.

### Never twice

The never twice rule: an automatic retry never pastes a message into a session twice. A message whose send may have reached the wrapper is re-sent automatically only after the original wrapper answered, showed no turn carrying its `orch_message_id`, and the settle window passed. The wrapper records a turn, with its metadata, synchronously before it answers 202, so a missing record after the settle window means the request never reached the wrapper's router.

The residual case: a turn request the wrapper holds for longer than the settle window before recording it (a stalled wrapper), accepted after the verifier already saw no turn. Then the message can run twice. The settle window is four times the default send timeout to keep this to a stalled wrapper; raise `ORCH_AMBIGUOUS_SETTLE_SECONDS` to widen the margin where wrappers stall for longer.

## Data model

Plain SQL migrations (`src/agent_orchestrator/db/migrations`), psycopg 3, no ORM. Events live as long as their cluster and are deleted by cascade when a discard deletes the cluster row.

| Table | Holds |
|---|---|
| `clusters` | id, unique slug, `spec` (jsonb ClusterSpec), `generation` / `observed_generation` (the reconcile queue), `desired` (`active`, `deleting`), `status`, `preset_id` (nullable; the `ON DELETE SET NULL` cascade sets it to null when the api deletes the preset, an api-originated write, so `clusters` keeps a single writer) and `preset_name` (the name at create, kept) |
| `presets` | named ClusterSpec templates: unique case-insensitive `name` (1 to 40 characters), `spec` (jsonb, only what the user set), `is_default`; migration `0002` seeds `Default` with `{"replicas": 1}`, which can be edited but not renamed or deleted |
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

Documented exception, 2026-10-23: `PATCH /api/v1/clusters/{cluster_id}` was removed (it now answers 405), and `POST /api/v1/clusters` requires exactly one of `spec` and `preset_id`. Why: a live spec edit rolled every agent pod of a running cluster; clusters now snapshot their preset at create and the spec is fixed from then on. To change it, discard the cluster and schedule again from a preset.

Source allowlist: the api checks the direct socket peer against `ORCH_ALLOWED_CIDRS` (default `192.168.1.0/24`). `Forwarded`, `X-Forwarded-For` and `X-Real-IP` are never read. A caller outside the list gets `403 forbidden`. `/healthz` and `/readyz` are exempt.

Error codes: `bad_request` (400), `forbidden` (403), `forbidden_origin` (403, `Origin` not in `ORCH_ALLOWED_ORIGINS` on a terminal POST or DELETE or a WebSocket upgrade), `not_found` (404), `conflict` (409, slug or preset name exists), `preset_default` (409, renaming or deleting the default preset), `last_preset` (409, deleting the only preset), `cluster_deleting` (409), `cluster_limit` (409, `ORCH_MAX_CLUSTERS` reached), `not_queued` (409), `not_failed` (409), `agent_gone` (409), `agent_not_ready` (409, no ready agent for a file or terminal proxy), `invalid_path` (400, a file path is absolute, holds a NUL byte or leaves the workdir), `not_a_directory` (400, listing a file), `not_a_file` (400, reading a directory or special file), `terminal_limit` (409, the wrapper's per-pod terminal cap is reached), `session_not_active` (409, the agent's wrapper session is not active), `terminal_not_found` (404, no such shell on that agent), `invalid_size` (422, rows or cols outside 1..1000), `invalid_request` (422, also a terminal id that is not `term_` plus 12 lowercase hex digits), `target_gone` (422), `internal` (500, 503 on `/readyz`), `wrapper_error` (502, the wrapper answered 5xx), `wrapper_unreachable` (502, the wrapper pod could not be reached), `unavailable` (503, no master key for the proxies).

### `GET /api/v1/meta`

Defaults and limits for forms: `defaults` (model, effort, replicas, auto_compact_pct, pvc_size, storage_class, max_attempts), `agent_image`, `agent_images` (the image allowlist), `models`, `caps` (`max_replicas`, `max_pvc_size`, `max_clusters`, from `ORCH_MAX_REPLICAS`, `ORCH_MAX_PVC_SIZE`, `ORCH_MAX_CLUSTERS`) and `discard_hook` (`none`, `command`, `url`). Response 200.

### `GET /api/v1/presets`

All presets as a list of preset views `{id, name, is_default, spec, created_at, updated_at}`, the default first, then by name. `spec` holds only the keys the caller stored. A fresh database has one preset, `Default`, with spec `{"replicas": 1}`. Response 200.

### `POST /api/v1/presets`

Body `{"name": "<name>", "spec": {...}}`. The name is 1 to 40 characters of letters, digits, spaces, dots, dashes and underscores, with no leading or trailing space; the API never trims it. Names are unique ignoring case. `spec` takes the same fields as the cluster spec (all optional) and is checked against the current settings (models, efforts, image allowlist, caps) like a cluster create; only the keys sent are stored, no defaults are filled. Response 201 with the preset view. Errors: 422 `invalid_request` (bad name, or a spec the settings refuse), 409 `conflict` (name taken).

### `GET /api/v1/presets/{preset_id}`

One preset view. Response 200. Errors: 404 `not_found`.

### `PUT /api/v1/presets/{preset_id}`

Body `{"name": "<name>", "spec": {...}}` replaces the name and the whole spec, with the rules of the create. A model, effort or image that is no longer in the settings lists is accepted only when it equals the value already stored in this preset. The default preset can be edited but not renamed. Response 200 with the preset view. Errors: 404 `not_found`, 409 `conflict` (name taken), 409 `preset_default` (renaming the default), 422 `invalid_request`.

### `DELETE /api/v1/presets/{preset_id}`

Deletes the preset. Clusters made from it keep their spec; their `preset_id` becomes null. Response 204 with no body. Errors: 404 `not_found`, 409 `preset_default` (the default cannot be deleted), 409 `last_preset` (the only preset cannot be deleted).

### `POST /api/v1/presets/{preset_id}/copy`

Optional body `{"name": "<name>"}`. Creates a new, non-default preset holding the source's stored spec verbatim. Without a name it is `<source name> copy`, then `<source name> copy 2`, `copy 3` and so on until free; the source part is cut to fit 40 characters and right-trimmed before ` copy`. Response 201 with the new preset view. Errors: 404 `not_found` (unknown source), 409 `conflict` (an explicit name is taken), 422 `invalid_request` (bad explicit name).

### `GET /api/v1/clusters`

All clusters as a list of cluster views. Response 200.

### `POST /api/v1/clusters`

Schedules a cluster. The body holds exactly one of `spec` and `preset_id`, else 422 `invalid_request` ("exactly one of spec or preset_id"):

- From a preset: `{"preset_id": "<uuid>", "slug": "<dns label, optional>"}`. Without `slug` the API generates `<preset name as a dns label>-<4 random letters>` (for example `default-qkzt`) and retries a clash with a fresh suffix up to 5 times, then answers 409 `conflict`. The preset spec is validated against the current settings when you schedule (a preset that has become invalid is 422 `invalid_request` with a message starting `preset '<name>': `), empty resource keys and an unset `replicas` are filled from the current defaults (`ORCH_DEFAULT_*`, `ORCH_DEFAULT_REPLICAS`), and the result is stored as the cluster's own spec: a snapshot. Editing or deleting the preset afterwards never changes the cluster, its `spec` or its `generation`; the cluster view keeps `preset_name` and `preset_id` becomes null once the preset is deleted. An unknown `preset_id` is 404 `not_found`.
- Raw spec, for headless callers: `{"slug": "<dns label>", "spec": {...}}`; `slug` is required with `spec` (422 otherwise). `spec` fields (all optional): `replicas`, `max_replicas`, `static`, `image`, `model`, `effort`, `peers`, `workdir`, `pvc_size`, `resources`, `env`, `any_mcp_config`, `max_attempts`, `auto_compact_pct`, `discard_hook_url`. `max_replicas` (1 to 16, at most `ORCH_MAX_REPLICAS`, at least `replicas`) is the replica cap and defaults to `replicas`; `static` (default false) pins the size and requires `max_replicas == replicas`. A cluster with `max_replicas > 1` is a hive (no switch): it may run several agents; `static` only forbids scaling. A cluster with `max_replicas == 1` runs alone. The any-mcp plugin name and namespace `orchestrator` are reserved for the rendered plugin (422 `invalid_request`). Presets stay sparse: unset `max_replicas` and `static` are not stored. Such a cluster has null `preset_id` and `preset_name`.

Response 201 with the cluster view. Errors: 422 `invalid_request` (bad slug, both or neither of `spec` and `preset_id`, `spec` without `slug`, image outside `ORCH_AGENT_IMAGES`, reserved env key, replicas or PVC size above the caps, invalid `any_mcp_config`, an invalid preset), 404 `not_found` (unknown preset), 409 `conflict` (slug exists, or no free generated slug), 409 `cluster_limit`, 403 `forbidden`.

### `GET /api/v1/clusters/{cluster_id}`

One cluster view: `id`, `slug`, `spec`, `desired`, `status`, `generation`, `observed_generation`, `created_at`, `preset_id` (null for a raw-spec cluster or once the preset is deleted), `preset_name` and `summary` (`state` of `working`, `waiting`, `booting`, `error`, `deleting`; `ready`, `replicas`, `context_pct_max`, `auto_compact_pct`, `queued`, `failed`). Response 200. Errors: 404 `not_found`.

### `DELETE /api/v1/clusters/{cluster_id}`

Discards the cluster: after the optional history-export hook, deletes every Kubernetes object, the PVC and the chat history. Takes no query parameters. Response 202 with the cluster view (`desired: deleting`). Errors: 404 `not_found`, 422 `invalid_request` (any query parameter).

### `GET /api/v1/clusters/{cluster_id}/agents`

List of agent views: `id`, `ordinal`, `pod_name`, `ready`, `state`, `free_since`, `current_turn_id`, `context` (`used_tokens`, `window_tokens`, `used_percentage`, or null), `peers`, `gone`. Response 200. Errors: 404 `not_found`.

### `POST /api/v1/clusters/{cluster_id}/messages`

Body `{"text": "<non-empty>", "target_agent_id": "<uuid, optional>"}`. Queues a message for round-robin dispatch (or for one agent). Response 202 `{"message_id": <int>, "status": "queued"}`. Errors: 404 `not_found`, 409 `cluster_deleting`, 422 `invalid_request` (empty text, target not in this cluster), 422 `target_gone`.

### `GET /api/v1/clusters/{cluster_id}/messages`

Query `status` (comma-separated, from `queued`, `dispatching`, `verifying`, `sent`, `failed`, `cancelled`) and `limit` (1-500, default 100). Response 200: list of message views (`id`, `cluster_id`, `text`, `status`, `target_agent_id`, `agent_id`, `turn_id`, `attempts`, `max_attempts`, `next_attempt_at`, `error`, `created_at`, `dispatched_at`, `turn`: `status`, `started_at`, `completed_at`, `final_text`, `usage`). Errors: 404 `not_found`, 422 `invalid_request`.

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

### `GET /api/v1/clusters/{cluster_id}/files`

Lists one directory of the cluster's shared workdir, read through the wrapper of the first ready agent (lowest ordinal) or of `agent_id`. Read-only; dotfiles and `.git` are included. Query: `path` (relative to the workdir, default empty is the workdir root), `offset` (integer >= 0, default 0; pass the previous `next_offset`), `agent_id` (optional; an agent of this cluster, else 404 `not_found`). Response 200:

```json
{
  "cluster_id": "5b7e...", "agent_id": "0c1d...",
  "root": "/workdir/proj", "path": "src",
  "entries": [
    {"name": "pkg", "type": "dir", "size": null, "mtime": "2026-10-23T09:01:00.011Z"},
    {"name": "main.py", "type": "file", "size": 812, "mtime": "2026-10-23T09:02:10.500Z"}
  ],
  "next_offset": null
}
```

`root` is the absolute real path of the workdir and `path` the normalised relative path. `type` is `dir`, `file`, `symlink` (listed, never followed) or `other`; `size` is bytes for a `file` and null otherwise; `mtime` is UTC ISO-8601. Directories come first, then the rest, each by name; a page holds at most the wrapper's `limits.files_max_entries` entries and `next_offset` is the `offset` of the next page, or null on the last. A workdir that does not exist yet answers 404 `not_found` for the empty path. Errors: 400 `invalid_path`, 400 `not_a_directory`, 404 `not_found` (cluster, agent, path or workdir), 409 `agent_not_ready` (no usable agent), 422 `invalid_request` (bad `offset`), 502 `wrapper_unreachable`, 502 `wrapper_error`, 503 `unavailable` (no master key). The wrapper status and code are passed through unchanged for 4xx.

### `GET /api/v1/clusters/{cluster_id}/files/content`

Reads one file of the cluster workdir through the same agent choice as the listing. Query: `path` (relative to the workdir), `agent_id` (optional). Response 200:

```json
{
  "cluster_id": "5b7e...", "agent_id": "0c1d...",
  "path": "src/main.py", "size": 812, "mtime": "2026-10-23T09:02:10.500Z",
  "mime": "text/x-python", "kind": "text", "content": "print('hi')\n"
}
```

`mime` is guessed from the file name (null when unknown). `kind` is `too_large` when `size` exceeds the wrapper's `limits.file_preview_max_bytes` (default 1 MiB; `content` null), `binary` when the first 8 KiB hold a NUL byte or the file is not valid UTF-8 (`content` null), else `text` with the decoded `content`. The content is relayed as stored and is not passed through the secret redactor. Errors: 400 `invalid_path`, 400 `not_a_file`, 404 `not_found`, 409 `agent_not_ready`, 502 `wrapper_unreachable`, 502 `wrapper_error`, 503 `unavailable`.

### `POST /api/v1/agents/{agent_id}/terminals`

Starts an interactive login shell in the agent's session workdir, through the agent's wrapper. Body (optional, extra keys are refused): `{"rows": 24, "cols": 80}`, both 1..1000 and defaulting to 24 and 80. Guarded by the Origin allowlist: a request whose `Origin` is not in `ORCH_ALLOWED_ORIGINS` is `403 forbidden_origin`; a request without `Origin` passes. Response 201:

```json
{
  "agent_id": "0c1d...", "cluster_id": "5b7e...", "pod_name": "demo-0",
  "terminal_id": "term_0123456789ab", "created_at": "2026-10-23T09:03:00.000Z",
  "alive": true, "exit_code": null, "attached": false, "rows": 24, "cols": 80
}
```

Attach to the shell with the WebSocket route. Errors: 403 `forbidden_origin`, 404 `not_found` (agent), 409 `agent_not_ready`, 409 `terminal_limit` (the wrapper allows 8 shells per pod), 409 `session_not_active`, 422 `invalid_size`, 422 `invalid_request` (unknown body key), 502 `wrapper_unreachable`, 502 `wrapper_error`, 503 `unavailable`. The wrapper status and code are passed through unchanged for 4xx.

### `GET /api/v1/clusters/{cluster_id}/terminals`

Lists the shells of every ready agent of the cluster. The agents are queried concurrently; one that cannot answer is listed in `unavailable_agents` instead of failing the request, and a cluster with no ready agent answers an empty list. Response 200:

```json
{
  "terminals": [
    {
      "agent_id": "0c1d...", "cluster_id": "5b7e...", "pod_name": "demo-0",
      "terminal_id": "term_0123456789ab", "created_at": "2026-10-23T09:03:00.000Z",
      "alive": true, "exit_code": null, "attached": true, "rows": 40, "cols": 120
    }
  ],
  "unavailable_agents": ["7a2f..."]
}
```

`attached` is true while a client is attached; an exited shell stays listed with `alive` false and its `exit_code` until closed or reaped. Errors: 404 `not_found` (cluster), 503 `unavailable`.

### `DELETE /api/v1/agents/{agent_id}/terminals/{terminal_id}`

Closes one shell and everything it started. Guarded by the Origin allowlist like the create route. `terminal_id` must be `term_` plus 12 lowercase hex digits, else `422 invalid_request` before any wrapper call. Response 204 without a body. Errors: 403 `forbidden_origin`, 404 `not_found` (agent), 404 `terminal_not_found`, 409 `agent_not_ready`, 422 `invalid_request`, 502 `wrapper_unreachable`, 502 `wrapper_error`, 503 `unavailable`.

### `WS /api/v1/agents/{agent_id}/terminals/{terminal_id}/attach`

Attaches to one shell created with `POST /api/v1/agents/{agent_id}/terminals`. WebSocket routes are not in OpenAPI, so this section is the contract. The api relays the socket to the agent's wrapper (`/v1/sessions/{session_id}/terminals/{terminal_id}/attach`), adding the cluster's wrapper bearer itself: the client never sees or sends a wrapper token, and the token appears in no frame, close reason or log line.

Frames, relayed unchanged in both directions:

- binary frames are raw PTY bytes: keystrokes from the client, shell output to the client. Split input into frames of at most 64 KiB; a larger frame is closed by the wrapper with 1009.
- text frames are JSON control messages. Client to server: `{"type": "resize", "cols": 120, "rows": 40}` (1..1000 each) and `{"type": "ping"}`; server to client: `{"type": "pong"}` and `{"type": "exit", "code": 0}` when the shell exits, followed by a close 1000. An unknown control message is closed with 1003. The browser UI pings every 20 s to keep idle proxies from dropping the socket; the api adds no pings of its own.

Replay on attach: the wrapper first sends the shell's recent output (its scrollback ring, up to 256 KiB) as binary frames, then live output, so a reconnecting client redraws the screen. Several clients may attach to one shell at once; a client that cannot keep up is closed with 1013 rather than slowing the shell. Attaching to a shell that already exited replays its output, sends the `exit` frame and closes 1000.

Origin rule: the upgrade is checked against `ORCH_ALLOWED_ORIGINS` (see Origin allowlist) and the source allowlist before it is accepted. An `Origin` of `null` or one not in the list is refused with close 1008 before accept, which a client sees as an HTTP 403 handshake answer; a client that sends no `Origin` (a headless client) passes.

Close codes. Every other failure is a close after the socket was accepted, with the code below and a short reason:

| Code | Reason | When |
|---|---|---|
| 1008 | `invalid_terminal_id` | `terminal_id` is not `term_` plus 12 lowercase hex digits; checked first, no wrapper connection is opened |
| 4404 | `not_found` | unknown agent (or not a UUID), or the wrapper knows no such session or terminal (its 404) |
| 4409 | `agent_not_ready` | the agent is not ready, has no wrapper session or its cluster is being discarded, or the wrapper session is not active (its 409) |
| 1011 | `wrapper_unavailable`, `unavailable` | the wrapper is unreachable, rejected the bearer, or failed otherwise; `unavailable` when no master key is configured |

Once both sides are open, a close on either side closes the other side with a mapped code: 1000, 1003, 1008, 1009, 1011, 1013, 4000-4999 and every other code a close frame may carry (such as 1001 when a browser tab closes) pass through with their reason; 1005 (a close frame without a code) becomes 1000; 1006 (connection dropped without a close frame), 1015 and a close without a code become 1011. The reserved codes 1005, 1006 and 1015 are never sent. The relay keeps no buffer of its own and ends with its connections.

Headless example (Python, `websockets` 17, no `Origin` header):

```python
import asyncio
import json

import httpx
from websockets.asyncio.client import connect

API = "http://orchestrator.example.com"
AGENT = "0c1d..."  # an agent id from GET /api/v1/clusters/{cluster_id}/agents


async def main() -> None:
    async with httpx.AsyncClient(base_url=API) as http:
        made = await http.post(f"/api/v1/agents/{AGENT}/terminals", json={"rows": 24, "cols": 80})
        made.raise_for_status()
        terminal_id = made.json()["terminal_id"]
    url = f"ws://orchestrator.example.com/api/v1/agents/{AGENT}/terminals/{terminal_id}/attach"
    async with connect(url) as ws:
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await ws.send(b"ls -la\rexit\r")  # keystrokes are binary frames
        async for frame in ws:
            if isinstance(frame, bytes):
                print(frame.decode(errors="replace"), end="")  # PTY output, replay first
            elif json.loads(frame)["type"] == "exit":
                break  # the shell ended; the server closes with 1000


asyncio.run(main())
```

Use `wss://` behind a TLS ingress. A closed socket raises `websockets.exceptions.ConnectionClosed`, whose `rcvd.code` is one of the codes above.

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

## Agent API

Agent pods call the orchestrator on `/agent/v1`. It is served only on the agent port (`ORCH_AGENT_PORT`, default 8001, reached through the agents Service); the main port answers 404 for it, and the agent port serves nothing else except `/healthz` and `/readyz`. The pod env carries `ORCH_AGENT_URL`, `ORCH_CLUSTER_ID` and `ORCH_AGENT_TOKEN`.

Every request needs:

- `Authorization: Bearer <ORCH_AGENT_TOKEN>`, the cluster's derived agent token.
- `X-Orch-Cluster: <cluster id>`.

The caller's identity is its socket peer: the direct peer IP must be the `pod_ip` of a not-gone agent of that cluster. Forwarded headers are never read.

Errors use the usual `{"error": {"code", "message"}}` shape:

- `401 unauthorized`: a header is missing, the cluster is unknown or the token is wrong. One shared message for every case.
- `403 forbidden`: the peer IP is not a live agent pod of that cluster (including the former IP of a gone agent).

### `GET /agent/v1/me`

The caller's identity, the leader and the hive members.

```json
{
  "cluster_id": "uuid", "agent_id": "uuid", "pod_name": "string", "ordinal": 0,
  "leader": "pod name or null", "epoch": "int or null",
  "max_replicas": 4, "static": false,
  "members": [
    {"pod_name": "string", "ready": true, "state": "idle", "context_pct": 12.5, "leader": true}
  ]
}
```

`leader` and `epoch` come from the cluster's leader record and are null while there is none. `members` are the not-gone agents by ordinal; `state` is the stored agent state and `context_pct` is null when unknown.

## Bench

`orch bench SPEC --api URL --out FILE [--keep]` runs the experiment: does round-robin with several peered agents finish faster, fail less and verify more often? It talks only to the public `/api/v1` through httpx. See `examples/bench.yaml` (arms 1/off, 3/off and 3/on).

Spec keys: `arms: [{name, replicas, peers}]`, `cluster` (a cluster spec base, for example `model`, `effort`), `tasks: [{name, prompt, verify}]`, `repeats`, `timeout_seconds` and optional `chaos: {after_seconds}`. A bad spec exits 2 and names the field. Prompts are sent verbatim.

For each arm it creates cluster `bench-<arm>-<run>` (a DNS-1123 slug of at most 40 characters), waits for every replica to be ready with a session, then for each task and repeat posts the message and follows `GET /api/v1/stream` until the turn completes or fails (polling `GET /messages/{id}` when the stream is unavailable). With `chaos`, after `after_seconds` it restarts one random busy agent via `POST /agents/{id}/restart`. It then runs the task's `verify` command as a check and waits for it.

Each (arm, task, repeat) appends one JSONL record to `--out`: `arm`, `task`, `repeat`, `cluster_id`, `message_id`, `wall_seconds` (queued to completed), `turn_status`, `failed_turns` (retries plus a final failure), `input_tokens` and `output_tokens` (from the turn `usage`), `verify_passed` and `exit_code`. The summary table has per arm the runs, mean and p50 wall seconds (completed runs), failures, mean tokens and verify pass rate. The bench clusters are discarded at the end (which deletes their history too) unless `--keep`.
