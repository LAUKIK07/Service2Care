-- Signal2Care dashboard store (Cloudflare D1). Synthetic wearable data plus the alert threads shown on the dashboard.
-- Phone numbers are never stored here.

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
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_persona ON alerts(persona, id);

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
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS thread(
  alert_id INTEGER NOT NULL,
  mid TEXT NOT NULL,
  direction TEXT,
  text TEXT,
  created_at TEXT,
  PRIMARY KEY(alert_id, mid)
) WITHOUT ROWID;

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
