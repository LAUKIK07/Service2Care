"""Things a person controls about what we hold: consent, pause, their health notes, and removing or correcting items.

Forgetting is real deletion with a check afterwards. "Forget" clears the fact (every version of it), the events that cite the
same source messages, the search index rows, the embeddings (they live on the event rows), the source messages (their text is
replaced and their index rows removed), attached files, any saved check-in built from those messages, and the rolling summary
(which is rebuilt from what is left). The result lists anything that could not be removed, and the caller must not tell
the person it is gone unless that list is empty.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .llm import LLMError
from .textutil import now_iso

log = logging.getLogger("s2c.memctl")
REDACTED = "[removed at your request]"
LABELS = {"preferred_name": "Name", "allergies": "Allergies", "surgeries": "Surgeries", "lung_conditions": "Lungs or breathing",
          "other_health_notes": "Other health notes", "message_style": "How you like messages", "reported_bp": "Blood pressure you reported"}


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class ControlMixin:
    # ------------------------------------------------------------ state flags
    def user_row(self, handle: str):
        with self.db.conn() as c:
            return c.execute("SELECT * FROM users WHERE handle=?", (handle,)).fetchone()

    def has_consent(self, handle: str) -> bool:
        r = self.user_row(handle)
        return bool(r and r["consent"])

    def set_consent(self, handle: str, value: bool) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE users SET consent=?, consent_at=? WHERE handle=?", (int(value), now_iso() if value else None, handle))

    def onboarding(self, handle: str) -> tuple[str, int]:
        r = self.user_row(handle)
        return (r["onboarding"], r["ob_step"]) if r else ("new", 0)

    def set_onboarding(self, handle: str, state: str, step: int | None = None) -> None:
        with self.db.conn() as c:
            if step is None:
                c.execute("UPDATE users SET onboarding=? WHERE handle=?", (state, handle))
            else:
                c.execute("UPDATE users SET onboarding=?, ob_step=? WHERE handle=?", (state, step, handle))

    def pause(self, handle: str, hours: float) -> str:
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        with self.db.conn() as c:
            c.execute("UPDATE users SET paused_until=? WHERE handle=?", (until, handle))
        return until

    def resume(self, handle: str) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE users SET paused_until=NULL WHERE handle=?", (handle,))

    def is_paused(self, handle: str) -> bool:
        r = self.user_row(handle)
        if not r or not r["paused_until"]:
            return False
        return datetime.fromisoformat(r["paused_until"]) > datetime.now(timezone.utc)

    # ----------------------------------------------------------------- facts
    def set_fact(self, handle: str, key: str, value: str, source_ids: list[str], origin: str) -> int:
        """Store a fact. An earlier active fact with the same key is superseded (kept as history)."""
        with self.db.conn() as c:
            cur = c.execute("SELECT id FROM facts WHERE handle=? AND key=? AND status='active'", (handle, key)).fetchone()
            new_id = c.execute("INSERT INTO facts(handle,key,value,source_ids,created_at,origin) VALUES(?,?,?,?,?,?)",
                               (handle, key, value[:300], json.dumps(source_ids), now_iso(), origin)).lastrowid
            if cur:
                c.execute("UPDATE facts SET status='superseded', superseded_by=? WHERE id=?", (new_id, cur["id"]))
        return new_id

    def fact_value(self, handle: str, key: str) -> str | None:
        with self.db.conn() as c:
            r = c.execute("SELECT value FROM facts WHERE handle=? AND key=? AND status='active'", (handle, key)).fetchone()
        return r["value"] if r else None

    def profile_items(self, handle: str) -> list[dict]:
        """The person's health notes as label/value pairs, for cards, the dashboard and Jev."""
        with self.db.conn() as c:
            rows = c.execute("SELECT id,key,value,origin,created_at FROM facts WHERE handle=? AND status='active' "
                             "AND key IN (%s) ORDER BY id" % ",".join("?" * len(LABELS)), (handle, *LABELS)).fetchall()
        return [{"id": r["id"], "key": r["key"], "label": LABELS[r["key"]], "value": r["value"], "origin": r["origin"],
                 "at": r["created_at"]} for r in rows]

    def risk_flags(self, handle: str) -> list[str]:
        """Plain flags from what the person told us. They change which question we ask, never what we conclude."""
        flags = []
        none = re.compile(r"^(none|no|nothing|n/?a)\b", re.I)
        for key, flag in (("lung_conditions", "lung_condition"), ("allergies", "allergies"), ("surgeries", "past_surgery")):
            v = self.fact_value(handle, key)
            if v and not none.match(v.strip()):
                flags.append(flag)
        other = (self.fact_value(handle, "other_health_notes") or "").lower()
        if re.search(r"heart|cardiac|blood pressure|hypertens", other):
            flags.append("heart_or_bp_note")
        return flags

    # ------------------------------------------------------ numbered listing
    def list_items(self, handle: str, limit: int = 14) -> list[dict]:
        items: list[dict] = []
        with self.db.conn() as c:
            for r in c.execute("SELECT id,key,value,origin FROM facts WHERE handle=? AND status='active' ORDER BY id", (handle,)):
                items.append({"ref": f"fact:{r['id']}", "text": f"{LABELS.get(r['key'], r['key'].replace('_', ' ').capitalize())}: {r['value']}"})
            for r in c.execute("SELECT a.alert_id, a.summary, a.metric, a.event_id FROM annotations a JOIN alerts l ON l.id=a.alert_id "
                               "WHERE l.handle=? AND a.summary IS NOT NULL AND a.summary != '' ORDER BY a.alert_id DESC LIMIT 5", (handle,)):
                items.append({"ref": f"story:{r['alert_id']}", "text": f"Check-in: {r['summary']}"})
            for r in c.execute("SELECT id,text FROM events WHERE handle=? AND status='active' ORDER BY id DESC LIMIT 6", (handle,)):
                items.append({"ref": f"event:{r['id']}", "text": r["text"]})
        for n, it in enumerate(items[:limit], start=1):
            it["n"] = n
        return items[:limit]

    def render_items(self, items: list[dict]) -> str:
        if not items:
            return "I do not have any notes about you yet."
        lines = ["Here is what I remember:", ""]
        lines += [f"{it['n']}. {it['text'][:160]}" for it in items]
        lines += ["", "To remove one, say \"forget 2\". To fix one, say \"correct 2: the right answer\". "
                      "To erase everything, say \"forget everything\"."]
        return "\n".join(lines)

    def remember_listing(self, handle: str, refs: list[str]) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE users SET last_ref=? WHERE handle=?", (json.dumps(refs), handle))

    def last_refs(self, handle: str) -> list[str]:
        r = self.user_row(handle)
        return _loads(r["last_ref"], []) if r else []

    # -------------------------------------------------------------- forgetting
    def _source_ids(self, c, handle: str, ref: str) -> tuple[list[str], dict]:
        kind, _, rid = ref.partition(":")
        out: dict = {"fact_ids": [], "event_ids": [], "alert_ids": [], "needles": []}
        if kind == "fact":
            row = c.execute("SELECT key, source_ids FROM facts WHERE id=? AND handle=?", (rid, handle)).fetchone()
            if not row:
                return [], out
            same = c.execute("SELECT id, source_ids, value FROM facts WHERE handle=? AND key=?", (handle, row["key"])).fetchall()
            out["fact_ids"] = [r["id"] for r in same]
            out["needles"] = [r["value"] for r in same if len(r["value"].strip()) >= 4 and not r["value"].lower().startswith("none")]
            ids = [i for r in same for i in _loads(r["source_ids"], [])]
            return list(dict.fromkeys(ids)), out
        if kind == "event":
            row = c.execute("SELECT source_ids FROM events WHERE id=? AND handle=?", (rid, handle)).fetchone()
            if not row:
                return [], out
            out["event_ids"] = [int(rid)]
            return _loads(row["source_ids"], []), out
        if kind == "story":
            ids = [r["id"] for r in c.execute("SELECT id FROM messages WHERE handle=? AND alert_id=? AND direction='in'", (handle, rid))]
            out["alert_ids"] = [int(rid)]
            return ids, out
        return [], out

    async def forget_ref(self, handle: str, ref: str) -> dict:
        """Remove one item and everything derived from it. Returns a report; `leftovers` must be empty to claim success."""
        with self.db.conn() as c:
            msg_ids, out = self._source_ids(c, handle, ref)
            if not (out["fact_ids"] or out["event_ids"] or out["alert_ids"]):
                return {"ok": False, "reason": "not found", "leftovers": ["item not found"], "alert_ids": []}
            # everything that cites the same source messages goes with it
            for mid in msg_ids:
                for r in c.execute("SELECT id, source_ids FROM events WHERE handle=?", (handle,)):
                    if mid in _loads(r["source_ids"], []) and r["id"] not in out["event_ids"]:
                        out["event_ids"].append(r["id"])
                for r in c.execute("SELECT id, source_ids FROM facts WHERE handle=?", (handle,)):
                    if mid in _loads(r["source_ids"], []) and r["id"] not in out["fact_ids"]:
                        out["fact_ids"].append(r["id"])
                for r in c.execute("SELECT alert_id FROM messages WHERE id=? AND alert_id IS NOT NULL", (mid,)):
                    if r["alert_id"] not in out["alert_ids"]:
                        out["alert_ids"].append(r["alert_id"])
            # our own messages that repeat the removed text (the memory list, confirmations, cards) are copies of it too
            for needle in out["needles"]:
                for r in c.execute("SELECT id FROM messages WHERE handle=? AND text LIKE ? AND text != ?", (handle, f"%{needle}%", REDACTED)):
                    if r["id"] not in msg_ids:
                        msg_ids.append(r["id"])
                c.execute("DELETE FROM outbox WHERE handle=? AND payload LIKE ?", (handle, f"%{needle}%"))
                c.execute("DELETE FROM pending_confirms WHERE handle=? AND payload LIKE ?", (handle, f"%{needle}%"))
            files = []
            for mid in msg_ids:
                files += [r["path"] for r in c.execute("SELECT path FROM attachments WHERE message_id=? AND path IS NOT NULL", (mid,))]
            for fid in out["fact_ids"]:
                c.execute("DELETE FROM facts WHERE id=? AND handle=?", (fid, handle))
            for eid in out["event_ids"]:
                c.execute("DELETE FROM events_fts WHERE event_id=?", (eid,))          # search index
                c.execute("DELETE FROM events WHERE id=? AND handle=?", (eid, handle))  # the embedding lives on this row
            for mid in msg_ids:
                c.execute("DELETE FROM messages_fts WHERE msg_id=?", (mid,))
                c.execute("DELETE FROM attachments WHERE message_id=?", (mid,))
                c.execute("UPDATE messages SET text=? WHERE id=? AND handle=?", (REDACTED, mid, handle))
            for aid in out["alert_ids"]:
                c.execute("DELETE FROM annotations WHERE alert_id=?", (aid,))
                c.execute("DELETE FROM feed WHERE kind='annotation' AND data LIKE ?", (f'%"alert_id": {aid}%',))
        for p in files:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        await self.rebuild_summary(handle)
        left = self.verify_gone(handle, msg_ids, out)
        return {"ok": not left, "leftovers": left, "removed": {k: len(v) for k, v in out.items()}, "alert_ids": out["alert_ids"],
                "message_ids": msg_ids}

    def verify_gone(self, handle: str, msg_ids: list[str], out: dict) -> list[str]:
        left: list[str] = []
        with self.db.conn() as c:
            for needle in out.get("needles", []):
                if c.execute("SELECT 1 FROM messages WHERE handle=? AND text LIKE ?", (handle, f"%{needle}%")).fetchone():
                    left.append("a message that repeats it")
                if c.execute("SELECT 1 FROM messages_fts WHERE handle=? AND text LIKE ?", (handle, f"%{needle}%")).fetchone():
                    left.append("a search entry that repeats it")
                if c.execute("SELECT 1 FROM outbox WHERE handle=? AND payload LIKE ?", (handle, f"%{needle}%")).fetchone():
                    left.append("a queued copy")
            for fid in out["fact_ids"]:
                if c.execute("SELECT 1 FROM facts WHERE id=?", (fid,)).fetchone():
                    left.append(f"fact {fid}")
            for eid in out["event_ids"]:
                if c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone() or c.execute("SELECT 1 FROM events_fts WHERE event_id=?", (eid,)).fetchone():
                    left.append(f"event {eid}")
            for mid in msg_ids:
                if c.execute("SELECT 1 FROM messages_fts WHERE msg_id=?", (mid,)).fetchone():
                    left.append(f"search index for message {mid[:8]}")
                r = c.execute("SELECT text FROM messages WHERE id=?", (mid,)).fetchone()
                if r and r["text"] != REDACTED:
                    left.append(f"message {mid[:8]}")
                if c.execute("SELECT 1 FROM attachments WHERE message_id=?", (mid,)).fetchone():
                    left.append(f"attachment of {mid[:8]}")
            for aid in out["alert_ids"]:
                if c.execute("SELECT 1 FROM annotations WHERE alert_id=?", (aid,)).fetchone():
                    left.append(f"saved check-in {aid}")
        return left

    async def rebuild_summary(self, handle: str) -> None:
        """Write the rolling summary again from the notes that remain, so a removed item cannot survive inside it."""
        facts = [f"{f['key'].replace('_', ' ')}: {f['value']}" for f in self.active_facts(handle)]
        with self.db.conn() as c:
            events = [r["text"] for r in c.execute("SELECT text FROM events WHERE handle=? AND status='active' ORDER BY id DESC LIMIT 20", (handle,))]
        if not facts and not events:
            with self.db.conn() as c:
                c.execute("DELETE FROM summaries WHERE handle=?", (handle,))
            return
        prompt = [{"role": "system", "content": "Write a neutral orientation note of at most 700 characters from ONLY the notes below. "
                   "Do not add anything that is not in them. Plain text."},
                  {"role": "user", "content": "Facts:\n" + "\n".join(facts) + "\n\nEvents:\n" + "\n".join(events)}]
        try:
            text, _ = await self.llm.chat(prompt, self.s.memory_models, max_tokens=300, temperature=0.0)
            text = text.strip()[:900]
        except LLMError:
            text = ""                              # safer to hold nothing than to keep a summary that may still mention it
        with self.db.conn() as c:
            if text:
                c.execute("INSERT INTO summaries(handle,text,updated_at) VALUES(?,?,?) ON CONFLICT(handle) DO UPDATE SET "
                          "text=excluded.text, updated_at=excluded.updated_at", (handle, text, now_iso()))
            else:
                c.execute("DELETE FROM summaries WHERE handle=?", (handle,))

    # --------------------------------------------------------------- correcting
    def correct_ref(self, handle: str, ref: str, new_text: str, source_id: str) -> dict:
        """Replace a fact or a saved check-in with what the person says is right. Earlier values stay as history."""
        kind, _, rid = ref.partition(":")
        new_text = new_text.strip()[:300]
        with self.db.conn() as c:
            if kind == "fact":
                row = c.execute("SELECT key FROM facts WHERE id=? AND handle=?", (rid, handle)).fetchone()
                if not row:
                    return {"ok": False}
                key = row["key"]
            elif kind == "story":
                row = c.execute("SELECT a.summary, a.versions FROM annotations a JOIN alerts l ON l.id=a.alert_id WHERE a.alert_id=? AND l.handle=?",
                                (rid, handle)).fetchone()
                if not row:
                    return {"ok": False}
                versions = _loads(row["versions"], [])
                versions.append({"v": len(versions) + 1, "kind": "user_correction", "text": new_text, "at": now_iso(), "changed": True,
                                 "before": row["summary"]})
                c.execute("UPDATE annotations SET summary=?, versions=?, updated_at=? WHERE alert_id=?",
                          (new_text, json.dumps(versions), now_iso(), rid))
                return {"ok": True, "kind": "story", "alert_id": int(rid)}
            else:
                return {"ok": False}
        self.set_fact(handle, key, new_text, [source_id], "correction")
        return {"ok": True, "kind": "fact", "key": key}
