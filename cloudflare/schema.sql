-- Signal2Care dashboard store (Cloudflare D1), schema version 2.
-- Wearable series (synthetic or from public datasets), the alerts ("events") with the conversation ("story") attached to each,
-- and the person's own health notes. Phone numbers are never stored here.
--
-- Fresh install:   npx wrangler d1 execute signal2care --local  --file=schema.sql        (or --remote)
-- Existing v1 DB:  apply migrations/0002_story.sql instead (this file would not add the new columns to existing tables).
-- Columns added in version 2 are listed last in each table, in the same order as migrations/0002_story.sql,
-- so a migrated database and a fresh one have identical tables.

CREATE TABLE IF NOT EXISTS series(
  persona TEXT NOT NULL,
  i INTEGER NOT NULL,
  ts TEXT NOT NULL,
  state TEXT NOT NULL,
  quality TEXT NOT NULL,
  v TEXT NOT NULL,
  PRIMARY KEY(persona, i)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS state(
  persona TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor(
  persona TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed(
  id INTEGER PRIMARY KEY,
  persona TEXT NOT NULL,
  ts TEXT,
  actor TEXT,
  kind TEXT,
  text TEXT,
  data TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_persona ON feed(persona, id);

-- One row per alert = one "event". event_id is the human-readable id printed on the picture card, e.g. evt-maya-202610201000-skin_temp.
-- evidence (JSON) holds the numbers and, from version 2, the receipt; status is one of: sent, discussed, resolved, dry_run, suppressed,
-- watching, ignored, data_issue, send_failed, sending, held_consent, held_paused, held_quiet, held_open_story, held_rate,
-- digested, cancelled, expired.
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  persona TEXT NOT NULL,
  kind TEXT,
  metric TEXT,
  family TEXT,
  ts TEXT,
  started TEXT,
  evidence TEXT,
  decision TEXT,
  status TEXT,
  text TEXT,
  sent_at TEXT,
  closed_at TEXT,
  created_at TEXT,
  event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_persona ON alerts(persona, id);

-- The story of one alert: what the person said in the conversation window, the checks run against their own history,
-- and the interpretation as versions (v1 from the data, v2 after they shared context). status is open|closed;
-- while open, closes_at (ISO UTC) is when the window closes. factors/computed/qa/versions are JSON text.
CREATE TABLE IF NOT EXISTS annotations(
  alert_id INTEGER PRIMARY KEY,
  persona TEXT NOT NULL,
  metric TEXT,
  start_ts TEXT,
  end_ts TEXT,
  summary TEXT,
  factors TEXT,
  computed TEXT,
  created_at TEXT,
  updated_at TEXT,
  event_id TEXT,
  status TEXT,
  opened_at TEXT,
  closes_at TEXT,
  qa TEXT,
  versions TEXT
);
CREATE INDEX IF NOT EXISTS idx_annotations_persona ON annotations(persona, alert_id);

-- Messages of an alert's conversation. kind: alert|question|poll|reply|update|closing|explain|system|reply_in;
-- options is a JSON array of strings, only for kind poll. delivery (outgoing messages): sent|delivered|read|failed, only moves forward.
CREATE TABLE IF NOT EXISTS thread(
  alert_id INTEGER NOT NULL,
  mid TEXT NOT NULL,
  direction TEXT,
  text TEXT,
  created_at TEXT,
  kind TEXT,
  options TEXT,
  delivery TEXT,
  PRIMARY KEY(alert_id, mid)
) WITHOUT ROWID;

-- The person's own health notes: a snapshot that replaces the previous one. items and flags are JSON text.
CREATE TABLE IF NOT EXISTS profile(
  persona TEXT PRIMARY KEY,
  items TEXT NOT NULL,
  flags TEXT,
  updated_at INTEGER NOT NULL
);

-- Small singletons: rev, epoch, send_alerts, health, personas (the people the dashboard can show, plus which one a phone is linked to).
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  persona TEXT,
  args TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  result TEXT,
  created_at INTEGER NOT NULL,
  done_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status, id);
