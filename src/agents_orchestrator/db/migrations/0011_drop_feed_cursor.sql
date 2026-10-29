-- Hive decoupling (D1): members no longer receive streamed activity, so the dispatcher stopped
-- reading and writing the leader row's feed cursor. Drop the dead column. Idempotent like 0003
-- and 0006. The column's values are lost on apply. (0010 is reserved for slash-commands-from-chat,
-- which has not merged; the migrator applies by set of names, so the gap is harmless.)
ALTER TABLE session_leaders DROP COLUMN IF EXISTS feed_cursor;
