"""Mirror of the local brain's state to the Cloudflare Worker, and the command channel back.

The brain stays on this machine (messages, memory, models, keys). Only what the dashboard shows is
pushed: the simulated series, alert records, the activity feed, annotations and the alert threads.
Phone numbers never leave. When CLOUD_URL is not set every call is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

log = logging.getLogger("s2c.cloud")

BATCH = 300
FLUSH_EVERY = 1.0
POLL_EVERY = 1.5
HEALTH_EVERY = 5.0


class CloudSync:
    def __init__(self, url: str, token: str, http: httpx.AsyncClient):
        self.url = url.rstrip("/")
        self.token = token
        self.http = http
        self.enabled = bool(self.url and self.token)
        self.buf: deque = deque(maxlen=40_000)
        self.health_provider = None      # () -> dict
        self.on_command = None           # async (cmd: dict) -> str
        self.status = {"ok": None, "last_ok": None, "error": None, "pushed": 0, "dropped": 0}

    def push(self, kind: str, obj: dict) -> None:
        if not self.enabled:
            return
        if len(self.buf) == self.buf.maxlen:
            self.status["dropped"] += 1
        self.buf.append({"k": kind, "d": obj})

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _flush(self) -> bool:
        while self.buf:
            n = min(BATCH, len(self.buf))
            batch = [self.buf.popleft() for _ in range(n)]
            try:
                r = await self.http.post(f"{self.url}/api/ingest", json={"items": batch}, headers=self._headers(), timeout=30)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
            except Exception as exc:
                self.buf.extendleft(reversed(batch))          # keep order, retry later
                self.status.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
                return False
            self.status["pushed"] += n
        self.status.update(ok=True, error=None, last_ok=time.time())
        return True

    async def _commands(self) -> None:
        if not self.on_command:
            return
        r = await self.http.get(f"{self.url}/api/commands", headers=self._headers(), timeout=15)
        if r.status_code != 200:
            return
        for cmd in r.json().get("commands", []):
            try:
                result = await self.on_command(cmd)
                status = "done"
            except Exception as exc:
                log.exception("command %s failed", cmd.get("type"))
                result, status = f"{type(exc).__name__}: {exc}"[:200], "failed"
            await self.http.post(f"{self.url}/api/commands/ack", json={"id": cmd["id"], "status": status, "result": result},
                                 headers=self._headers(), timeout=15)

    async def run(self) -> None:
        if not self.enabled:
            log.info("cloud sync disabled (CLOUD_URL or INGEST_TOKEN not set)")
            return
        log.info("cloud sync -> %s", self.url)
        last_health = 0.0
        delay = 0.0
        while True:
            try:
                await asyncio.sleep(max(FLUSH_EVERY if delay == 0 else delay, 0.2))
                if time.monotonic() - last_health >= HEALTH_EVERY and self.health_provider:
                    last_health = time.monotonic()
                    self.push("health", {**self.health_provider(), "cloud": {k: self.status[k] for k in ("pushed", "dropped")}})
                ok = await self._flush()
                if ok:
                    await self._commands()
                delay = 0.0 if ok else min(delay * 2 or 2.0, 15.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
                delay = min(delay * 2 or 2.0, 15.0)
