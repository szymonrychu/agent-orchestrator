{{/*
Chosen Postgres sources. Returns a comma separated list of the sources that are set.
*/}}
{{- define "agent-orchestrator.postgresSources" -}}
{{- $s := list -}}
{{- if .Values.cnpg.enabled }}{{ $s = append $s "cnpg.enabled" }}{{ end -}}
{{- if .Values.postgres.existingSecret }}{{ $s = append $s "postgres.existingSecret" }}{{ end -}}
{{- if .Values.postgres.url }}{{ $s = append $s "postgres.url" }}{{ end -}}
{{- if and .Values.postgres.host .Values.postgres.user .Values.postgres.database }}{{ $s = append $s "postgres.host/user/database" }}{{ end -}}
{{- join "," $s -}}
{{- end }}

{{/*
Validated Postgres source name: one of cnpg, existingSecret, url, parts.
*/}}
{{- define "agent-orchestrator.postgresSource" -}}
{{- $sources := splitList "," (include "agent-orchestrator.postgresSources" .) | compact -}}
{{- if eq (len $sources) 0 -}}
{{- fail "agent-orchestrator: a Postgres connection is required: set postgres.url, postgres.host/user/database/password, postgres.existingSecret, or cnpg.enabled=true" -}}
{{- else if gt (len $sources) 1 -}}
{{- fail (printf "agent-orchestrator: exactly one Postgres source may be set, got %s" (join ", " $sources)) -}}
{{- end -}}
{{- index (dict "cnpg.enabled" "cnpg" "postgres.existingSecret" "existingSecret" "postgres.url" "url" "postgres.host/user/database" "parts") (first $sources) -}}
{{- end }}

{{/*
The connection URI stored in the chart database Secret (sources url and parts).
*/}}
{{- define "agent-orchestrator.databaseUri" -}}
{{- $p := .Values.postgres -}}
{{- if $p.url -}}
{{- $p.url -}}
{{- else -}}
{{- $uri := printf "postgresql://%s:%s@%s:%v/%s" (urlquery $p.user | replace "+" "%20") (urlquery $p.password | replace "+" "%20") $p.host $p.port $p.database -}}
{{- if $p.sslmode }}{{ $uri = printf "%s?sslmode=%s" $uri $p.sslmode }}{{ end -}}
{{- $uri -}}
{{- end -}}
{{- end }}

{{/*
ORCH_DATABASE_URL env entry from the resolved source.
*/}}
{{- define "agent-orchestrator.databaseUrlEnv" -}}
{{- $source := include "agent-orchestrator.postgresSource" . -}}
- name: ORCH_DATABASE_URL
  valueFrom:
    secretKeyRef:
{{- if eq $source "cnpg" }}
      name: {{ include "agent-orchestrator.fullname" . }}-db-app
      key: uri
{{- else if eq $source "existingSecret" }}
      name: {{ .Values.postgres.existingSecret }}
      key: {{ .Values.postgres.existingSecretKey | default "uri" }}
{{- else }}
      name: {{ include "agent-orchestrator.fullname" . }}-database
      key: uri
{{- end }}
{{- end }}

{{/* Name of the Secret holding the master key. */}}
{{- define "agent-orchestrator.masterKeySecretName" -}}
{{- if .Values.masterKey.existingSecret -}}
{{- .Values.masterKey.existingSecret -}}
{{- else if .Values.masterKey.value -}}
{{- include "agent-orchestrator.fullname" . }}-master-key
{{- else -}}
{{- fail "agent-orchestrator: masterKey is required: set masterKey.value or masterKey.existingSecret" -}}
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the Claude OAuth token. */}}
{{- define "agent-orchestrator.claudeSecretName" -}}
{{- if .Values.claude.existingSecret -}}
{{- .Values.claude.existingSecret -}}
{{- else if .Values.claude.token -}}
{{- include "agent-orchestrator.fullname" . }}-claude
{{- else -}}
{{- fail "agent-orchestrator: claude is required: set claude.token or claude.existingSecret" -}}
{{- end -}}
{{- end }}

{{/*
Container env shared by every component: envFrom the settings ConfigMap, the database
URL and the master key. Include inside a container with nindent.
*/}}
{{- define "agent-orchestrator.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agent-orchestrator.fullname" . }}-settings
env:
{{ include "agent-orchestrator.databaseUrlEnv" . | indent 2 }}
  - name: ORCH_MASTER_KEY
    valueFrom:
      secretKeyRef:
        name: {{ include "agent-orchestrator.masterKeySecretName" . }}
        key: {{ .Values.masterKey.existingSecretKey | default "master-key" }}
{{- if or .Values.serviceApi.existingSecret .Values.serviceApi.tokenHashes }}
  - name: ORCH_SERVICE_TOKENS
    valueFrom:
      secretKeyRef:
        name: {{ include "agent-orchestrator.serviceTokensSecretName" . }}
        key: {{ .Values.serviceApi.existingSecretKey | default "service-tokens" }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding ORCH_SERVICE_TOKENS: existingSecret wins over the chart Secret. */}}
{{- define "agent-orchestrator.serviceTokensSecretName" -}}
{{- default (printf "%s-services" (include "agent-orchestrator.fullname" .)) .Values.serviceApi.existingSecret -}}
{{- end }}

{{/* CSV name:sha256hex of serviceApi.tokenHashes sorted by name; a malformed hash fails the render. */}}
{{- define "agent-orchestrator.serviceTokensCsv" -}}
{{- $pairs := list -}}
{{- range $name := keys .Values.serviceApi.tokenHashes | sortAlpha -}}
{{- $hash := index $.Values.serviceApi.tokenHashes $name | toString -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" $hash) -}}
{{- fail (printf "agent-orchestrator: serviceApi.tokenHashes.%s must be 64 lowercase hex characters (sha256)" $name) -}}
{{- end -}}
{{- $pairs = append $pairs (printf "%s:%s" $name $hash) -}}
{{- end -}}
{{- join "," $pairs -}}
{{- end }}

{{/*
ORCH_AGENT_API_URL: the cluster-internal agents Service URL, unless env.ORCH_AGENT_API_URL overrides it.
*/}}
{{- define "agent-orchestrator.agentApiUrl" -}}
{{- default (printf "http://%s-agents.%s.svc:%v" (include "agent-orchestrator.fullname" .) .Release.Namespace .Values.agentApi.port) (index .Values.env "ORCH_AGENT_API_URL") -}}
{{- end }}
