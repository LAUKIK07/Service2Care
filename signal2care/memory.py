"""Layered memory for one chat thread per person.

Layers
1. Raw log: every message, verbatim, never rewritten (lossless).
2. Events: short self-contained statements that cite their source messages (episodic,
   non-compressive, in the spirit of event-centric memory research).
3. Facts: stable profile slots. A changed fact supersedes the old one and history is kept.
4. Summary: a short rolling note for orientation only.
5. Working window: the most recent messages, loaded verbatim every turn.

Recall fuses BM25 (SQLite FTS5) and embedding similarity with reciprocal rank fusion.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from array import array
from operator import mul
from pathlib import Path

from .config import Settings
from .db import Database
from .llm import LLMClient, LLMError, extract_json
from .memctl import ControlMixin
from .prompts import CONSOLIDATE_PROMPT
from .textutil import fmt_ts, now_iso

log = logging.getLogger("s2c.memory")

STOPWORDS = frozenset(
    "the and for are but not you your with that this have has was were from they them what when where "
    "which who how can will would could should just like about into out get got its our their there here "
    "been being than then too very also did does done".split()
)
SEMANTIC_MIN_EVENTS = 20  # below this, the window plus lexical recall already cover everything
BATCH = 40


def fts_query(text: str) -> str | None:
    words: list[str] = []
    for w in re.findall(r"[A-Za-z0-9']{3,}", (text or "").lower()):
        w = w.replace("'", "")
        if len(w) >= 3 and w not in STOPWORDS and w not in words:
            words.append(w)
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words[:24])


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def _norm(a: array) -> float:
    return math.sqrt(sum(map(mul, a, a)))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")[:40]


def _rrf(*ranked: list[int], k: int = 60) -> list[int]:
    score: dict[int, float] = {}
    for ids in ranked:
        for rank, ident in enumerate(ids):
            score[ident] = score.get(ident, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=score.get, reverse=True)  # type: ignore[arg-type]


class Memory(ControlMixin):
    def __init__(self, db: Database, llm: LLMClient, settings: Settings):
        self.db = db
        self.llm = llm
        self.s = settings

    # ------------------------------------------------------------------ users
    def ensure_user(self, handle: str) -> bool:
        """Create the user row. Returns True if this is the first time we see them."""
        with self.db.conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO users(handle, created_at) VALUES(?, ?)", (handle, now_iso()))
            return cur.rowcount == 1

    def is_opted_out(self, handle: str) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT opted_out FROM users WHERE handle=?", (handle,)).fetchone()
        return bool(row and row["opted_out"])

    def set_opt_out(self, handle: str, value: bool) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE users SET opted_out=? WHERE handle=?", (int(value), handle))

    def pending_forget(self, handle: str) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT pending_forget FROM users WHERE handle=?", (handle,)).fetchone()
        return bool(row and row["pending_forget"])

    def set_pending_forget(self, handle: str, value: bool) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE users SET pending_forget=? WHERE handle=?", (int(value), handle))

    # ---------------------------------------------------------------- raw log
    def record_message(self, *, msg_id: str, event_id: str | None, chat_id: str, handle: str,
                       direction: str, text: str, created_at: str) -> bool:
        """Store a message. Returns False if it was already stored (duplicate delivery)."""
        with self.db.conn() as c:
            try:
                c.execute(
                    "INSERT INTO messages(id, event_id, chat_id, handle, direction, text, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (msg_id, event_id, chat_id, handle, direction, text, created_at),
                )
            except sqlite3.IntegrityError:
                return False
            c.execute("INSERT INTO messages_fts(text, msg_id, handle) VALUES(?,?,?)", (text, msg_id, handle))
        return True

    def pending_user_messages(self, handle: str) -> list[sqlite3.Row]:
        """Incoming messages that arrived after our last reply."""
        with self.db.conn() as c:
            return c.execute(
                "SELECT rowid, id, chat_id, text, created_at FROM messages "
                "WHERE handle=? AND direction='in' AND kind != 'tapback' AND rowid > "
                "COALESCE((SELECT MAX(rowid) FROM messages WHERE handle=? AND direction='out'), 0) "
                "ORDER BY rowid",
                (handle, handle),
            ).fetchall()

    def recent_transcript(self, handle: str, char_budget: int = 40_000) -> list[sqlite3.Row]:
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT rowid, id, direction, text, created_at FROM messages WHERE handle=? "
                "ORDER BY rowid DESC LIMIT 300",
                (handle,),
            ).fetchall()
        out: list[sqlite3.Row] = []
        used = 0
        for r in rows:
            used += len(r["text"]) + 40
            if used > char_budget and out:
                break
            out.append(r)
        return list(reversed(out))

    # ------------------------------------------------------------ attachments
    def add_attachment(self, *, att_id: str, message_id: str, handle: str, filename: str | None,
                       mime: str | None, size_bytes: int | None, url: str | None) -> None:
        with self.db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO attachments(id, message_id, handle, filename, mime, size_bytes, url, "
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (att_id, message_id, handle, filename, mime, size_bytes, url, now_iso()),
            )

    def attachments_for(self, message_id: str) -> list[sqlite3.Row]:
        with self.db.conn() as c:
            return c.execute("SELECT * FROM attachments WHERE message_id=? ORDER BY created_at", (message_id,)).fetchall()

    def attachment_texts(self, message_ids: list[str]) -> dict[str, list[tuple[str, str]]]:
        """Text we hold for attachments (document text, OCR, image notes), keyed by message id."""
        if not message_ids:
            return {}
        out: dict[str, list[tuple[str, str]]] = {}
        with self.db.conn() as c:
            for i in range(0, len(message_ids), 90):
                chunk = message_ids[i:i + 90]
                marks = ",".join("?" * len(chunk))
                for r in c.execute(
                    f"SELECT message_id, filename, ocr_text FROM attachments WHERE message_id IN ({marks}) "
                    "AND ocr_text IS NOT NULL AND ocr_text != '' ORDER BY created_at", chunk):
                    out.setdefault(r["message_id"], []).append((r["filename"] or "file", r["ocr_text"]))
        return out

    def get_attachment(self, att_id: str) -> sqlite3.Row | None:
        with self.db.conn() as c:
            return c.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()

    def recent_image_attachment(self, handle: str) -> sqlite3.Row | None:
        with self.db.conn() as c:
            return c.execute(
                "SELECT * FROM attachments WHERE handle=? AND status='ready' AND mime LIKE 'image/%' "
                "ORDER BY created_at DESC LIMIT 1",
                (handle,),
            ).fetchone()

    def mark_attachment(self, att_id: str, *, status: str, path: str | None = None, sha256: str | None = None) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE attachments SET status=?, path=COALESCE(?, path), sha256=COALESCE(?, sha256) WHERE id=?",
                      (status, path, sha256, att_id))

    def set_ocr(self, att_id: str, message_id: str, handle: str, text: str) -> None:
        """Store OCR text and make it searchable alongside the message."""
        with self.db.conn() as c:
            c.execute("UPDATE attachments SET ocr_text=? WHERE id=?", (text, att_id))
            if text.strip():
                c.execute("INSERT INTO messages_fts(text, msg_id, handle) VALUES(?,?,?)", (text, message_id, handle))

    # -------------------------------------------------------- profile + notes
    def active_facts(self, handle: str) -> list[sqlite3.Row]:
        with self.db.conn() as c:
            return c.execute(
                "SELECT key, value FROM facts WHERE handle=? AND status='active' ORDER BY key", (handle,)
            ).fetchall()

    def get_summary(self, handle: str) -> str:
        with self.db.conn() as c:
            row = c.execute("SELECT text FROM summaries WHERE handle=?", (handle,)).fetchone()
        return row["text"] if row else ""

    def export_for_user(self, handle: str) -> str:
        facts = self.active_facts(handle)
        summary = self.get_summary(handle)
        with self.db.conn() as c:
            events = c.execute(
                "SELECT text, when_hint FROM events WHERE handle=? AND status='active' ORDER BY id DESC LIMIT 12",
                (handle,),
            ).fetchall()
        lines: list[str] = []
        if facts:
            lines.append("Things I've noted about you:")
            lines += [f"- {f['key'].replace('_', ' ')}: {f['value']}" for f in facts]
        if summary:
            lines.append("")
            lines.append("The short version of our chats: " + summary)
        if events:
            lines.append("")
            lines.append("Recent details I kept:")
            lines += [f"- {e['text']}" for e in events]
        if not lines:
            return "I don't have any notes about you yet. Tell me about yourself and I'll remember."
        lines.append("")
        lines.append("Say \"forget everything\" to erase all of it.")
        return "\n".join(lines)[:1500]

    def wipe(self, handle: str) -> None:
        """Erase everything stored for this person (messages, notes, images)."""
        with self.db.conn() as c:
            paths = [r["path"] for r in c.execute(
                "SELECT path FROM attachments WHERE handle=? AND path IS NOT NULL", (handle,))]
            for table in ("messages_fts", "events_fts"):
                c.execute(f"DELETE FROM {table} WHERE handle=?", (handle,))
            for table in ("attachments", "events", "facts", "summaries", "cursors", "messages", "pending_polls", "pending_confirms", "outbox"):
                c.execute(f"DELETE FROM {table} WHERE handle=?", (handle,))
            # a clean slate: the person is asked again for consent and their health notes
            c.execute("UPDATE users SET pending_forget=0, consent=0, consent_at=NULL, onboarding='new', ob_step=0, "
                      "paused_until=NULL, last_ref=NULL WHERE handle=?", (handle,))
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass

    # -------------------------------------------------------------- retrieval
    async def retrieve(self, handle: str, query: str, *, window_min_rowid: int,
                       k_events: int = 10, k_msgs: int = 4) -> dict:
        """Recall older details relevant to `query`. Messages inside the working window are skipped."""
        q = fts_query(query)
        ev_lex: list[sqlite3.Row] = []
        msg_lex: list[sqlite3.Row] = []
        with self.db.conn() as c:
            if q:
                try:
                    ev_lex = c.execute(
                        "SELECT e.id, e.text, e.when_hint, e.created_at FROM events_fts "
                        "JOIN events e ON e.id = events_fts.event_id "
                        "WHERE events_fts MATCH ? AND events_fts.handle = ? AND e.status='active' "
                        "ORDER BY bm25(events_fts) LIMIT 30",
                        (q, handle),
                    ).fetchall()
                    msg_lex = c.execute(
                        "SELECT m.rowid AS rowid, m.id AS mid, m.direction, m.text, m.created_at FROM messages_fts "
                        "JOIN messages m ON m.id = messages_fts.msg_id "
                        "WHERE messages_fts MATCH ? AND messages_fts.handle = ? "
                        "ORDER BY bm25(messages_fts) LIMIT 20",
                        (q, handle),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    log.warning("fts query failed: %s", exc)
            embedded = c.execute(
                "SELECT COUNT(*) AS n FROM events WHERE handle=? AND status='active' AND embedding IS NOT NULL",
                (handle,),
            ).fetchone()["n"]

        ev_sem: list[tuple[int, sqlite3.Row]] = []
        if embedded >= SEMANTIC_MIN_EVENTS and query.strip():
            try:
                qv = (await self.llm.embed([query[:2000]]))[0]
                ev_sem = self._semantic(handle, qv)
            except LLMError as exc:
                log.info("semantic recall skipped: %s", exc)

        by_id = {r["id"]: r for r in ev_lex}
        by_id.update({eid: row for eid, row in ev_sem})
        fused = _rrf([r["id"] for r in ev_lex], [eid for eid, _ in ev_sem])[:k_events]
        events = [by_id[i] for i in fused]
        events.sort(key=lambda r: r["created_at"])

        msgs: list[sqlite3.Row] = []
        seen_rows: set[int] = set()
        for m in msg_lex:  # a message can match twice (its text and its OCR text)
            if m["rowid"] < window_min_rowid and m["rowid"] not in seen_rows:
                seen_rows.add(m["rowid"])
                msgs.append(m)
        msgs = msgs[:k_msgs]
        msgs.sort(key=lambda r: r["rowid"])
        shown = []
        with self.db.conn() as c:
            for m in msgs:
                text = m["text"][:500]
                ocr = c.execute("SELECT ocr_text FROM attachments WHERE message_id=? AND ocr_text != '' LIMIT 1",
                                (m["mid"],)).fetchone()
                if ocr:
                    text += f" [attachment text: {ocr['ocr_text'][:1500]}]"
                shown.append({"who": "you" if m["direction"] == "in" else "me", "text": text, "at": m["created_at"]})
        return {
            "events": [{"text": e["text"], "when": e["when_hint"], "at": e["created_at"]} for e in events],
            "messages": shown,
        }

    def _semantic(self, handle: str, qv: list[float]) -> list[tuple[int, sqlite3.Row]]:
        qa = array("f", qv)
        qn = _norm(qa)
        if qn == 0:
            return []
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT id, text, when_hint, created_at, embedding FROM events "
                "WHERE handle=? AND status='active' AND embedding IS NOT NULL",
                (handle,),
            ).fetchall()
        scored = []
        for r in rows:
            v = _unpack(r["embedding"])
            if len(v) != len(qa):  # stored with a different embedding model
                continue
            n = _norm(v)
            if n:
                scored.append((sum(map(mul, qa, v)) / (qn * n), r))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [(r["id"], r) for s, r in scored[:30] if s > 0.35]

    # ---------------------------------------------------------- consolidation
    async def consolidate(self, handle: str) -> bool:
        """Turn new messages into events, fact updates and a refreshed summary. Returns True if work was done.

        Raises LLMUnavailable (from the client) if no model can answer; the cursor stays put, so the
        same messages are retried next time and nothing is lost.
        """
        with self.db.conn() as c:
            cur = c.execute("SELECT consolidated_rowid FROM cursors WHERE handle=?", (handle,)).fetchone()
            after = cur["consolidated_rowid"] if cur else 0
            rows = c.execute(
                "SELECT rowid, id, direction, text, created_at FROM messages WHERE handle=? AND rowid>? "
                "ORDER BY rowid LIMIT ?", (handle, after, BATCH)).fetchall()
            if not rows:
                return False
            ocr = {r["message_id"]: r["ocr_text"] for r in c.execute(
                "SELECT message_id, ocr_text FROM attachments WHERE handle=? AND ocr_text IS NOT NULL AND ocr_text != ''",
                (handle,))}
            facts = c.execute("SELECT key, value FROM facts WHERE handle=? AND status='active'", (handle,)).fetchall()
            prior = c.execute("SELECT text FROM summaries WHERE handle=?", (handle,)).fetchone()

        lines = []
        for r in rows:
            who = "USER" if r["direction"] == "in" else "ASSISTANT"
            lines.append(f"[m{r['rowid']}] {r['created_at']} {who}: {r['text']}")
            if r["id"] in ocr:
                lines.append(f"  (image text: {ocr[r['id']][:1500]})")
        user_block = (
            "Existing profile facts:\n" + ("\n".join(f"- {f['key']}: {f['value']}" for f in facts) or "(none)")
            + "\n\nPrevious summary:\n" + (prior["text"] if prior else "(none)")
            + "\n\nNew messages:\n" + "\n".join(lines)
        )
        text, _ = await self.llm.chat(
            [{"role": "system", "content": CONSOLIDATE_PROMPT}, {"role": "user", "content": user_block}],
            self.s.memory_models, max_tokens=1800, temperature=0.0, json_mode=True,
        )
        try:
            data = extract_json(text)
        except (ValueError, TypeError) as exc:
            log.warning("consolidation returned unusable JSON (%s); batch will be retried", exc)
            return False

        by_ref = {f"m{r['rowid']}": r["id"] for r in rows}

        def sources(item: dict) -> list[str]:
            return [by_ref[s] for s in (item.get("source") or []) if isinstance(s, str) and s in by_ref]

        with self.db.conn() as c:
            for e in (data.get("events") or [])[:12]:
                if not isinstance(e, dict):
                    continue
                etext = str(e.get("text") or "").strip()[:500]
                src = sources(e)
                if len(etext) < 8 or not src:
                    continue
                entities = [str(x)[:60] for x in (e.get("entities") or []) if isinstance(x, str)][:8]
                when = str(e["when"])[:60] if e.get("when") else None
                cur = c.execute(
                    "INSERT INTO events(handle, text, entities, when_hint, source_ids, created_at) VALUES(?,?,?,?,?,?)",
                    (handle, etext, json.dumps(entities), when, json.dumps(src), now_iso()),
                )
                c.execute("INSERT INTO events_fts(text, event_id, handle) VALUES(?,?,?)", (etext, cur.lastrowid, handle))

            for f in (data.get("facts") or [])[:8]:
                if not isinstance(f, dict):
                    continue
                action = f.get("action")
                key = _slug(str(f.get("key") or ""))
                value = str(f.get("value") or "").strip()[:300]
                src = sources(f)
                if action not in ("add", "update", "retract") or not key or not src:
                    continue
                current = c.execute(
                    "SELECT id, value FROM facts WHERE handle=? AND key=? AND status='active'", (handle, key)
                ).fetchone()
                if action == "retract":
                    if current:
                        c.execute("UPDATE facts SET status='retracted' WHERE id=?", (current["id"],))
                    continue
                if not value or (current and current["value"].strip().lower() == value.lower()):
                    continue
                new_id = c.execute(
                    "INSERT INTO facts(handle, key, value, source_ids, created_at) VALUES(?,?,?,?,?)",
                    (handle, key, value, json.dumps(src), now_iso()),
                ).lastrowid
                if current:
                    c.execute("UPDATE facts SET status='superseded', superseded_by=? WHERE id=?",
                              (new_id, current["id"]))

            summary = str(data.get("summary") or "").strip()[:1200]
            if summary:
                c.execute(
                    "INSERT INTO summaries(handle, text, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(handle) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                    (handle, summary, now_iso()),
                )
            c.execute(
                "INSERT INTO cursors(handle, consolidated_rowid) VALUES(?,?) "
                "ON CONFLICT(handle) DO UPDATE SET consolidated_rowid=excluded.consolidated_rowid",
                (handle, rows[-1]["rowid"]),
            )
        await self._embed_missing(handle)
        return True

    async def _embed_missing(self, handle: str, limit: int = 24) -> None:
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT id, text FROM events WHERE handle=? AND embedding IS NULL AND status='active' LIMIT ?",
                (handle, limit),
            ).fetchall()
        if not rows:
            return
        try:
            vecs = await self.llm.embed([r["text"] for r in rows])
        except LLMError as exc:
            log.info("embedding skipped: %s", exc)
            return
        with self.db.conn() as c:
            for r, v in zip(rows, vecs):
                c.execute("UPDATE events SET embedding=? WHERE id=?", (_pack(v), r["id"]))

    # ---------------------------------------------------------- prompt render
    def render(self, handle: str, recalled: dict) -> str:
        facts = self.active_facts(handle)
        lines = ["<memory>", "<profile>"]
        lines += [f"- {f['key'].replace('_', ' ')}: {f['value']}" for f in facts] or ["(nothing yet)"]
        lines += ["</profile>", "<summary>", self.get_summary(handle) or "(none yet)", "</summary>"]
        if recalled["events"] or recalled["messages"]:
            lines.append("<recalled>")
            for e in recalled["events"]:
                when = f" (when: {e['when']})" if e.get("when") else ""
                lines.append(f"- [{fmt_ts(e['at'])}] {e['text']}{when}")
            for m in recalled["messages"]:
                lines.append(f"- earlier, {m['who']} said [{fmt_ts(m['at'])}]: {m['text']}")
            lines.append("</recalled>")
        lines.append("</memory>")
        return "\n".join(lines)
