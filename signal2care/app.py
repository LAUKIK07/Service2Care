"""FastAPI receiver for Linq webhooks. Bound to localhost and guarded by a shared secret."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from .agent import Agent
from .config import load_settings
from .db import Database
from .linq import LinqClient, parse_inbound
from .memory import Memory
from .mistral import MistralClient

log = logging.getLogger("s2c.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    missing = [n for n, v in (("MISTRAL_API_KEY", settings.mistral_api_key), ("LINQ_API_KEY", settings.linq_api_key),
                              ("WEBHOOK_SECRET", settings.webhook_secret)) if not v]
    if missing:
        raise RuntimeError(f"missing settings: {', '.join(missing)}")
    if not settings.allowed_numbers:
        raise RuntimeError("ALLOWED_NUMBERS is empty, so nobody could be answered")
    http = httpx.AsyncClient()
    db = Database(settings.data_dir / "s2c.sqlite3")
    mistral = MistralClient(settings.mistral_api_key, http, embed_model=settings.embed_model,
                            ocr_model=settings.ocr_model)
    linq = LinqClient(settings.linq_api_key, http)
    memory = Memory(db, mistral, settings)
    agent = Agent(settings, memory, mistral, linq)
    app.state.settings = settings
    app.state.agent = agent
    app.state.tasks = set()
    sweeper = asyncio.create_task(agent.sweeper())
    log.info("ready: chat models %s, allowlist size %d", settings.chat_models, len(settings.allowed_numbers))
    try:
        yield
    finally:
        sweeper.cancel()
        await http.aclose()


app = FastAPI(title="Signal2Care", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/webhooks/linq")
async def linq_webhook(request: Request, k: str = "") -> dict:
    secret = request.app.state.settings.webhook_secret
    if not hmac.compare_digest(k.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")
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


async def _handle(app: FastAPI, inbound) -> None:
    try:
        await app.state.agent.on_inbound(inbound)
    except Exception:
        log.exception("failed to handle inbound message")
