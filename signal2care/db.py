"""SQLite storage. One file, WAL mode, FTS5 for lexical recall."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  handle TEXT PRIMARY KEY,
  opted_out INTEGER NOT NULL DEFAULT 0,
  pending_forget INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- Layer 1: lossless raw log. Rows are never rewritten.
CREATE TABLE IF NOT EXISTS messages(
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  event_id TEXT,
  chat_id TEXT,
  handle TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in','out')),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_handle ON messages(handle, rowid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_event ON messages(event_id) WHERE event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  filename TEXT,
  mime TEXT,
  size_bytes INTEGER,
  url TEXT,
  path TEXT,
  sha256 TEXT,
  ocr_text TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);

-- Layer 2: episodic events (self-contained statements with source message ids).
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL,
  text TEXT NOT NULL,
  entities TEXT NOT NULL DEFAULT '[]',
  when_hint TEXT,
  source_ids TEXT NOT NULL DEFAULT '[]',
  embedding BLOB,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

-- Layer 3: semantic profile facts with supersession (history is kept).
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(handle, key, status);

-- Layer 4: rolling summary, orientation only.
CREATE TABLE IF NOT EXISTS summaries(
  handle TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors(
  handle TEXT PRIMARY KEY,
  consolidated_rowid INTEGER NOT NULL DEFAULT 0
);

-- Wearable stream (synthetic). One row per 15-minute tick; v is JSON of every channel.
CREATE TABLE IF NOT EXISTS series(
  persona TEXT NOT NULL,
  i INTEGER NOT NULL,
  ts TEXT NOT NULL,
  state TEXT NOT NULL,
  quality TEXT NOT NULL,
  v TEXT NOT NULL,
  PRIMARY KEY(persona, i)
);
CREATE TABLE IF NOT EXISTS replay(
  persona TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'idle',
  cursor INTEGER NOT NULL DEFAULT 0,
  speed REAL NOT NULL DEFAULT 2,
  updated_at TEXT
);
-- Alerts the monitor raised and what was decided about them.
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona TEXT NOT NULL,
  kind TEXT,
  metric TEXT,
  family TEXT,
  ts TEXT,
  started TEXT,
  evidence TEXT,
  decision TEXT,
  status TEXT NOT NULL,
  text TEXT,
  message_id TEXT,
  handle TEXT,
  sent_at TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_persona ON alerts(persona, id);
-- What the person said about an alert plus the arithmetic that checks it.
CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id INTEGER UNIQUE,
  persona TEXT,
  metric TEXT,
  start_ts TEXT,
  end_ts TEXT,
  summary TEXT,
  factors TEXT,
  computed TEXT,
  created_at TEXT,
  updated_at TEXT
);
-- Activity log of what the monitor, Jev and the language model did.
CREATE TABLE IF NOT EXISTS feed(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona TEXT,
  ts TEXT,
  actor TEXT,
  kind TEXT,
  text TEXT,
  data TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);

-- Every proactive send goes through the outbox first, so a retry or a restart can never send the same thing twice.
CREATE TABLE IF NOT EXISTS outbox(
  key TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  message_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Polls we sent that expect a tap: which question, and which option id means what.
CREATE TABLE IF NOT EXISTS pending_polls(
  message_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  purpose TEXT NOT NULL,
  ref TEXT,
  options TEXT NOT NULL,
  created_at TEXT NOT NULL,
  answered_at TEXT
);

-- Things the person asked us to double-check before we use them (for example a number read from a photo).
CREATE TABLE IF NOT EXISTS pending_confirms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  source_message_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, msg_id UNINDEXED, handle UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, event_id UNINDEXED, handle UNINDEXED, tokenize='porter unicode61'
);
"""


# Columns added after the first release. Applied once, in order, so existing databases keep working.
MIGRATIONS = [
    ("users", "consent", "INTEGER NOT NULL DEFAULT 0"),            # explicit START before any proactive text
    ("users", "consent_at", "TEXT"),
    ("users", "onboarding", "TEXT NOT NULL DEFAULT 'new'"),         # new | consent | profile | done
    ("users", "ob_step", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "paused_until", "TEXT"),
    ("users", "quiet_off", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_ref", "TEXT"),                                  # items the last 'what do you remember' listed
    ("messages", "alert_id", "INTEGER"),                            # conversation -> event link
    ("messages", "kind", "TEXT NOT NULL DEFAULT 'text'"),
    ("messages", "delivery", "TEXT"),
    ("messages", "reply_to", "TEXT"),
    ("alerts", "event_id", "TEXT"),
    ("alerts", "receipt", "TEXT"),
    ("alerts", "question_kind", "TEXT"),
    ("alerts", "poll_message_id", "TEXT"),
    ("alerts", "delivery", "TEXT"),
    ("annotations", "event_id", "TEXT"),
    ("annotations", "status", "TEXT NOT NULL DEFAULT 'closed'"),    # open while the conversation window is running
    ("annotations", "opened_at", "TEXT"),
    ("annotations", "closes_at", "TEXT"),
    ("annotations", "qa", "TEXT"),
    ("annotations", "versions", "TEXT"),
    ("facts", "origin", "TEXT NOT NULL DEFAULT 'chat'"),            # chat | onboarding | correction | photo
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, column, ddl in MIGRATIONS:
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_messages_alert ON messages(alert_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_event ON alerts(event_id) WHERE event_id IS NOT NULL")
        os.chmod(path, 0o600)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
