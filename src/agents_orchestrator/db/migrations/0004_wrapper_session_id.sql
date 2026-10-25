-- The claude-code-wrapper's session columns on agents and events take the wrapper_ prefix, so
-- "session" names only the orchestrator's unit (design D3). Renames only: every row is kept.
-- Constraint and index names follow their columns; NOT NULL constraints are named only on
-- Postgres 18 and later, so names are read from the catalog instead of being hardcoded.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname AS tbl, a.attname AS col
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.oid IN ('agents'::regclass, 'events'::regclass)
          AND a.attnum > 0 AND NOT a.attisdropped AND a.attname LIKE 'session\_%'
    LOOP
        EXECUTE format('ALTER TABLE %I RENAME COLUMN %I TO %I', r.tbl, r.col, 'wrapper_' || r.col);
    END LOOP;
    FOR r IN
        SELECT c.relname AS tbl, k.conname AS name
        FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
        WHERE c.oid IN ('agents'::regclass, 'events'::regclass)
          AND k.conname LIKE '%\_session\_%' AND k.conname NOT LIKE '%wrapper\_session\_%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            r.tbl, r.name, replace(r.name, '_session_', '_wrapper_session_')
        );
    END LOOP;
    FOR r IN
        SELECT i.relname AS name
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
        WHERE x.indrelid IN ('agents'::regclass, 'events'::regclass)
          AND i.relname LIKE '%\_session\_%' AND i.relname NOT LIKE '%wrapper\_session\_%'
    LOOP
        EXECUTE format(
            'ALTER INDEX %I RENAME TO %I', r.name, replace(r.name, '_session_', '_wrapper_session_')
        );
    END LOOP;
END $$;
