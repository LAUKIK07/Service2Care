"""The one place that sends iMessages.

What it adds on top of LinqClient:
- an outbox row per send, keyed by an idempotency key, so a retry, a restart or a repeated webhook never sends twice;
- the sent message is recorded in the message log with its kind, the event (alert) it belongs to and what it replied to;
- iMessage-only extras (bold, effects, polls) are used only when the recipient can receive them;
- a local sliding-window limit that stays under Linq's 30 messages per 60 seconds per recipient.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from .linq import LinqClient, LinqError, text_part, utf16_range
from .memory import Memory
from .textutil import now_iso

log = logging.getLogger("s2c.messenger")

WINDOW_S = 60
WINDOW_MAX = 24            # Linq allows 30 per pair per minute; stay clear of it


class Messenger:
    def __init__(self, memory: Memory, linq: LinqClient, use_polls: bool = False):
        self.memory = memory
        self.linq = linq
        self.use_polls = use_polls
        self.on_delivery = None     # (message_id, state, reason) -> None
        self._sent: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self.on_thread = None       # (alert_id, mid, direction, text, kind, at, options) -> None; feeds the dashboard conversation

    # ---------------------------------------------------------------- helpers
    def chat_id_for(self, handle: str) -> str | None:
        with self.memory.db.conn() as c:
            r = c.execute("SELECT chat_id FROM messages WHERE handle=? AND chat_id IS NOT NULL AND chat_id != '' "
                          "ORDER BY rowid DESC LIMIT 1", (handle,)).fetchone()
        return r["chat_id"] if r else None

    async def _throttle(self, handle: str) -> None:
        async with self._lock:
            now = time.monotonic()
            window = [t for t in self._sent.get(handle, []) if now - t < WINDOW_S]
            if len(window) >= WINDOW_MAX:
                await asyncio.sleep(max(0.5, WINDOW_S - (now - window[0])))
                now = time.monotonic()
                window = [t for t in window if now - t < WINDOW_S]
            window.append(time.monotonic())
            self._sent[handle] = window

    def _outbox_get(self, key: str):
        with self.memory.db.conn() as c:
            return c.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()

    def _outbox_put(self, key: str, handle: str, kind: str, payload: dict, status: str, *, message_id: str | None = None, error: str | None = None) -> None:
        now = now_iso()
        with self.memory.db.conn() as c:
            c.execute(
                "INSERT INTO outbox(key,handle,kind,payload,status,attempts,message_id,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,?,?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status, "
                "attempts=outbox.attempts+1, message_id=COALESCE(excluded.message_id, outbox.message_id), "
                "error=excluded.error, updated_at=excluded.updated_at",
                (key, handle, kind, json.dumps(payload)[:4000], status, message_id, error, now, now))

    def _record(self, handle: str, resp: dict, text: str, *, kind: str, alert_id: int | None, reply_to: str | None) -> str:
        msg = resp.get("message") or {}
        mid = str(msg.get("id") or f"out-{time.time_ns()}")
        self.memory.record_message(msg_id=mid, event_id=None, chat_id=str(resp.get("chat_id") or ""), handle=handle,
                                   direction="out", text=text, created_at=now_iso())
        with self.memory.db.conn() as c:
            c.execute("UPDATE messages SET kind=?, alert_id=?, reply_to=?, delivery='sent' WHERE id=?", (kind, alert_id, reply_to, mid))
        if alert_id and self.on_thread:
            self.on_thread(alert_id, mid, "out", text, kind, now_iso(), None)
        return mid

    # ------------------------------------------------------------------ sends
    async def say(self, handle: str, text: str, *, key: str | None = None, kind: str = "text", alert_id: int | None = None,
                  reply_to: str | None = None, effect: tuple[str, str] | None = None, bold: list[str] | None = None,
                  record_text: str | None = None, record: bool = True) -> str | None:
        """Send text. Returns the Linq message id, or None if it could not be sent.

        `bold` lists substrings to make bold. Effects and bold need iMessage and are dropped otherwise.
        `key` makes the send safe to repeat: the same key never produces a second message.
        """
        key = key or f"auto:{uuid.uuid4()}"
        prior = self._outbox_get(key)
        if prior and prior["status"] == "sent" and prior["message_id"]:
            log.info("send skipped, already sent (%s)", key[:40])
            return prior["message_id"]
        rich = bool(bold or effect) and await self.linq.imessage_available(handle)
        ranges = [r for r in (utf16_range(text, b) for b in (bold or [])) if r] if rich else []
        parts = [text_part(text, bold=ranges)]
        await self._throttle(handle)
        try:
            resp = await self.linq.send(handle, parts, chat_id=self.chat_id_for(handle), reply_to=reply_to,
                                        effect=effect if rich else None, idempotency_key=key)
        except LinqError as exc:
            log.error("send failed (%s): %s", kind, exc)
            self._outbox_put(key, handle, kind, {"text": text[:300]}, "failed", error=str(exc)[:200])
            return None
        if record:
            mid = self._record(handle, resp, record_text or text, kind=kind, alert_id=alert_id, reply_to=reply_to)
        else:                                       # notices that must not count as an answer to the person's last message
            mid = str((resp.get("message") or {}).get("id") or f"out-{time.time_ns()}")
        self._outbox_put(key, handle, kind, {"text": text[:300]}, "sent", message_id=mid)
        return mid

    async def react(self, handle: str, message_id: str, kind: str = "like", *, emoji: str | None = None) -> bool:
        """A tapback on one of the person's messages. Best effort; never raises."""
        try:
            await self.linq.react(message_id, kind, emoji=emoji)
            return True
        except Exception as exc:
            log.info("tapback skipped: %s", exc)
            return False

    async def card(self, handle: str, png: bytes, caption: str | None = None, *, key: str | None = None, kind: str = "card",
                   alert_id: int | None = None, reply_to: str | None = None, filename: str = "card.png",
                   bold: list[str] | None = None) -> str | None:
        """Send an image card with an optional caption. The caption is stored in the log, the image itself is not."""
        key = key or f"auto:{uuid.uuid4()}"
        prior = self._outbox_get(key)
        if prior and prior["status"] == "sent" and prior["message_id"]:
            return prior["message_id"]
        await self._throttle(handle)
        try:
            rich = bool(bold) and bool(caption) and await self.linq.imessage_available(handle)
            ranges = [r for r in (utf16_range(caption, b) for b in (bold or [])) if r] if rich else []
            resp = await self.linq.send_image(handle, png, caption=caption, bold=ranges, filename=filename,
                                              chat_id=self.chat_id_for(handle), reply_to=reply_to, idempotency_key=key)
        except LinqError as exc:
            log.error("card send failed: %s", exc)
            self._outbox_put(key, handle, kind, {"caption": (caption or "")[:300]}, "failed", error=str(exc)[:200])
            return None
        mid = self._record(handle, resp, (caption or "") + "\n[card image]", kind=kind, alert_id=alert_id, reply_to=reply_to)
        self._outbox_put(key, handle, kind, {"caption": (caption or "")[:300]}, "sent", message_id=mid)
        return mid

    async def link(self, handle: str, url: str, *, key: str | None = None) -> str | None:
        """A rich link preview. The URL is not written to the message log (it can carry a sign-in key)."""
        key = key or f"auto:{uuid.uuid4()}"
        prior = self._outbox_get(key)
        if prior and prior["status"] == "sent" and prior["message_id"]:
            return prior["message_id"]
        await self._throttle(handle)
        try:
            resp = await self.linq.send_link(handle, url, chat_id=self.chat_id_for(handle), idempotency_key=key)
        except LinqError as exc:
            log.error("link send failed: %s", exc)
            self._outbox_put(key, handle, "link", {}, "failed", error=str(exc)[:200])
            return None
        mid = self._record(handle, resp, "[dashboard link]", kind="link", alert_id=None, reply_to=None)
        self._outbox_put(key, handle, "link", {}, "sent", message_id=mid)
        return mid

    async def ask(self, handle: str, question: str, options: list[str], *, purpose: str, ref: str | None = None,
                  key: str | None = None, alert_id: int | None = None, reply_to: str | None = None) -> str | None:
        """Send a question followed by a tap-to-answer poll. Falls back to a plain question if polls are unavailable.

        The vote arrives as a webhook and is turned into a normal reply by the agent (see Agent.on_poll_vote).
        Returns the id of the question message, or None if nothing was sent.
        """
        key = key or f"auto:{uuid.uuid4()}"
        poll_ok = bool(self.use_polls and len(options) >= 2 and self.chat_id_for(handle) and await self.linq.imessage_available(handle))
        if not poll_ok and len(options) >= 2:
            # one message with the choices in words; a typed answer is matched to them
            text = question + "\n\nReply with: " + " / ".join(options)
            qid = await self.say(handle, text, key=f"{key}:q", kind="question", alert_id=alert_id, reply_to=reply_to)
            if qid:
                with self.memory.db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO pending_polls(message_id,handle,purpose,ref,options,created_at) VALUES(?,?,?,?,?,?)",
                              (f"txt-{key}", handle, purpose, ref, json.dumps({o: o for o in options}), now_iso()))
            return qid
        qid = await self.say(handle, question, key=f"{key}:q", kind="question", alert_id=alert_id, reply_to=reply_to)
        if not qid:
            return None
        await self.poll(handle, options, purpose=purpose, ref=ref, key=key, alert_id=alert_id)
        return qid

    async def poll(self, handle: str, options: list[str], *, purpose: str, ref: str | None = None, key: str | None = None,
                   alert_id: int | None = None) -> str | None:
        """Only the tap-to-answer part (the question was already sent). Returns the poll message id, or None."""
        key = key or f"auto:{uuid.uuid4()}"
        qid = "ok"
        if len(options) < 2:
            return None
        chat_id = self.chat_id_for(handle)
        if not self.use_polls or not chat_id or not await self.linq.imessage_available(handle):
            # Typed answers: the options are shown in words and a typed "yes" or "not sure" is matched to them (see Agent._typed_answer).
            await self.say(handle, "Reply with: " + " / ".join(options), key=f"{key}:hint", kind="question", alert_id=alert_id)
            with self.memory.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO pending_polls(message_id,handle,purpose,ref,options,created_at) VALUES(?,?,?,?,?,?)",
                          (f"txt-{key}", handle, purpose, ref, json.dumps({o: o for o in options}), now_iso()))
            return None
        prior = self._outbox_get(f"{key}:poll")
        if prior and prior["status"] == "sent":
            return prior["message_id"]
        await self._throttle(handle)
        try:
            resp = await self.linq.poll(chat_id, options, idempotency_key=f"{key}:poll")
        except LinqError as exc:
            log.warning("poll failed, asking in words instead: %s", exc)
            self._outbox_put(f"{key}:poll", handle, "poll", {"options": options}, "failed", error=str(exc)[:200])
            await self.say(handle, "Reply with: " + " / ".join(options), key=f"{key}:hint", kind="question", alert_id=alert_id)
            return None
        poll = resp.get("poll") or {}
        mapping = {str(o.get("id")): str(o.get("text")) for o in (poll.get("options") or []) if o.get("id")}
        pmid = str(resp.get("message_id") or "")
        if pmid and not mapping:
            try:
                got = await self.linq.get_poll(pmid)
                mapping = {str(o.get("id")): str(o.get("text")) for o in ((got.get("poll") or {}).get("options") or []) if o.get("id")}
            except Exception:
                pass
        if pmid:
            with self.memory.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO pending_polls(message_id,handle,purpose,ref,options,created_at) VALUES(?,?,?,?,?,?)",
                          (pmid, handle, purpose, ref, json.dumps(mapping or {o: o for o in options}), now_iso()))
            shown = "[poll] " + " | ".join(options)
            self.memory.record_message(msg_id=pmid, event_id=None, chat_id=chat_id, handle=handle, direction="out", text=shown, created_at=now_iso())
            with self.memory.db.conn() as c:
                c.execute("UPDATE messages SET kind='poll', alert_id=? WHERE id=?", (alert_id, pmid))
                if alert_id:
                    c.execute("UPDATE alerts SET poll_message_id=? WHERE id=?", (pmid, alert_id))
            if alert_id and self.on_thread:
                self.on_thread(alert_id, pmid, "out", shown, "poll", now_iso(), options)
        self._outbox_put(f"{key}:poll", handle, "poll", {"options": options}, "sent", message_id=pmid or None)
        return pmid or None

    # ------------------------------------------------------------- receipts
    def on_receipt(self, message_id: str, state: str, reason: str | None = None) -> None:
        if self.on_delivery:
            try:
                self.on_delivery(message_id, state, reason)
            except Exception:
                log.exception("delivery hook failed")
        with self.memory.db.conn() as c:
            c.execute("UPDATE messages SET delivery=? WHERE id=? AND (delivery IS NULL OR delivery != 'read' OR ?='read')",
                      (state, message_id, state))
            if state == "failed":
                c.execute("UPDATE outbox SET status='failed', error=? WHERE message_id=?", (reason or "delivery failed", message_id))
                c.execute("UPDATE alerts SET delivery='failed' WHERE message_id=?", (message_id,))
            else:
                c.execute("UPDATE alerts SET delivery=? WHERE message_id=? AND COALESCE(delivery,'') != 'read'", (state, message_id))
