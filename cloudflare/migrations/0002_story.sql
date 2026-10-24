-- Migration 0002: stories, event ids, poll options and the health record.
-- Takes a database created from schema version 1 (9 tables) to version 2. It is NOT idempotent: SQLite/D1 has no
-- "ADD COLUMN IF NOT EXISTS", so run it exactly once per database (cloudflare/scripts/migrate.mjs checks first and is safe to repeat).
--
-- Apply locally:   cd cloudflare && npx wrangler d1 execute signal2care --local  --file=migrations/0002_story.sql
-- Apply remotely:  cd cloudflare && npx wrangler d1 execute signal2care --remote --file=migrations/0002_story.sql
--                  (the lead runs the remote one, BEFORE deploying the new Worker; the new Worker needs these columns.
--                   The old Worker keeps working on the migrated database, so the order migrate -> deploy is always safe.)
-- Check:           npx wrangler d1 execute signal2care --remote --command "SELECT name FROM pragma_table_info('annotations')"
--
-- Existing rows keep working: every new column is nullable and the dashboard treats a missing value as "not recorded".
-- Fresh databases do not need this file; schema.sql already contains the final tables.

ALTER TABLE alerts ADD COLUMN event_id TEXT;

ALTER TABLE annotations ADD COLUMN event_id TEXT;
ALTER TABLE annotations ADD COLUMN status TEXT;
ALTER TABLE annotations ADD COLUMN opened_at TEXT;
ALTER TABLE annotations ADD COLUMN closes_at TEXT;
ALTER TABLE annotations ADD COLUMN qa TEXT;
ALTER TABLE annotations ADD COLUMN versions TEXT;
CREATE INDEX IF NOT EXISTS idx_annotations_persona ON annotations(persona, alert_id);

ALTER TABLE thread ADD COLUMN kind TEXT;
ALTER TABLE thread ADD COLUMN options TEXT;
ALTER TABLE thread ADD COLUMN delivery TEXT;

CREATE TABLE IF NOT EXISTS profile(
  persona TEXT PRIMARY KEY,
  items TEXT NOT NULL,
  flags TEXT,
  updated_at INTEGER NOT NULL
);
