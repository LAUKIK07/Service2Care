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

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, msg_id UNINDEXED, handle UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, event_id UNINDEXED, handle UNINDEXED, tokenize='porter unicode61'
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with self.conn() as c:
            c.executescript(SCHEMA)
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
