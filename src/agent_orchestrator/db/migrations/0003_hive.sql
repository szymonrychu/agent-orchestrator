-- Hive mode: leadership queue, leader record, advisory file locks, pod identity.
-- Every statement is idempotent. No feed history is added: cluster_leaders is not backfilled.

-- Queue. The api inserts; the dispatcher decides rows only through status-guarded updates.
CREATE TABLE IF NOT EXISTS leadership_requests (
    id bigserial PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('handover', 'user')),
    from_agent_id uuid REFERENCES agents (id),
    to_agent_id uuid REFERENCES agents (id),
    reason text NOT NULL DEFAULT '',
    epoch int,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'applied', 'rejected')),
    reject_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz
);

CREATE INDEX IF NOT EXISTS leadership_requests_queued
    ON leadership_requests (cluster_id, id) WHERE status = 'queued';

-- Current leader per cluster. The dispatcher is the only writer. The leader-row insert sets
-- feed_cursor from coalesce(max(events.id), 0) of the cluster, so history is never pushed.
CREATE TABLE IF NOT EXISTS cluster_leaders (
    cluster_id uuid PRIMARY KEY REFERENCES clusters (id) ON DELETE CASCADE,
    agent_id uuid NOT NULL REFERENCES agents (id),
    epoch int NOT NULL,
    since timestamptz NOT NULL,
    reason text NOT NULL,
    unready_since timestamptz,
    feed_cursor bigint NOT NULL DEFAULT 0,
    state_hash text,
    state_pushed_at timestamptz
);

-- Advisory file claims. The api is the only writer.
CREATE TABLE IF NOT EXISTS file_locks (
    cluster_id uuid NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    path text NOT NULL,
    agent_id uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, path)
);

-- Pod IP the agent api binds tokens to. The reconciler is the writer, like the rest of agents.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS pod_ip text;

-- Freeze the replica cap for stored specs so later in-place replicas edits never change it.
UPDATE clusters SET spec = jsonb_set(spec, '{max_replicas}', spec -> 'replicas')
WHERE spec ? 'replicas' AND NOT spec ? 'max_replicas';
