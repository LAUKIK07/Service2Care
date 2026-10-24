"""FastAPI receiver for Linq webhooks plus the always-on wearable pipeline. Bound to localhost and guarded by a shared secret."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from .agent import Agent
from .alerts import AlertEngine
from .cloud import CloudSync
from .config import load_settings
from .db import Database
from .jev import JevClient
from .linq import LinqClient, parse_inbound
from .llm import LLMClient
from .memory import Memory
from .ocr import DocumentReader
from .pipeline import Pipeline
from .replay import Replay
from .sim.personas import PERSONAS
from .textutil import now_iso
from .tools import ToolBox

log = logging.getLogger("s2c.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    missing = [n for n, v in (("DEEPINFRA_API_KEY", settings.llm_api_key), ("LINQ_API_KEY", settings.linq_api_key),
                              ("WEBHOOK_SECRET", settings.webhook_secret)) if not v]
    if missing:
        raise RuntimeError(f"missing settings: {', '.join(missing)}")
    if not settings.allowed_numbers:
        raise RuntimeError("ALLOWED_NUMBERS is empty, so nobody could be answered")
    http = httpx.AsyncClient(limits=httpx.Limits(keepalive_expiry=5.0))  # avoid reusing stale connections
    db = Database(settings.data_dir / "s2c.sqlite3")
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, http, embed_model=settings.embed_model)
    linq = LinqClient(settings.linq_api_key, http)
    docs = DocumentReader(settings.ocr_provider, settings.ocr_api_key, http)
    memory = Memory(db, llm, settings)
    agent = Agent(settings, memory, llm, linq, docs)

    jev = JevClient(settings.openrouter_api_key, http, settings.jev_model) if settings.openrouter_api_key else None
    sync = CloudSync(settings.cloud_url, settings.ingest_token, http)
    engine = AlertEngine(settings, db, llm, jev, sync)
    pipeline = Pipeline(db, engine, sync)
    replay = Replay(db, pipeline)
    pipeline.replay = replay
    engine.replay = replay
    engine.say = agent.send_proactive
    agent.alerts, agent.replay = engine, replay
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
    pipeline.resync_all()

    app.state.settings = settings
    app.state.agent = agent
    app.state.pipeline = pipeline
    app.state.replay = replay
    app.state.tasks = set()
    sweeper = asyncio.create_task(agent.sweeper())
    syncer = asyncio.create_task(sync.run())
    log.info("ready: text=%s vision=%s ocr=%s jev=%s cloud=%s allowlist=%d", settings.text_models[0],
             settings.vision_models[0], settings.ocr_provider if docs.can_ocr else "text-layer only",
             settings.jev_model if jev else "off", settings.cloud_url or "off", len(settings.allowed_numbers))
    try:
        yield
    finally:
        sweeper.cancel()
        syncer.cancel()
        for persona in PERSONAS:
            await replay.stop(persona)
        await http.aclose()


app = FastAPI(title="Signal2Care", lifespan=lifespan)


def _authorized(request: Request, k: str) -> None:
    secret = request.app.state.settings.webhook_secret
    if not hmac.compare_digest(k.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/webhooks/linq")
async def linq_webhook(request: Request, k: str = "") -> dict:
    _authorized(request, k)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    log.info("webhook event_type=%s", body.get("event_type"))
    inbound = parse_inbound(body)
    if inbound:
        task = asyncio.create_task(_handle(request.app, inbound))
        request.app.state.tasks.add(task)
        task.add_done_callback(request.app.state.tasks.discard)
    return {"ok": True}


@app.post("/internal/command")
async def internal_command(request: Request, k: str = "") -> dict:
    """Local control (same secret as the webhook). The dashboard uses the Cloudflare queue instead."""
    _authorized(request, k)
    body = await request.json()
    try:
        result = await request.app.state.pipeline.command(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "result": result}


@app.get("/internal/state")
async def internal_state(request: Request, k: str = "") -> dict:
    _authorized(request, k)
    replay = request.app.state.replay
    return {p: replay.state(p) for p in PERSONAS}


async def _handle(app: FastAPI, inbound) -> None:
    try:
        await app.state.agent.on_inbound(inbound)
    except Exception:
        log.exception("failed to handle inbound message")
