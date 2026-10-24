-- Named ClusterSpec templates. The api is the only writer. Every statement is idempotent.
CREATE TABLE IF NOT EXISTS presets (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
    spec jsonb NOT NULL DEFAULT '{}',
    is_default boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS presets_name_lower ON presets (lower(name));
CREATE UNIQUE INDEX IF NOT EXISTS presets_one_default ON presets (is_default) WHERE is_default;

INSERT INTO presets (id, name, spec, is_default)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default', '{"replicas": 1}', true)
ON CONFLICT DO NOTHING;

-- Provenance of a cluster: its spec is snapshotted at create; the preset may later change or go.
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS preset_id uuid REFERENCES presets (id) ON DELETE SET NULL;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS preset_name text;
