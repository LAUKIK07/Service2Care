"""Glue between the replay loop, the alert engine and the cloud mirror, plus dashboard commands.

The replay loop must never wait for a language model, so alert handling and the hourly model reviews
run as background tasks. They are cancelled when a persona is reset.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .alerts import AlertEngine
from .cloud import CloudSync
from .db import Database
from .monitor import Monitor
from .replay import Replay
from .registry import PERSONAS

log = logging.getLogger("s2c.pipeline")


async def mem_say(pipe, handle: str, text: str) -> None:
    await pipe.alerts.messenger.say(handle, text, kind="system", key=f"demonote:{time.time_ns()}")
DEFAULT_START_DAY = 16      # days of history load instantly, so baselines exist before the first live tick
# Where a demo run begins: just before the story each person has, so the first text arrives within seconds.
DEMO_START = {"maya": 19, "daniel": 20, "priya": 16}
DEMO_SPEED = 6.0


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
        if tick["ts"][14:16] == "45":                  # once per simulated hour keeps D1 writes low
            self.sync.push("state", self.replay.state(persona))
            self.sync.push("monitor", {"persona": persona, "ts": tick["ts"], "review": mon.last_review,
                                       "night": mon.last_night, "gap_hours": mon.gap_hours})
        if cands:
            self._spawn(persona, self.alerts.on_candidates(persona, cands))
        self._spawn(persona, self.alerts.review_hour(persona, tick, mon))
        self._spawn(persona, self.alerts.note_period(persona, tick, mon))

    async def on_state(self, persona: str) -> None:
        self.sync.push("state", self.replay.state(persona))
        self._push_missing(persona)
        if self.replay.state(persona)["status"] == "finished" and persona == self.alerts.linked_persona:
            self._spawn(persona, self.announce_finished(persona))

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

    def push_thread(self, alert_id: int, mid: str, direction: str, text: str, kind: str, at: str, options: list | None = None) -> None:
        """One message of a check-in conversation, for the dashboard. Phone numbers are never included."""
        if text.startswith("[removed"):
            return
        self.sync.push("thread", {"alert_id": alert_id, "mid": mid, "direction": direction, "text": text[:1500], "kind": kind,
                                  "created_at": at, "options": options})

    def push_personas(self) -> None:
        self.sync.push("personas", {"items": PERSONAS.public(), "linked": self.alerts.linked_persona})

    def push_profile(self, handle: str) -> None:
        """The person's own health notes, as a snapshot (replaces the previous one). Sent only for the linked persona."""
        persona = self.alerts.persona_for(handle)
        if not persona or not self.alerts.memory:
            return
        items = self.alerts.memory.profile_items(handle)
        self.sync.push("profile", {"persona": persona, "items": [{"key": i["key"], "label": i["label"], "value": i["value"], "origin": i["origin"], "at": i["at"]} for i in items
                                    if i["key"] != "message_style"], "flags": self.alerts.memory.risk_flags(handle)})

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
                d = {k: a[k] for k in a.keys() if k not in ("handle", "message_id", "receipt")}
                self.sync.push("alert", d)
                with self.db.conn() as c:
                    msgs = c.execute("SELECT id, direction, text, kind, created_at FROM messages WHERE alert_id=? ORDER BY rowid", (d["id"],)).fetchall()
                for m in msgs:
                    opts = [o.strip() for o in m["text"][7:].split("|")] if m["kind"] == "poll" else None
                    self.push_thread(d["id"], m["id"], m["direction"], m["text"], m["kind"] if m["direction"] == "out" else "reply_in", m["created_at"], opts)
                if not msgs and d.get("text") and d["status"] in ("dry_run",):
                    self.push_thread(d["id"], f"alert-{d['id']}", "out", d["text"], "alert", d["created_at"])
            for n in notes:
                if self.alerts.stories:
                    self.alerts.stories._push(dict(n))                  # strips private parsing details before sending
            for f in reversed(feed):
                d = dict(f)
                d["data"] = json.loads(d["data"] or "{}")
                self.sync.push("feed", d)
        self.sync.push("setting", {"key": "send_alerts", "value": self.alerts.get_setting("send_alerts", "0")})
        self.push_personas()
        for h in self.alerts.s.allowed_numbers:
            self.push_profile(h)

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
            before = self.replay.state(persona)
            fresh = start_day is not None or before["cursor"] == 0 or before["status"] == "finished"
            if start_day is None and fresh:
                start_day = DEMO_START.get(persona, DEFAULT_START_DAY)
            if fresh:
                self.alerts.fresh_run(persona)
            st = await self.replay.start(persona, start_day=start_day, speed=args.get("speed"))
            if fresh and persona == self.alerts.linked_persona:
                await self._announce_start(persona, st)
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

    def dashboard_url(self) -> str | None:
        s = self.alerts.s
        if not (s.dashboard_url and s.cloud_url and s.dash_token):
            return None
        from urllib.parse import quote
        return f"{s.dashboard_url}/#api={quote(s.cloud_url, safe='')}&k={s.dash_token}"

    def dashboard_link(self) -> str:
        url = self.dashboard_url()
        return f"Your dashboard (opens already signed in, so keep this link to yourself): {url}" if url else "The dashboard is not configured yet."

    async def _announce_start(self, persona: str, st: dict) -> None:
        """Tell the person the demo began, so they can follow what is happening."""
        handle = self.alerts.handle_for(persona)
        mem = self.alerts.memory
        if not (handle and self.alerts.send_enabled and mem and mem.has_consent(handle) and not mem.is_opted_out(handle)):
            return
        from . import voice
        name = PERSONAS[persona].name
        when = st.get("ts") or ""
        day = f"{when[5:7]}/{when[8:10]}" if when else ""
        text = (f"Demo started. \U0001F3AC\n\nI am replaying {name}'s wearable data from {day} (simulated time).\n\n"
                "I will text you when something looks different from their usual. This takes about a minute.\n\n"
                "Say \"demo restart\" to run it again, or \"demo pause\" to stop.")
        await self.alerts.messenger.say(handle, text, kind="system", key=f"demostart:{persona}:{time.time_ns()}", bold=["Demo started."])

    async def announce_finished(self, persona: str) -> None:
        handle = self.alerts.handle_for(persona)
        if not (handle and self.alerts.send_enabled and self.alerts.memory.has_consent(handle)):
            return
        with self.db.conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM alerts WHERE persona=? AND status IN ('sent','discussed','resolved')", (persona,)).fetchone()["n"]
        await self.alerts.messenger.say(handle, f"The demo run finished. I sent {n} check-in{'s' if n != 1 else ''} this time.\n\n"
                                                "Say \"demo restart\" to see it again from the start.", kind="system",
                                        key=f"demoend:{persona}:{time.time_ns()}")

    async def start_demo(self, handle: str) -> None:
        """The person tapped 'Yes, start' at the end of setup: turn on alert texts and start the replay."""
        await self.command({"type": "send_alerts", "args": {"value": True}})
        persona = self.alerts.linked_persona
        await self.command({"type": "play", "persona": persona, "args": {"speed": 4}})
        await self.alerts.messenger.say(handle, "Started. I will text you only if something changes. Say \"demo pause\" to stop, or \"demo speed 12\" to go faster.",
                                        kind="system", key=f"startdemo:{time.time_ns()}")

    def my_day_card(self, handle: str) -> tuple[bytes, str] | None:
        """'Show me my day': today's latest values next to the person's usual, as a picture. Built from the series, no model."""
        from . import cards, voice
        persona = self.alerts.persona_for(handle)
        if not persona or not self.replay:
            return None
        st = self.replay.state(persona)
        ticks = self.replay.ticks(persona)[: st["cursor"]]
        if not ticks:
            return None
        mon = self.replay.monitor(persona)
        last = ticks[-1]
        tiles = []
        for key, fmt_ in (("heart_rate", "{:.0f}"), ("respiratory_rate", "{:.1f}"), ("spo2", "{:.1f}%"), ("skin_temp", "{:.1f} \u00b0C"), ("hrv_rmssd", "{:.0f} ms")):
            vals = [t["v"].get(key) for t in ticks[-4:] if t["quality"] == "good" and t["v"].get(key) is not None]
            if not vals:
                continue
            val = sum(vals) / len(vals)
            rev = mon.last_review.get(key)
            small = f"usual {voice.num(rev['median'])}" if rev else "learning your usual"
            tiles.append((voice.cap(voice.plain(key)[0]), fmt_.format(val), small))
        night = mon.last_night
        if night:
            tiles.append(("Sleep last night", f"{night['sleep_hours']:.1f} h", "from your band"))
        steps = sum(t["v"].get("steps") or 0 for t in ticks[-(len(ticks) % 96 or 96):])
        tiles.append(("Steps today", f"{steps:,.0f}", "so far"))
        png = cards.day_card(title="Your day so far", when=last["ts"].replace("T", " ") + " (simulated)", tiles=tiles[:6],
                             note="Demo data. Not medical advice.")
        return png, "Here is your day."

    def doctor_summary_card(self, handle: str) -> tuple[bytes, str] | None:
        """A one-page record of recent check-ins and the person's own notes, to show a clinician. Only what is stored; nothing is interpreted."""
        from . import cards
        with self.db.conn() as c:
            notes = c.execute("SELECT a.*, l.ts AS alert_ts FROM annotations a JOIN alerts l ON l.id=a.alert_id WHERE l.handle=? "
                              "AND a.summary != '' ORDER BY a.alert_id DESC LIMIT 4", (handle,)).fetchall()
        rows = []
        profile = self.alerts.memory.profile_items(handle)
        if profile:
            rows.append(("Notes you gave me", "; ".join(f"{p['label']}: {p['value']}" for p in profile if p["key"] != "message_style")[:300]))
        from .alerts import LABEL, pretty
        for n in notes:
            versions = json.loads(n["versions"] or "[]")
            rows.append((f"{pretty(n['alert_ts'])}: {LABEL.get(n['metric'], n['metric'])}",
                         f"You said: {n['summary']} Numbers: {versions[-1]['text'] if versions else 'no reading recorded.'}"[:420]))
        if not rows:
            return None
        png = cards.story_card(title="Summary for your care team", when="Recent check-ins", rows=rows,
                               note="Made from wearable demo data and what you told me. It describes numbers and your words. It is not a diagnosis.",
                               tag="For your doctor")
        return png, "Here is a one-page summary you can show your doctor."

    async def chat_command(self, handle: str, word: str) -> str | None:
        """Typed iMessage controls: 'demo play', 'demo pause', 'demo status', 'demo reset', 'demo day 18', 'demo speed 6', 'demo alerts on'."""
        persona = self.alerts.persona_for(handle)
        if not persona:
            return None
        arg = word.removeprefix("demo ").strip()
        if arg == "link":
            return self.dashboard_link()
        if arg in ("play", "start", "resume", "restart", "again"):
            mem = self.alerts.memory
            if mem.is_opted_out(handle):
                return "You opted out with STOP. Reply START to turn check-ins back on."
            note = ""
            if not mem.has_consent(handle):
                mem.set_consent(handle, True)                      # asking for the demo from your own phone is a clear yes
                if mem.onboarding(handle)[0] in ("new", "consent"):
                    mem.set_onboarding(handle, "done", 0)
                note = "Check-ins are on for this demo. Say STOP any time to end them. Say \"demo onboarding\" to add your health notes.\n\n"
            mem.resume(handle)
            self.alerts.set_setting("send_alerts", "1")
            self.sync.push("setting", {"key": "send_alerts", "value": "1"})
            if arg in ("restart", "again"):
                await self.command({"type": "reset", "persona": persona})
            if note:
                await mem_say(self, handle, note.strip())
            await self.command({"type": "play", "persona": persona, "args": {"speed": DEMO_SPEED}})
            return ""                      # nothing more to say: the "Demo started" text is sent by the run itself

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
