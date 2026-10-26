-- Session messages (cross-session messaging D8): a message one session sends to another. The api
-- is the only inserter. `sender_session_id` has no foreign key, so purging the sender session
-- leaves the message intact; `sender_slug` keeps the name the frame shows. `hop` counts how many
-- session messages led to this one. Idempotent like 0003 and 0006.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_session_id uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_slug text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS hop integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS messages_sender_session_created
    ON messages (sender_session_id, created_at);
