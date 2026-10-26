-- Leader auto handover (D3): when the agent last compacted, by the wrapper's clock (the `ts` of
-- its `compacting` or `compacted` event). The ingester is the only writer and never moves it
-- backwards. Idempotent like 0003.
ALTER TABLE agent_status ADD COLUMN IF NOT EXISTS compaction_at timestamptz;
