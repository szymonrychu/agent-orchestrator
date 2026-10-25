CREATE TABLE clusters (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    spec jsonb NOT NULL DEFAULT '{}',
    generation int NOT NULL DEFAULT 1,
    observed_generation int NOT NULL DEFAULT 0,
    desired text NOT NULL DEFAULT 'active' CHECK (desired IN ('active', 'deleting')),
    status jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agents (
    id uuid PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    last_dispatched_at timestamptz,
    ordinal int NOT NULL,
    pod_name text NOT NULL,
    url text,
    pod_phase text,
    ready boolean NOT NULL DEFAULT false,
    session_id text,
    session_created_at timestamptz,
    gone_at timestamptz,
    UNIQUE (cluster_id, ordinal)
);

CREATE TABLE agent_status (
    agent_id uuid PRIMARY KEY REFERENCES agents (id) ON DELETE CASCADE,
    wrapper_idle_since timestamptz,
    state text NOT NULL DEFAULT 'unknown'
        CHECK (state IN ('unknown', 'booting', 'idle', 'busy', 'dead')),
    free_since timestamptz,
    current_turn_id text,
    context_used_tokens int,
    context_window_tokens int,
    context_pct double precision,
    last_seq bigint,
    peers jsonb NOT NULL DEFAULT '[]',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- messages.error is free text; documented reasons: dispatcher_lost, ambiguous,
-- ambiguous_not_started, target_gone, session_gone, unavailable, rejected, config_error.
CREATE TABLE messages (
    id bigserial PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    heartbeat_at timestamptz,
    target_agent_id uuid,
    text text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'dispatching', 'verifying', 'sent', 'failed', 'cancelled')),
    agent_id uuid,
    turn_id text,
    attempts int NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    not_before timestamptz,
    ambiguous_at timestamptz
);
CREATE INDEX messages_cluster_status_id ON messages (cluster_id, status, id);

CREATE TABLE turns (
    turn_id text PRIMARY KEY,
    agent_id uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    origin text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text,
    final_text text,
    usage jsonb
);

CREATE TABLE events (
    id bigserial PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    agent_id uuid NOT NULL,
    session_id text NOT NULL,
    seq bigint NOT NULL,
    ts timestamptz NOT NULL DEFAULT now(),
    type text NOT NULL,
    turn_id text,
    data jsonb NOT NULL DEFAULT '{}',
    UNIQUE (agent_id, session_id, seq)
);
CREATE INDEX events_cluster_id_desc ON events (cluster_id, id DESC);
CREATE INDEX events_agent_id_desc ON events (agent_id, id DESC);

CREATE TABLE commands (
    id bigserial PRIMARY KEY,
    agent_id uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('interrupt', 'compact', 'restart', 'rebootstrap')),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE TABLE checks (
    id bigserial PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    command text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'passed', 'failed', 'cancelled')),
    job_name text,
    exit_code int,
    log_tail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
