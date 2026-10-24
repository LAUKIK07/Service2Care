"""Linq (iMessage) client and inbound webhook parsing."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from .config import normalize_phone

log = logging.getLogger("s2c.linq")


class LinqError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


@dataclass
class Inbound:
    event_id: str
    message_id: str
    chat_id: str
    handle: str
    sent_at: str | None
    text: str
    media: list[dict] = field(default_factory=list)


def _normalize_handle(raw: str) -> str:
    raw = (raw or "").strip()
    if "@" in raw:
        return raw.lower()
    return normalize_phone(raw) or raw


def parse_inbound(body: dict) -> Inbound | None:
    """Return an Inbound for message.received events, else None."""
    if body.get("event_type") != "message.received":
        return None
    data = body.get("data") or {}
    # 2025-01-01 payloads nest the message under data.message
    msg = data["message"] if isinstance(data.get("message"), dict) else data
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
    return Inbound(
        event_id=str(body.get("event_id") or message_id),
        message_id=message_id,
        chat_id=str(chat.get("id") or msg.get("chat_id") or ""),
        handle=handle,
        sent_at=msg.get("sent_at") or body.get("created_at"),
        text=text,
        media=media,
    )


class LinqClient:
    BASE = "https://api.linqapp.com/api/partner/v3"

    def __init__(self, api_key: str, http: httpx.AsyncClient):
        self._key = api_key
        self._http = http

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._key}"}

    async def send_text(self, to: str, text: str) -> dict:
        """Send into the existing chat with `to` (Linq picks the line)."""
        resp = await self._http.post(
            f"{self.BASE}/messages",
            json={"to": [to], "message": {"parts": [{"type": "text", "value": text}]}},
            headers=self._auth(),
            timeout=30,
        )
        if resp.status_code >= 300:
            raise LinqError(resp.status_code, resp.text[:300])
        return resp.json()

    async def typing(self, chat_id: str) -> None:
        """Best-effort typing indicator."""
        if not chat_id:
            return
        try:
            await self._http.post(f"{self.BASE}/chats/{chat_id}/typing", headers=self._auth(), timeout=10)
        except Exception:
            pass

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
