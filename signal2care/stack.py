"""Builds the whole system from settings. Used by the web app and by tests (which pass a fake Linq client)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .agent import Agent
from .alerts import AlertEngine
from .cloud import CloudSync
from .config import Settings
from .db import Database
from .jev import JevClient
from .linq import LinqClient
from .llm import LLMClient
from .memory import Memory
from .messenger import Messenger
from .ocr import DocumentReader
from .onboarding import Onboarding
from .pipeline import Pipeline
from .registry import PERSONAS
from .replay import Replay
from .stories import StoryEngine
from .textutil import now_iso
from .tools import ToolBox


@dataclass
class Stack:
    settings: Settings
    http: httpx.AsyncClient
    db: Database
    llm: LLMClient
    linq: object
    docs: DocumentReader
    memory: Memory
    messenger: Messenger
    agent: Agent
    jev: JevClient | None
    sync: CloudSync
    engine: AlertEngine
    pipeline: Pipeline
    replay: Replay
    stories: StoryEngine
    onboarding: Onboarding


def build(settings: Settings, http: httpx.AsyncClient, linq=None) -> Stack:
    db = Database(settings.data_dir / "s2c.sqlite3")
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, http, embed_model=settings.embed_model)
    linq = linq or LinqClient(settings.linq_api_key, http)
    docs = DocumentReader(settings.ocr_provider, settings.ocr_api_key, http)
    memory = Memory(db, llm, settings)
    messenger = Messenger(memory, linq, use_polls=settings.use_polls)
    agent = Agent(settings, memory, llm, linq, docs, messenger)
    jev = JevClient(settings.openrouter_api_key, http, settings.jev_model) if settings.openrouter_api_key else None
    sync = CloudSync(settings.cloud_url, settings.ingest_token, http)
    engine = AlertEngine(settings, db, llm, jev, sync)
    engine.messenger, engine.memory = messenger, memory
    pipeline = Pipeline(db, engine, sync)
    replay = Replay(db, pipeline)
    pipeline.replay = replay
    engine.replay = replay
    stories = StoryEngine(settings, db, llm, jev, engine, messenger, memory, sync)
    engine.stories = stories
    onboarding = Onboarding(memory, messenger)
    onboarding.link = pipeline.dashboard_url
    onboarding.on_profile = pipeline.push_profile
    onboarding.start_demo = pipeline.start_demo
    agent.alerts, agent.replay, agent.stories, agent.onboarding = engine, replay, stories, onboarding
    agent.push_profile = pipeline.push_profile
    messenger.on_thread = pipeline.push_thread

    def on_delivery(message_id: str, state: str, reason: str | None) -> None:
        """Delivered/read receipts for our alert texts show up in the activity feed, so you can see the person read it."""
        with db.conn() as c:
            a = c.execute("SELECT id, persona, ts, metric, event_id FROM alerts WHERE message_id=?", (message_id,)).fetchone()
        with db.conn() as c:
            m = c.execute("SELECT alert_id FROM messages WHERE id=?", (message_id,)).fetchone()
        if m and m["alert_id"] and state in ("delivered", "read", "failed"):
            sync.push("receipt", {"alert_id": m["alert_id"], "mid": message_id, "state": state, "at": now_iso()})
        if a and state in ("delivered", "read", "failed"):
            engine.feed(a["persona"], a["ts"], "alert", f"receipt_{state}", {"delivered": "Delivered to the phone", "read": "Read on the phone", "failed": "Failed to deliver"}[state]
                        + f" ({a['event_id']})", {"alert_id": a["id"], "state": state})
    messenger.on_delivery = on_delivery
    engine.push_thread = pipeline.push_thread
    agent.pipeline = pipeline
    agent.tools = ToolBox(replay, engine, http)
    agent.demo = pipeline.chat_command
    sync.on_command = pipeline.command
    sync.is_active = lambda: any(replay.state(p)["status"] == "running" for p in PERSONAS)
    sync.health_provider = lambda: {
        "ts": now_iso(), "send_alerts": engine.send_enabled, "jev": bool(jev), "jev_model": settings.jev_model,
        "text_model": settings.text_models[0], "vision_model": settings.vision_models[0],
        "ocr": settings.ocr_provider if docs.can_ocr else "text-layer only",
        "personas": {p: replay.state(p) for p in PERSONAS},
    }
    return Stack(settings, http, db, llm, linq, docs, memory, messenger, agent, jev, sync, engine, pipeline, replay, stories, onboarding)
