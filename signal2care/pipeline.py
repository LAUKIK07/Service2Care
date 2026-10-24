"""Glue between the replay loop, the alert engine and the cloud mirror, plus dashboard commands.

The replay loop must never wait for a language model, so alert handling and the hourly model reviews
run as background tasks. They are cancelled when a persona is reset.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .alerts import AlertEngine
from .cloud import CloudSync
from .db import Database
from .monitor import Monitor
from .replay import Replay
from .sim.personas import PERSONAS

log = logging.getLogger("s2c.pipeline")
DEFAULT_START_DAY = 16      # five days of history load instantly, so baselines exist before the first live tick


class Pipeline:
    def __init__(self, db: Database, alerts: AlertEngine, sync: CloudSync):
        self.db = db
        self.alerts = alerts
        self.sync = sync
        self.replay: Replay | None = None
        self._tasks: dict[str, set[asyncio.Task]] = {p: set() for p in PERSONAS}
        self._pushed: dict[str, int] = {p: -1 for p in PERSONAS}

    # -------------------------------------------------------------- replay hooks
    def _spawn(self, persona: str, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks[persona].add(task)
        task.add_done_callback(self._tasks[persona].discard)
        task.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    async def on_tick(self, persona: str, tick: dict, mon: Monitor, cands: list) -> None:
        self.sync.push("series", {"persona": persona, "i": tick["i"], "ts": tick["ts"], "state": tick["state"],
                                  "quality": tick["quality"], "v": tick["v"]})
        self._pushed[persona] = max(self._pushed[persona], tick["i"])
        self.sync.push("state", self.replay.state(persona))
        if tick["ts"][14:16] == "45":
            self.sync.push("monitor", {"persona": persona, "ts": tick["ts"], "review": mon.last_review,
                                       "night": mon.last_night, "gap_hours": mon.gap_hours})
        if cands:
            self._spawn(persona, self.alerts.on_candidates(persona, cands))
        self._spawn(persona, self.alerts.review_hour(persona, tick, mon))
        self._spawn(persona, self.alerts.note_period(persona, tick, mon))

    async def on_state(self, persona: str) -> None:
        self.sync.push("state", self.replay.state(persona))
        self._push_missing(persona)

    async def on_reset(self, persona: str) -> None:
        for task in list(self._tasks[persona]):
            task.cancel()
        self._pushed[persona] = -1
        self.sync.push("reset", {"persona": persona})
        self.sync.push("state", self.replay.state(persona))

    # ------------------------------------------------------------------ resync
    def _push_missing(self, persona: str) -> None:
        """Send series rows the cloud has not been given (the instant history loaded at a start day)."""
        with self.db.conn() as c:
            rows = c.execute("SELECT i,ts,state,quality,v FROM series WHERE persona=? AND i>? ORDER BY i",
                             (persona, self._pushed[persona])).fetchall()
        for r in rows:
            self.sync.push("series", {"persona": persona, "i": r["i"], "ts": r["ts"], "state": r["state"],
                                      "quality": r["quality"], "v": json.loads(r["v"])})
        if rows:
            self._pushed[persona] = rows[-1]["i"]

    def resync_all(self) -> None:
        """After a restart, send everything stored locally so the cloud copy matches (writes are idempotent)."""
        for persona in PERSONAS:
            self._pushed[persona] = -1
            self.sync.push("reset", {"persona": persona})
            self._push_missing(persona)
            self.sync.push("state", self.replay.state(persona))
            with self.db.conn() as c:
                alerts = c.execute("SELECT * FROM alerts WHERE persona=? ORDER BY id", (persona,)).fetchall()
                notes = c.execute("SELECT * FROM annotations WHERE persona=? ORDER BY id", (persona,)).fetchall()
                feed = c.execute("SELECT * FROM feed WHERE persona=? ORDER BY id DESC LIMIT 300", (persona,)).fetchall()
            for a in alerts:
                d = {k: a[k] for k in a.keys() if k not in ("handle", "message_id")}
                self.sync.push("alert", d)
                if d.get("text") and d["status"] in ("sent", "dry_run", "discussed", "resolved"):
                    self.sync.push("thread", {"alert_id": d["id"], "mid": f"alert-{d['id']}", "direction": "out",
                                              "text": d["text"], "created_at": d["created_at"]})
            for n in notes:
                self.sync.push("annotation", dict(n))
            for f in reversed(feed):
                d = dict(f)
                d["data"] = json.loads(d["data"] or "{}")
                self.sync.push("feed", d)
        self.sync.push("setting", {"key": "send_alerts", "value": self.alerts.get_setting("send_alerts", "0")})

    # ----------------------------------------------------------------- commands
    async def command(self, cmd: dict) -> str:
        """Run one dashboard command. Raises on bad input so the dashboard sees the failure."""
        kind = cmd.get("type")
        persona = cmd.get("persona")
        args = json.loads(cmd["args"]) if isinstance(cmd.get("args"), str) and cmd.get("args") else (cmd.get("args") or {})
        if kind == "send_alerts":
            value = "1" if args.get("value") else "0"
            self.alerts.set_setting("send_alerts", value)
            self.sync.push("setting", {"key": "send_alerts", "value": value})
            return "iMessage alerts " + ("ON" if value == "1" else "OFF")
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona!r}")
        if kind == "play":
            start_day = args.get("start_day")
            if start_day is None and self.replay.state(persona)["cursor"] == 0:
                start_day = DEFAULT_START_DAY
            st = await self.replay.start(persona, start_day=start_day, speed=args.get("speed"))
            return f"{st['status']} at tick {st['cursor']}"
        if kind == "stop":
            st = await self.replay.stop(persona)
            return st["status"]
        if kind == "reset":
            await self.replay.reset(persona)
            return "reset"
        if kind == "speed":
            st = self.replay.set_speed(persona, args.get("speed"))
            self.sync.push("state", st)
            return f"speed {st['speed']}"
        raise ValueError(f"unknown command {kind!r}")

    async def chat_command(self, handle: str, word: str) -> str | None:
        """Typed iMessage controls: 'demo play', 'demo pause', 'demo status', 'demo reset', 'demo day 18', 'demo speed 6', 'demo alerts on'."""
        persona = self.alerts.persona_for(handle)
        if not persona:
            return None
        arg = word.removeprefix("demo ").strip()
        if arg in ("play", "start", "resume"):
            await self.command({"type": "play", "persona": persona})
        elif arg in ("pause", "stop"):
            await self.command({"type": "stop", "persona": persona})
        elif arg == "reset":
            await self.command({"type": "reset", "persona": persona})
            return "Demo reset. Say \"demo play\" to start again."
        elif arg.startswith("day "):
            await self.command({"type": "play", "persona": persona, "args": {"start_day": int(arg[4:])}})
        elif arg.startswith("speed "):
            await self.command({"type": "speed", "persona": persona, "args": {"speed": float(arg[6:])}})
        elif arg.startswith("alerts "):
            on = arg.endswith("on")
            await self.command({"type": "send_alerts", "args": {"value": on}})
            return "Alert texts are ON. Detected changes will be texted to you." if on else "Alert texts are OFF. Alerts are drafted on the dashboard only."
        st = self.replay.state(persona)
        if st["cursor"] == 0:
            return "The replay has not started. Say \"demo play\" to begin."
        sending = "on" if self.alerts.send_enabled else "off"
        return (f"Replay {st['status']}: simulated day {st['day'] + 1} of 28, {st['ts'][11:16]}, {st['speed']:g} ticks per second. "
                f"Alert texts {sending}.")
