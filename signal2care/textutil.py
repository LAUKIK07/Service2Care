"""Small text and time helpers."""
from __future__ import annotations

import re
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(value: str | None) -> str:
    dt = parse_ts(value).astimezone()
    return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")


_MD = re.compile(r"(\*\*|__|`{1,3}|^#{1,6}\s*)", re.M)


def clean_reply(text: str) -> str:
    """iMessage does not render markdown, so strip the common markers."""
    text = _MD.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_reply(text: str, limit: int = 900) -> list[str]:
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        pieces = re.split(r"(?<=[.!?])\s+", para) if len(para) > limit else [para]
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > limit:
                parts.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts[:4]


def normalize_word(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (text or "").lower())).strip()
