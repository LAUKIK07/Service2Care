"""FastAPI receiver for Linq webhooks plus the always-on wearable pipeline. Bound to localhost and guarded by a shared secret."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from .config import load_settings
from .linq import parse_event
from .stack import build
from .registry import PERSONAS

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
    st = build(settings, http)
    agent, pipeline, replay, sync, jev, docs = st.agent, st.pipeline, st.replay, st.sync, st.jev, st.docs
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
    event = parse_event(body)
    if event:
        task = asyncio.create_task(_handle(request.app, event))
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


async def _handle(app: FastAPI, event) -> None:
    try:
        await app.state.agent.on_event(event)
    except Exception:
        log.exception("failed to handle inbound message")
