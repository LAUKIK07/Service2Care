"""Settings loaded from .env and the environment."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_phone(value: str) -> str:
    """Return E.164 for US-style numbers, else an empty string."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if len(digits) > 7 else ""


def mask(handle: str) -> str:
    if "@" in handle:
        return "***@" + handle.split("@", 1)[1]
    return "***" + handle[-2:] if handle else "?"


def _csv(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.environ.get(name, default).split(",") if p.strip()]


def _chain(*names_and_defaults: str) -> tuple[str, ...]:
    seen: list[str] = []
    for item in names_and_defaults:
        for part in item.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    mistral_api_key: str
    chat_models: tuple[str, ...]
    memory_models: tuple[str, ...]
    embed_model: str
    ocr_model: str
    linq_api_key: str
    linq_from_phone: str
    allowed_numbers: frozenset[str]
    webhook_secret: str
    host: str
    port: int
    data_dir: Path
    debounce_seconds: float


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get
    chat = _chain(
        env("MISTRAL_CHAT_MODEL", ""),
        env("MISTRAL_CHAT_MODELS", ""),
        "mistral-large-2512,mistral-medium-latest,mistral-small-latest",
    )
    memory = _chain(env("MISTRAL_MEMORY_MODEL", ""), "mistral-small-latest", *chat)
    return Settings(
        mistral_api_key=env("MISTRAL_API_KEY", ""),
        chat_models=chat,
        memory_models=memory,
        embed_model=env("MISTRAL_EMBED_MODEL", "mistral-embed"),
        ocr_model=env("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
        linq_api_key=env("LINQ_API_KEY", ""),
        linq_from_phone=env("LINQ_FROM_PHONE", ""),
        allowed_numbers=frozenset(n for n in map(normalize_phone, _csv("ALLOWED_NUMBERS")) if n),
        webhook_secret=env("WEBHOOK_SECRET", ""),
        host=env("BIND_HOST", "127.0.0.1"),
        port=int(env("BIND_PORT", "8787")),
        data_dir=Path(env("DATA_DIR", str(ROOT / "data"))),
        debounce_seconds=float(env("DEBOUNCE_SECONDS", "2.5")),
    )
