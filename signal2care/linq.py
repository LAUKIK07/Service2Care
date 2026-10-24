"""Linq (iMessage) client and webhook parsing.

Everything the Linq Partner API v3 offers that this product uses is wrapped here: threaded replies, tapbacks,
polls, screen and bubble effects, bold/italic text, images (pre-uploaded), read receipts, typing indicators
and idempotent sends. Request shapes follow https://cdn.linqapp.com/openapi/linq-api-v3.yaml.

Retries are only done when they cannot create a duplicate: a send is retried on a network error or a 5xx only if
it carries an idempotency key (Linq then returns the original result instead of sending again). A 429 waits for
Retry-After and tries again, because Linq has not accepted the message.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from .config import normalize_phone

log = logging.getLogger("s2c.linq")

TAPBACKS = {"love", "like", "dislike", "laugh", "emphasize", "question"}
SCREEN_EFFECTS = {"confetti", "fireworks", "lasers", "sparkles", "celebration", "hearts", "love", "balloons", "happy_birthday", "echo", "spotlight"}
BUBBLE_EFFECTS = {"slam", "loud", "gentle", "invisible"}


class LinqError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.retry_after = retry_after


# ----------------------------------------------------------------------------- inbound events
@dataclass
class Inbound:
    event_id: str
    message_id: str
    chat_id: str
    handle: str
    sent_at: str | None
    text: str
    media: list[dict] = field(default_factory=list)
    reply_to: str | None = None


@dataclass
class Reaction:
    event_id: str
    handle: str
    chat_id: str
    message_id: str
    kind: str                 # love | like | dislike | laugh | emphasize | question | custom | sticker
    emoji: str | None
    added: bool


@dataclass
class PollVote:
    event_id: str
    handle: str
    chat_id: str
    message_id: str           # the poll message
    option_id: str
    added: bool


@dataclass
class Receipt:
    event_id: str
    message_id: str
    state: str                # delivered | read | failed
    at: str | None
    reason: str | None = None
    code: int | None = None


def _normalize_handle(raw: str) -> str:
    raw = (raw or "").strip()
    if "@" in raw:
        return raw.lower()
    return normalize_phone(raw) or raw


def parse_event(body: dict):
    """Turn a webhook body into one of Inbound, Reaction, PollVote, Receipt, or None for events we ignore."""
    kind = body.get("event_type")
    data = body.get("data") or {}
    event_id = str(body.get("event_id") or "")
    if kind == "message.received":
        return parse_inbound(body)
    if kind in ("reaction.added", "reaction.removed"):
        if data.get("is_from_me"):
            return None
        handle = _normalize_handle((data.get("from_handle") or {}).get("handle") or data.get("from") or "")
        if not handle or not data.get("message_id"):
            return None
        return Reaction(event_id, handle, str(data.get("chat_id") or ""), str(data["message_id"]),
                        str(data.get("reaction_type") or "custom"), data.get("custom_emoji"), kind.endswith("added"))
    if kind in ("poll.vote.added", "poll.vote.removed"):
        sender = data.get("sender_handle") or {}
        if sender.get("is_me") or data.get("direction") == "outbound":
            return None
        handle = _normalize_handle(sender.get("handle") or "")
        if not handle or not data.get("option_id"):
            return None
        return PollVote(event_id, handle, str((data.get("chat") or {}).get("id") or ""), str(data.get("message_id") or ""),
                        str(data["option_id"]), kind.endswith("added"))
    if kind in ("message.delivered", "message.read"):
        mid = data.get("id") or data.get("message_id")
        if not mid:
            return None
        state = "read" if kind.endswith("read") else "delivered"
        return Receipt(event_id, str(mid), state, data.get("read_at" if state == "read" else "delivered_at"))
    if kind == "message.failed":
        mid = data.get("message_id") or data.get("id")
        if not mid:
            return None
        return Receipt(event_id, str(mid), "failed", data.get("failed_at"), data.get("reason"), data.get("code"))
    return None


def parse_inbound(body: dict) -> Inbound | None:
    """Return an Inbound for message.received events, else None."""
    if body.get("event_type") != "message.received":
        return None
    data = body.get("data") or {}
    msg = data["message"] if isinstance(data.get("message"), dict) else data   # 2025-01-01 payloads nest the message
    if msg.get("direction") not in (None, "inbound"):
        return None
    sender = msg.get("sender_handle") or data.get("sender_handle") or {}
    if sender.get("is_me"):
        return None
    handle = _normalize_handle(sender.get("handle") or msg.get("from") or "")
    if not handle:
        return None
    chat = data.get("chat") or msg.get("chat") or {}
    parts = msg.get("parts") or []
    text = "\n".join((p.get("value") or "") for p in parts if p.get("type") == "text").strip()
    media = [p for p in parts if p.get("type") == "media"]
    message_id = str(msg.get("id") or body.get("event_id") or "")
    if not message_id:
        return None
    reply = msg.get("reply_to") or {}
    return Inbound(
        event_id=str(body.get("event_id") or message_id),
        message_id=message_id,
        chat_id=str(chat.get("id") or msg.get("chat_id") or ""),
        handle=handle,
        sent_at=msg.get("sent_at") or body.get("created_at"),
        text=text,
        media=media,
        reply_to=str(reply.get("message_id")) if reply.get("message_id") else None,
    )


# ----------------------------------------------------------------------------------- client
def text_part(value: str, bold: list[tuple[int, int]] | None = None, italic: list[tuple[int, int]] | None = None) -> dict:
    """A text part with optional bold/italic ranges. Ranges are in UTF-16 code units, per the API."""
    part: dict = {"type": "text", "value": value}
    decorations = [{"range": list(r), "style": "bold"} for r in bold or []] + [{"range": list(r), "style": "italic"} for r in italic or []]
    if decorations:
        part["text_decorations"] = decorations
    return part


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def utf16_range(text: str, needle: str) -> tuple[int, int] | None:
    """UTF-16 range of the first occurrence of needle in text, or None."""
    i = text.find(needle)
    if i < 0 or not needle:
        return None
    start = utf16_len(text[:i])
    return start, start + utf16_len(needle)


class LinqClient:
    BASE = "https://api.linqapp.com/api/partner/v3"

    def __init__(self, api_key: str, http: httpx.AsyncClient):
        self._key = api_key
        self._http = http
        self._cap: dict[str, tuple[float, bool]] = {}
        self._gate = asyncio.Lock()
        self._last = 0.0

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._key}"}

    async def _request(self, method: str, path: str, *, json: dict | None = None, safe_to_retry: bool = False,
                       ok: tuple[int, ...] = (200, 201, 202, 204)) -> dict:
        """One API call with careful retries (see module docstring)."""
        last: LinqError | None = None
        for attempt in range(4):
            async with self._gate:                                  # at most ~5 calls per second from this process
                wait = 0.2 - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                resp = await self._http.request(method, self.BASE + path, json=json, headers=self._auth(), timeout=30)
            except httpx.TransportError as exc:
                last = LinqError(0, f"network error ({type(exc).__name__})")
                if not safe_to_retry:
                    raise last
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code in ok:
                return resp.json() if resp.content else {}
            if resp.status_code == 429:
                delay = float(resp.headers.get("retry-after") or 2 ** attempt)
                last = LinqError(429, resp.text[:200], retry_after=delay)
                await asyncio.sleep(min(delay, 20))
                continue
            if resp.status_code >= 500 and safe_to_retry:
                last = LinqError(resp.status_code, resp.text[:200])
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise LinqError(resp.status_code, resp.text[:300])
        raise last or LinqError(0, "gave up after retries")

    # ------------------------------------------------------------------------- sending
    async def send(self, handle: str, parts: list[dict], *, chat_id: str | None = None, reply_to: str | None = None,
                   effect: tuple[str, str] | None = None, idempotency_key: str | None = None) -> dict:
        """Send one message. Uses the known chat when we have it, else lets Linq pick the line by recipient."""
        message: dict = {"parts": parts}
        if reply_to:
            message["reply_to"] = {"message_id": reply_to}
        if effect and effect[1] in (SCREEN_EFFECTS if effect[0] == "screen" else BUBBLE_EFFECTS):
            message["effect"] = {"type": effect[0], "name": effect[1]}
        if idempotency_key:
            message["idempotency_key"] = idempotency_key[:255]
        if chat_id:
            return await self._request("POST", f"/chats/{chat_id}/messages", json={"message": message},
                                       safe_to_retry=bool(idempotency_key))
        return await self._request("POST", "/messages", json={"to": [handle], "message": message},
                                   safe_to_retry=bool(idempotency_key))

    async def send_text(self, to: str, text: str, **kw) -> dict:
        return await self.send(to, [{"type": "text", "value": text}], **kw)

    async def send_link(self, handle: str, url: str, *, chat_id: str | None = None, idempotency_key: str | None = None) -> dict:
        """A rich link preview card. A link part must be the only part of its message."""
        return await self.send(handle, [{"type": "link", "value": url}], chat_id=chat_id, idempotency_key=idempotency_key)

    async def react(self, message_id: str, kind: str, *, emoji: str | None = None, remove: bool = False, part_index: int = 0) -> None:
        """A tapback on a message. kind is one of love, like, dislike, laugh, emphasize, question, or custom (with emoji)."""
        body: dict = {"operation": "remove" if remove else "add", "type": kind, "part_index": part_index}
        if kind == "custom":
            body["custom_emoji"] = emoji or "👍"
        elif kind not in TAPBACKS:
            raise ValueError(f"unknown tapback {kind}")
        await self._request("POST", f"/messages/{message_id}/reactions", json=body, safe_to_retry=True)

    async def poll(self, chat_id: str, options: list[str], idempotency_key: str | None = None) -> dict:
        """A native iMessage poll. Needs an existing chat, at least two options, and has no title (send the question first)."""
        body: dict = {"poll": {"options": [{"text": o} for o in options]}}
        if idempotency_key:
            body["poll"]["idempotency_key"] = idempotency_key[:255]
        return await self._request("POST", f"/chats/{chat_id}/polls", json=body, safe_to_retry=bool(idempotency_key))

    async def get_poll(self, message_id: str) -> dict:
        return await self._request("GET", f"/messages/{message_id}/poll", safe_to_retry=True)

    async def mark_read(self, chat_id: str) -> None:
        if chat_id:
            try:
                await self._request("POST", f"/chats/{chat_id}/read", safe_to_retry=True)
            except Exception as exc:
                log.info("mark read skipped: %s", exc)

    async def typing(self, chat_id: str, on: bool = True) -> None:
        """Best-effort typing indicator. It clears by itself when a message is sent."""
        if not chat_id:
            return
        try:
            await self._http.request("POST" if on else "DELETE", f"{self.BASE}/chats/{chat_id}/typing", headers=self._auth(), timeout=10)
        except Exception:
            pass

    async def upload(self, data: bytes, filename: str, content_type: str) -> str:
        """Pre-upload a file and return its attachment id (presigned PUT, per the API)."""
        info = await self._request("POST", "/attachments", json={"filename": filename, "content_type": content_type, "size_bytes": len(data)},
                                   safe_to_retry=True)
        headers = dict(info.get("required_headers") or {})
        resp = await self._http.put(info["upload_url"], content=data, headers=headers, timeout=60)
        if resp.status_code >= 300:
            raise LinqError(resp.status_code, "attachment upload failed")
        return str(info["attachment_id"])

    async def send_image(self, handle: str, png: bytes, *, caption: str | None = None, bold: list[tuple[int, int]] | None = None,
                         filename: str = "card.png", chat_id: str | None = None, reply_to: str | None = None,
                         idempotency_key: str | None = None) -> dict:
        """Send an image (a card). The caption, if any, goes in the same bubble group as a separate text part after the image."""
        attachment_id = await self.upload(png, filename, "image/png")
        parts: list[dict] = [{"type": "media", "attachment_id": attachment_id}]
        if caption:
            parts.append(text_part(caption, bold=bold))
        return await self.send(handle, parts, chat_id=chat_id, reply_to=reply_to, idempotency_key=idempotency_key)

    async def imessage_available(self, handle: str) -> bool:
        """Can this person receive iMessage-only features (polls, effects, styling)? Cached for 10 minutes. Assume yes on errors."""
        cached = self._cap.get(handle)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        try:
            resp = await self._request("POST", "/capability/check_imessage", json={"address": handle})
            ok = bool(resp.get("available", True))
        except Exception:
            ok = True
        self._cap[handle] = (time.monotonic(), ok)
        return ok

    async def download(self, url: str, max_bytes: int = 25_000_000) -> bytes:
        """Fetch a signed attachment URL. The API token is deliberately not sent to the CDN."""
        chunks: list[bytes] = []
        total = 0
        async with self._http.stream("GET", url, follow_redirects=True, timeout=60) as resp:
            if resp.status_code >= 300:
                raise LinqError(resp.status_code, "attachment download failed")
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise LinqError(413, "attachment too large")
                chunks.append(chunk)
        return b"".join(chunks)
