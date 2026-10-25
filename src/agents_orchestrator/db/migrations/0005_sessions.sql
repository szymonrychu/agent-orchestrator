-- The orchestrator's unit is a session (design D2, D4): the unit's tables, its foreign-key
-- columns and every index and constraint named after it take the session name. Renames only:
-- every row is kept. NOT NULL constraints are named only on Postgres 18 and later, so index and
-- constraint names are read from the catalog instead of being hardcoded.
ALTER TABLE clusters RENAME TO sessions;
ALTER TABLE cluster_leaders RENAME TO session_leaders;

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname AS tbl
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind = 'r'
          AND a.attnum > 0 AND NOT a.attisdropped AND a.attname = 'cluster_id'
    LOOP
        EXECUTE format('ALTER TABLE %I RENAME COLUMN cluster_id TO session_id', r.tbl);
    END LOOP;
    -- Constraints first: renaming a primary key or unique constraint renames its index too.
    FOR r IN
        SELECT c.relname AS tbl, k.conname AS name
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND k.conname LIKE '%cluster%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            r.tbl, r.name, replace(r.name, 'cluster', 'session')
        );
    END LOOP;
    FOR r IN
        SELECT i.relname AS name
        FROM pg_class i
        JOIN pg_namespace n ON n.oid = i.relnamespace
        WHERE n.nspname = current_schema() AND i.relkind = 'i' AND i.relname LIKE '%cluster%'
    LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I', r.name, replace(r.name, 'cluster', 'session'));
    END LOOP;
END $$;
