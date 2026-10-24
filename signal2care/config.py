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


def _csv(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _chain(*items: str) -> tuple[str, ...]:
    seen: list[str] = []
    for item in items:
        for part in item.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    # LLM (OpenAI-compatible endpoint, DeepInfra by default)
    llm_base_url: str
    llm_api_key: str
    text_models: tuple[str, ...]      # used for turns without images
    vision_models: tuple[str, ...]    # used for turns that include images
    memory_models: tuple[str, ...]    # background note-taking
    embed_model: str
    # Documents
    ocr_provider: str                 # "ocrspace" or "none"; PDFs with a text layer never need it
    ocr_api_key: str
    # Decision model (Jev through OpenRouter). Not used by the chat path yet.
    openrouter_api_key: str
    jev_model: str
    # Cloudflare mirror for the dashboard (optional)
    cloud_url: str
    ingest_token: str
    dash_token: str
    dashboard_url: str
    # Conversation and notification pacing
    alert_min_gap_s: float       # least real time between two proactive alert texts to one person
    story_idle_min: float        # a check-in conversation closes after this many quiet minutes
    quiet_start: int             # local hour quiet hours begin (no proactive texts except urgent ones)
    quiet_end: int
    use_polls: bool              # tap-to-answer polls need poll votes delivered; the CLI relay does not deliver them
    # iMessage
    linq_api_key: str
    linq_from_phone: str
    allowed_numbers: frozenset[str]
    # Receiver
    webhook_secret: str
    host: str
    port: int
    data_dir: Path
    debounce_seconds: float


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get
    text = _chain(env("DEEPINFRA_TEXT_MODEL", ""), "deepseek-ai/DeepSeek-V4-Flash-0731",
                  "deepseek-ai/DeepSeek-V4.1-Flash-1017")
    vision = _chain(env("DEEPINFRA_VISION_MODEL", ""), "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                    "deepseek-ai/DeepSeek-V4.1-Flash")
    memory = _chain(env("DEEPINFRA_MEMORY_MODEL", ""), *text)
    return Settings(
        llm_base_url=env("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai"),
        llm_api_key=env("DEEPINFRA_API_KEY", ""),
        text_models=text,
        vision_models=vision,
        memory_models=memory,
        embed_model=env("DEEPINFRA_EMBED_MODEL", "BAAI/bge-m3"),
        ocr_provider=env("OCR_PROVIDER", "ocrspace" if env("OCR_API_KEY") else "none").lower(),
        ocr_api_key=env("OCR_API_KEY", ""),
        openrouter_api_key=env("OPENROUTER_API_KEY", ""),
        jev_model=env("JEV_MODEL", "typesafe/jev-1.14"),
        cloud_url=env("CLOUD_URL", ""),
        ingest_token=env("INGEST_TOKEN", ""),
        dash_token=env("DASH_TOKEN", ""),
        dashboard_url=env("DASHBOARD_URL", "").rstrip("/"),
        alert_min_gap_s=float(env("ALERT_MIN_GAP_S", "120")),
        story_idle_min=float(env("STORY_IDLE_MIN", "10")),
        quiet_start=int(env("QUIET_START", "22")),
        quiet_end=int(env("QUIET_END", "7")),
        use_polls=env("USE_POLLS", "0") == "1",
        linq_api_key=env("LINQ_API_KEY", ""),
        linq_from_phone=env("LINQ_FROM_PHONE", ""),
        allowed_numbers=frozenset(n for n in map(normalize_phone, _csv("ALLOWED_NUMBERS")) if n),
        webhook_secret=env("WEBHOOK_SECRET", ""),
        host=env("BIND_HOST", "127.0.0.1"),
        port=int(env("BIND_PORT", "8787")),
        data_dir=Path(env("DATA_DIR", str(ROOT / "data"))),
        debounce_seconds=float(env("DEBOUNCE_SECONDS", "2.5")),
    )
