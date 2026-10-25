# agents-orchestrator chart

Installs the agents-orchestrator api, dispatcher, ingester and reconciler into the one namespace
it manages. The full guide (install, Postgres, secrets, RBAC, ingress, security) is the root
`README.md`; this file covers the values that need deployment-specific input.

## Allowed origins

The api refuses every WebSocket upgrade (the terminal) whose `Origin` header is `null` or not
listed in `ORCH_ALLOWED_ORIGINS`, because browsers apply no CORS to WebSocket upgrades. The list
is empty by default, which refuses every browser upgrade, so a deployment that serves the UI must
list the UI origin through `env`:

```yaml
env:
  # production: the ingress origin the browser loads the UI from
  ORCH_ALLOWED_ORIGINS: https://orchestrator.example.com
  # local UI development against the Vite dev server (http://localhost:5173):
  # ORCH_ALLOWED_ORIGINS: http://localhost:5173
```

Entries are comma-separated `scheme://host[:port]` origins, lower-case, without a path or a
trailing slash. `env` reaches every component's ConfigMap unchanged.

## Ingress timeouts for WebSockets

ingress-nginx closes idle proxied connections after 60 s. Raise both timeouts through
`ingress.annotations`; the UI pings every 20 s, so 3600 keeps an idle terminal open:

```yaml
ingress:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```
