"""A stand-in for LinqClient that records what would be sent. Used by tests; nothing leaves the machine."""
from __future__ import annotations


class FakeLinq:
    """Records what would be sent. Mimics the parts of Linq the product uses, including idempotency keys and poll option ids."""

    def __init__(self):
        self.out: list[dict] = []
        self.n = 0
        self.keys: dict[str, dict] = {}
        self.polls: dict[str, list[tuple[str, str]]] = {}
        self.reads, self.taps = [], []

    async def send(self, handle, parts, *, chat_id=None, reply_to=None, effect=None, idempotency_key=None):
        if idempotency_key and idempotency_key in self.keys:
            return self.keys[idempotency_key]
        self.n += 1
        mid = f"out-{self.n}"
        kinds = {p["type"] for p in parts}
        rec = {"id": mid, "kinds": sorted(kinds), "text": "\n".join(p.get("value", "") for p in parts if p["type"] == "text"),
               "bold": [d for p in parts for d in p.get("text_decorations", [])], "reply_to": reply_to, "effect": effect, "key": idempotency_key,
               "link": next((p["value"] for p in parts if p["type"] == "link"), None), "media": "media" in kinds}
        self.out.append(rec)
        resp = {"chat_id": "chat-1", "message": {"id": mid}}
        if idempotency_key:
            self.keys[idempotency_key] = resp
        return resp

    async def send_text(self, to, text, **kw):
        return await self.send(to, [{"type": "text", "value": text}], **kw)

    async def send_link(self, handle, url, *, chat_id=None, idempotency_key=None):
        return await self.send(handle, [{"type": "link", "value": url}], chat_id=chat_id, idempotency_key=idempotency_key)

    async def send_image(self, handle, png, *, caption=None, bold=None, filename="card.png", chat_id=None, reply_to=None, idempotency_key=None):
        parts = [{"type": "media", "attachment_id": "att-1"}]
        if caption:
            parts.append({"type": "text", "value": caption, **({"text_decorations": [{"range": list(r), "style": "bold"} for r in bold]} if bold else {})})
        resp = await self.send(handle, parts, chat_id=chat_id, reply_to=reply_to, idempotency_key=idempotency_key)
        self.out[-1]["png"] = len(png)
        return resp

    async def react(self, message_id, kind, *, emoji=None, remove=False, part_index=0):
        self.taps.append((message_id, kind))

    async def poll(self, chat_id, options, idempotency_key=None):
        if idempotency_key and idempotency_key in self.keys:
            return self.keys[idempotency_key]
        self.n += 1
        mid = f"poll-{self.n}"
        self.polls[mid] = [(f"{mid}-o{i}", t) for i, t in enumerate(options)]
        self.out.append({"id": mid, "kinds": ["poll"], "text": " | ".join(options), "key": idempotency_key})
        resp = {"chat_id": chat_id, "message_id": mid, "poll": {"options": [{"id": oid, "text": t} for oid, t in self.polls[mid]]}}
        if idempotency_key:
            self.keys[idempotency_key] = resp
        return resp

    async def get_poll(self, message_id):
        return {"poll": {"options": [{"id": oid, "text": t} for oid, t in self.polls[message_id]]}}

    async def mark_read(self, chat_id):
        self.reads.append(chat_id)

    async def typing(self, chat_id, on=True):
        return None

    async def imessage_available(self, handle):
        return True

    async def download(self, url, max_bytes=0):
        raise RuntimeError("no network in test")
