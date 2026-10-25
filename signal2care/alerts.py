"""From a detected deviation to a message the person can answer.

For each candidate from the monitor:
1. Deterministic pre-gates: cooldown, daily cap, spacing, "already explained".
2. Jev decides from a typed state (never raw user text): alert now, keep watching, ignore, or data problem.
   If Jev is unreachable, plain rules decide. A very large, sustained, well-measured change is never silenced by a model.
3. Delivery gates (the person's choices and good manners): consent, PAUSE, quiet hours, one open conversation at a time,
   and a minimum real-time gap between texts. A blocked alert is kept, shown on the dashboard, and sent later as one message.
4. Jev picks ONE question from a short approved list, using the context (sleep or awake, recent activity, what was asked
   already, health notes the person gave). Fixed wording turns the numbers into plain sentences; no model words an alert.
5. The text goes out with a picture card and a tap-to-answer poll, through an outbox so nothing is ever sent twice.
Every alert gets an event id and an evidence receipt (the exact readings, the baseline, the arithmetic and the rule),
and replies, checks and corrections are linked to that same id (see stories.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta

from . import cards, claims, stats, voice
from .config import Settings
from .db import Database
from .jev import Decision, JevClient, JevError, choice, noul, score
from .llm import LLMClient, LLMError
from .monitor import Candidate, Monitor
from .registry import PERSONAS
from .sim.personas import LABELS, START_DATE, TICK_MINUTES, UNITS
from .textutil import now_iso

log = logging.getLogger("s2c.alerts")

LABEL = {**LABELS, "sleep_hours": "Sleep duration", "rhr_sleep": "Resting heart rate during sleep",
         "hrv_sleep": "Overnight HRV", "data_quality": "Tracker data"}
UNIT = {**UNITS, "sleep_hours": "h", "rhr_sleep": "bpm", "hrv_sleep": "ms", "data_quality": ""}
FAMILY = voice.FAMILY
COLORS = {"heart": (255, 56, 92), "breathing": (14, 165, 233), "temperature": (245, 158, 11), "blood_pressure": (228, 87, 46),
          "stress": (192, 38, 211), "sleep": (99, 102, 241), "device": (106, 106, 106), "other": (255, 56, 92)}

COOLDOWN_H = 6              # simulated hours
ONGOING_COOLDOWN_H = 12
SPACING_MIN = 180           # simulated minutes between texts: one text per change, not one per metric
OPEN_HOLD_H = 12
DAILY_CAP = 3               # per simulated day
FLOOR_Z = 7.0
FLOOR_PERSIST = 2
HELD_TTL_H = 24             # real hours a held alert may wait before it is dropped as stale
REVIEW_GAP_S = 3.0          # at fast replay speeds the model reviews sample the stream instead of paying per simulated hour
NOTE_GAP_S = 8.0
_START = datetime.fromisoformat(START_DATE)

OPTIONS = {
    "alert_now": "Ask the person about this change now.",
    "watch": "Keep watching, do not message yet.",
    "ignore": "Within normal variation, no action.",
    "data_issue": "The reading is unreliable, treat it as a data problem.",
}
QUESTION_HELP = {
    "activity": "Ask whether they were exercising or moving a lot just before. Best for heart or breathing changes while awake.",
    "symptoms": "Ask how they feel right now. Best when several signals moved together or when they have health notes on file.",
    "sleep": "Ask how they slept. Best for changes after a short or broken night.",
    "stress": "Ask how their day has been. Best for heart, blood pressure or sweat changes without exercise.",
    "device": "Ask whether the band is snug. Best when readings look implausible or data is patchy.",
    "heat": "Ask whether they are somewhere warm. Best for a skin temperature rise.",
    "open": "Ask an open question about anything different today.",
}


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def index_of(ts: str) -> int:
    return int((_t(ts) - _START).total_seconds() // (TICK_MINUTES * 60))


def pretty(ts: str) -> str:
    return _t(ts).strftime("%a %b %-d \u00b7 %H:%M")


class AlertEngine:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, jev: JevClient | None, sync):
        self.s = settings
        self.db = db
        self.llm = llm
        self.jev = jev
        self.sync = sync
        self.messenger = None      # Messenger, set by the app
        self.memory = None         # Memory, set by the app
        self.stories = None        # StoryEngine, set by the app
        self.replay = None         # set by the app
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_wall: dict[tuple, float] = {}
        self._last_sent_wall: dict[str, float] = {}

    def _due(self, persona: str, kind: str, gap: float) -> bool:
        now, key = time.monotonic(), (persona, kind)
        if now - self._last_wall.get(key, -1e9) < gap:
            return False
        self._last_wall[key] = now
        return True

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        with self.db.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def send_enabled(self) -> bool:
        return self.get_setting("send_alerts", "0") == "1"

    @property
    def linked_persona(self) -> str:
        pid = self.get_setting("linked_persona", "maya")
        return pid if pid in PERSONAS else "maya"

    def handle_for(self, persona: str) -> str | None:
        if persona != self.linked_persona:
            return None
        return sorted(self.s.allowed_numbers)[0] if self.s.allowed_numbers else None

    def persona_for(self, handle: str) -> str | None:
        return self.linked_persona if handle in self.s.allowed_numbers else None

    # -------------------------------------------------------------------- feed
    def feed(self, persona: str, ts: str | None, actor: str, kind: str, text: str, data: dict | None = None) -> None:
        row = (persona, ts, actor, kind, text, json.dumps(data or {}), now_iso())
        with self.db.conn() as c:
            cur = c.execute("INSERT INTO feed(persona,ts,actor,kind,text,data,created_at) VALUES(?,?,?,?,?,?,?)", row)
        self.sync.push("feed", {"id": cur.lastrowid, "persona": persona, "ts": ts, "actor": actor, "kind": kind,
                                "text": text, "data": data or {}, "created_at": row[-1]})

    # ----------------------------------------------------------- hourly review
    async def review_hour(self, persona: str, tick: dict, mon: Monitor) -> None:
        """Every completed hour Jev looks at the most deviating metric, even if nothing fired. This is the 'always watching' log."""
        if not self.jev or not mon.last_review or tick["ts"][14:16] != "45" or not self._due(persona, "review", REVIEW_GAP_S):
            return
        m, o = max(mon.last_review.items(), key=lambda kv: abs(kv[1]["z"]))
        state = self._jev_state(m, o["value"], o["median"], o["z"], 1, o["state"], "good", [], 0, False, "review", [])
        try:
            d = await self.jev.decide(state, self._questions())
        except JevError as exc:
            self.feed(persona, tick["ts"], "jev", "error", f"Jev unavailable: {exc}")
            return
        pick, conf, _ = d.picked("action")
        self.feed(persona, tick["ts"], "jev", "review",
                  f"Reviewed {LABEL.get(m, m)}: {o['z']:+.1f}\u03c3 vs own baseline. p(alert) {d.p_yes('should_alert'):.2f} \u2192 {pick}",
                  {"metric": m, "z": round(o["z"], 2), "p_alert": round(d.p_yes("should_alert"), 3), "action": pick,
                   "confidence": round(conf, 3), "cost": d.cost})

    async def note_period(self, persona: str, tick: dict, mon: Monitor) -> None:
        """Every 6 simulated hours the language model writes a one-line observation from computed numbers."""
        if (tick["ts"][14:16] != "45" or int(tick["ts"][11:13]) % 6 != 5 or not mon.last_review
                or not self._due(persona, "note", NOTE_GAP_S)):
            return
        top = sorted(mon.last_review.items(), key=lambda kv: abs(kv[1]["z"]), reverse=True)[:4]
        digest = [{"metric": LABEL.get(m, m), "z": round(o["z"], 1), "value": round(o["value"], 1), "baseline": round(o["median"], 1),
                   "unit": UNIT.get(m, "")} for m, o in top]
        prompt = [
            {"role": "system", "content": "You are the observer of a wearable-monitoring system. In one plain sentence "
             "(max 28 words) say what the numbers show right now. Use only numbers given. No diagnosis, no causes, no advice."},
            {"role": "user", "content": json.dumps({"time": tick["ts"], "context": tick["state"], "largest_deviations": digest})},
        ]
        try:
            text, _ = await self.llm.chat(prompt, self.s.memory_models, max_tokens=80, temperature=0.2)
        except LLMError:
            return
        self.feed(persona, tick["ts"], "llm", "observation", text.strip())

    # -------------------------------------------------------------- candidates
    async def on_candidates(self, persona: str, cands: list[Candidate]) -> None:
        for c in cands:
            async with self._locks.setdefault(persona, asyncio.Lock()):
                try:
                    await self._handle(c)
                except Exception:
                    log.exception("alert handling failed")

    def _recent(self, persona: str, family: str, since: datetime, statuses=("sent", "discussed", "resolved", "dry_run")) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE persona=? AND family=? AND ts>=? AND status IN (%s) ORDER BY id DESC"
                             % ",".join("?" * len(statuses)), (persona, family, since.isoformat(timespec="minutes"), *statuses)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _floor(cand: Candidate) -> bool:
        quality_ok = "good" in cand.quality or "sleep samples" in cand.quality
        return abs(cand.z) >= FLOOR_Z and cand.persistence_h >= FLOOR_PERSIST and quality_ok

    def _pregate(self, cand: Candidate, family: str) -> str | None:
        """Rules about the signal itself, in simulated time."""
        now = _t(cand.ts)
        same = self._recent(cand.persona, family, now - timedelta(hours=ONGOING_COOLDOWN_H))
        if same:
            age = (now - _t(same[0]["ts"])).total_seconds() / 3600
            if age < COOLDOWN_H or cand.ongoing:
                return f"already alerted about {family} {age:.0f}h ago"
        with self.db.conn() as c:
            ann = c.execute("SELECT a.* FROM annotations a JOIN alerts l ON l.id=a.alert_id WHERE a.persona=? AND l.family=? "
                            "AND a.end_ts>=? ORDER BY a.id DESC LIMIT 1",
                            (cand.persona, family, (now - timedelta(hours=24)).isoformat(timespec="minutes"))).fetchone()
        if ann and ann["summary"]:
            return "the person already explained this recently"
        day_start = now.replace(hour=0, minute=0)
        with self.db.conn() as c:
            n_day = c.execute("SELECT COUNT(*) n FROM alerts WHERE persona=? AND ts>=? AND status IN ('sent','discussed','resolved','dry_run')",
                              (cand.persona, day_start.isoformat(timespec="minutes"))).fetchone()["n"]
            last = c.execute("SELECT ts FROM alerts WHERE persona=? AND status IN ('sent','discussed','resolved','dry_run') ORDER BY id DESC LIMIT 1",
                             (cand.persona,)).fetchone()
        if n_day >= DAILY_CAP:
            return f"daily cap of {DAILY_CAP} reached"
        if not self._floor(cand):
            if last and (now - _t(last["ts"])).total_seconds() < SPACING_MIN * 60:
                return "too soon after the previous text"
            with self.db.conn() as c:
                open_ = c.execute("SELECT id FROM alerts WHERE persona=? AND status IN ('sent','discussed','dry_run') AND closed_at IS NULL "
                                  "AND ts>=? LIMIT 1", (cand.persona, (now - timedelta(hours=OPEN_HOLD_H)).isoformat(timespec="minutes"))).fetchone()
            if open_:
                return "an earlier text is still open"
        return None

    def _delivery_gate(self, handle: str, floor: bool) -> str | None:
        """Rules about the person: what they agreed to, and when it is polite to text. Urgent changes skip only the polite ones."""
        m = self.memory
        if m.is_opted_out(handle):
            return "optout"
        if not m.has_consent(handle):
            return "consent"
        if m.onboarding(handle)[0] in ("consent", "profile"):
            return "onboarding"                          # one thing at a time: finish setup before any alert
        if m.is_paused(handle):
            return "paused"
        if floor:
            return None
        demo = self.get_setting("demo_mode", "1") == "1"        # on by default: this is a demo, so texts are not held back for the night
        if not demo and not self._quiet_off(handle) and self.in_quiet_hours():
            return "quiet"
        if self.stories and self.stories.open_story(handle):
            return "open_story"
        gap = min(self.s.alert_min_gap_s, 15.0) if demo else self.s.alert_min_gap_s
        if time.monotonic() - self._last_sent_wall.get(handle, -1e9) < gap:
            return "rate"
        return None

    def _quiet_off(self, handle: str) -> bool:
        r = self.memory.user_row(handle)
        return bool(r and r["quiet_off"])

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        h = (now or datetime.now().astimezone()).hour
        a, b = self.s.quiet_start, self.s.quiet_end
        return (h >= a or h < b) if a > b else (a <= h < b)

    def _questions(self) -> dict:
        return {
            "should_alert": noul("Should the person be messaged now about this change?",
                                 true="The change is large, persistent and well supported by good data, and not already explained.",
                                 false="The change is small, brief, already explained, or the data are unreliable."),
            "severity": score("How large is this change relative to the person's own normal?",
                              ["Within normal day-to-day variation", "Mild, worth noting", "Notable, worth asking about",
                               "Large and persistent"]),
            "action": choice("What should the system do?", OPTIONS),
        }

    def _jev_state(self, metric: str, value: float, baseline: float, z: float, persist: int, context: str, quality: str,
                   co: list, n_same_24h: int, explained: bool, kind: str, flags: list) -> dict:
        return {"kind": kind, "metric": LABEL.get(metric, metric), "unit": UNIT.get(metric, ""), "context": context,
                "latest": round(value, 2), "baseline_median": round(baseline, 2), "robust_z": round(z, 2),
                "change_pct": round(100 * (value - baseline) / baseline, 1) if baseline else 0.0,
                "persistence_hours": persist, "data_quality": quality,
                "co_occurring": [f"{LABEL.get(c['metric'], c['metric'])} {c['z']:+.1f}" for c in co],
                "alerts_same_family_last_24h": n_same_24h, "person_already_explained_recently": explained,
                "person_health_notes": flags}

    async def _decide(self, cand: Candidate, family: str, flags: list) -> dict:
        n24 = len(self._recent(cand.persona, family, _t(cand.ts) - timedelta(hours=24)))
        quality = "good" if "good" in cand.quality or "sleep samples" in cand.quality else "poor"
        state = self._jev_state(cand.metric, cand.value, cand.baseline, cand.z, cand.persistence_h, cand.state, quality, cand.co, n24,
                                False, cand.kind + ("_continuing" if cand.ongoing else ""), flags)
        floor = self._floor(cand)
        if self.jev:
            try:
                d: Decision = await self.jev.decide(state, self._questions())
                pick, conf, probs = d.picked("action")
                out = {"source": "jev", "model": d.model, "p_alert": round(d.p_yes("should_alert"), 3),
                       "severity": round(d.level("severity"), 2), "action": pick, "confidence": round(conf, 3),
                       "probs": {k: round(v, 3) for k, v in probs.items()}, "cost": d.cost, "floor": floor}
            except JevError as exc:
                log.warning("jev unavailable (%s); rules decide", exc)
                out = self._rules(cand, floor)
        else:
            out = self._rules(cand, floor)
        out["state"] = state
        out["send"] = (out["action"] == "alert_now" and out["p_alert"] >= 0.5 and out["confidence"] >= 0.5) or floor
        return out

    @staticmethod
    def _rules(cand: Candidate, floor: bool) -> dict:
        strong = abs(cand.z) >= 5 and cand.persistence_h >= 2
        return {"source": "rules", "model": "rules-v1", "p_alert": round(min(1.0, abs(cand.z) / 8), 3),
                "severity": 2.0 if strong else 1.0, "action": "alert_now" if strong else "watch", "confidence": 0.5,
                "probs": {}, "cost": 0.0, "floor": floor}

    # ------------------------------------------------------- choosing a question
    def _minutes_since_active(self, persona: str, upto_i: int) -> int | None:
        if not self.replay:
            return None
        ticks = self.replay.ticks(persona)
        for i in range(min(upto_i, len(ticks) - 1), max(-1, upto_i - 96), -1):
            if ticks[i]["state"] == "active":
                return (upto_i - i) * TICK_MINUTES
        return None

    async def choose_question(self, cand: Candidate, family: str, flags: list) -> tuple[str, dict]:
        """Jev picks one question from the approved list for this family. Rules narrow the list first and back it up."""
        allowed = list(voice.QUESTIONS_FOR.get(family, ["open"]))
        if cand.state == "sleep":
            allowed = [q for q in allowed if q not in ("activity", "stress", "heat")] or ["symptoms"]
        with self.db.conn() as c:
            asked = [r["question_kind"] for r in c.execute("SELECT question_kind FROM alerts WHERE persona=? AND question_kind IS NOT NULL "
                                                          "ORDER BY id DESC LIMIT 4", (cand.persona,))]
        fresh = [q for q in allowed if q not in asked[:2]] or allowed
        if "lung_condition" in flags and family == "breathing" and "symptoms" in fresh:
            fresh = ["symptoms"]                         # a person with a lung note is asked how they feel first
        info = {"question_kind": fresh[0], "source": "rules", "options": fresh}
        if len(fresh) == 1 or not self.jev:
            return fresh[0], info
        since = self._minutes_since_active(cand.persona, index_of(cand.ts))
        state = {"metric": LABEL.get(cand.metric, cand.metric), "direction": "up" if cand.value > cand.baseline else "down",
                 "context": cand.state, "hour_of_day": int(cand.ts[11:13]), "minutes_since_exercise": since,
                 "also_moving": [LABEL.get(c["metric"], c["metric"]) for c in cand.co], "lasting_hours": cand.persistence_h,
                 "recently_asked": asked[:3], "person_health_notes": flags}
        try:
            d = await self.jev.decide(state, {"q": choice("Which single question is most useful to ask this person now?",
                                                          {k: QUESTION_HELP[k] for k in fresh})})
            pick, conf, probs = d.picked("q")
            if pick in fresh and conf >= 0.4:
                return pick, {"question_kind": pick, "source": "jev", "confidence": round(conf, 3), "options": fresh,
                              "probs": {k: round(v, 3) for k, v in probs.items()}}
        except JevError as exc:
            log.info("jev question pick unavailable: %s", exc)
        return fresh[0], info

    # ------------------------------------------------------------------ handling
    def _event_id(self, cand: Candidate) -> str:
        who = re.sub(r"[^a-z0-9]+", "", PERSONAS[cand.persona].name.lower()) or cand.persona
        base = f"evt-{who}-{cand.started.replace('-', '').replace(':', '').replace('T', '')}-{cand.metric}"
        with self.db.conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM alerts WHERE event_id=? OR event_id LIKE ?", (base, base + "-%")).fetchone()["n"]
        return base if n == 0 else f"{base}-{n + 1}"

    def _receipt(self, cand: Candidate, event_id: str) -> dict:
        """Everything needed to check the alert by hand: the readings, the baseline, the arithmetic, the rule."""
        readings = []
        if self.replay and cand.kind == "hourly":
            ticks = self.replay.ticks(cand.persona)
            i1, i0 = index_of(cand.ts), index_of(cand.started)
            by_hour: dict[str, list[float]] = {}
            for t in ticks[max(0, i0 - 3): i1 + 1]:
                v = t["v"].get(cand.metric)
                if v is not None and t["quality"] == "good":
                    by_hour.setdefault(t["ts"][:13], []).append(v)
            readings = [{"hour": h, "median": round(stats.median(vs), 2), "samples": len(vs)} for h, vs in sorted(by_hour.items())][-12:]
        rule = ("a robust z of at least 4.5 for 3 hours, 5 for 2 hours, or 7 for 1 hour" if cand.kind == "hourly"
                else "a nightly value at least 3.5 robust z from this person's earlier nights" if cand.kind == "daily"
                else "no usable samples for 2 hours")
        return {"event_id": event_id, "metric": cand.metric, "kind": cand.kind, "context": cand.state,
                "window": {"started": cand.started, "detected": cand.ts, "hours": cand.persistence_h, "quality": cand.quality},
                "readings": readings,
                "baseline": {"definition": "median of this person's earlier readings in the same context (asleep, or awake at this time of day)",
                             "median": cand.baseline, "scale": cand.scale, "samples": cand.n_hist},
                "calculation": {"formula": "z = (reading - median) / scale", "z": cand.z, "percent_change": cand.pct},
                "rule": rule, "source": PERSONAS[cand.persona].source or "Seeded simulator",
                "persona": cand.persona, "synthetic": PERSONAS[cand.persona].kind == "synthetic"}

    async def _handle(self, cand: Candidate) -> None:
        family = FAMILY.get(cand.metric, "other")
        label = LABEL.get(cand.metric, cand.metric)
        reason = self._pregate(cand, family)
        if reason:
            self._store(cand, family, "suppressed", {"reason": reason}, None)
            self.feed(cand.persona, cand.ts, "policy", "suppressed", f"{label}: held back, {reason}.", {"metric": cand.metric})
            return
        handle = self.handle_for(cand.persona)
        flags = self.memory.risk_flags(handle) if (handle and self.memory) else []
        if cand.kind == "gap":
            decision = {"source": "rules", "action": "alert_now", "send": True, "reason": "tracker silent", "p_alert": 1.0,
                        "confidence": 1.0, "model": "rules-v1", "floor": False}
        else:
            decision = await self._decide(cand, family, flags)
            self.feed(cand.persona, cand.ts, "jev", "decision",
                      f"{label} {cand.z:+.1f}\u03c3 for {cand.persistence_h}h: p(alert) {decision['p_alert']:.2f}, "
                      f"{decision['action']} ({decision['confidence']:.2f})" + (", floor rule" if decision.get("floor") else ""),
                      {"metric": cand.metric, **{k: decision[k] for k in ("p_alert", "action", "confidence", "source")}})
        if not decision["send"]:
            status = "data_issue" if decision["action"] == "data_issue" else "watching" if decision["action"] == "watch" else "ignored"
            self._store(cand, family, status, decision, None)
            if status == "watching":
                self._rearm(cand)           # look again next hour, with one more hour of evidence
            return
        event_id = self._event_id(cand)
        receipt = self._receipt(cand, event_id)
        floor = bool(decision.get("floor"))
        qkey, qinfo = await self.choose_question(cand, family, flags)
        if qinfo.get("source") == "jev":
            self.feed(cand.persona, cand.ts, "jev", "question", f"Jev chose the question \u201c{qkey}\u201d for {label} (confidence {qinfo['confidence']:.2f})",
                      {"question_kind": qkey})
        decision["question"] = qinfo
        text, bold = self._compose(cand, qkey, handle, first=self._first_alert(cand.persona))
        # the person's choices come first; urgent changes skip only quiet hours, the open conversation and the real-time gap
        gate = self._delivery_gate(handle, floor) if (handle and self.send_enabled) else None
        if gate:
            alert_id = self._store(cand, family, f"held_{gate}", decision, text, handle=handle, event_id=event_id, receipt=receipt, qkey=qkey)
            self.feed(cand.persona, cand.ts, "policy", "held", f"{label}: held ({gate.replace('_', ' ')}), will be sent later as one message.",
                      {"alert_id": alert_id, "gate": gate})
            return
        await self._deliver(cand, family, decision, text, bold, qkey, handle, event_id, receipt)

    def _first_alert(self, persona: str) -> bool:
        with self.db.conn() as c:
            return not c.execute("SELECT 1 FROM alerts WHERE persona=? AND status IN ('sent','discussed','resolved') LIMIT 1", (persona,)).fetchone()

    def _compose(self, cand: Candidate, qkey: str, handle: str | None, *, first: bool) -> tuple[str, list[str]]:
        name = self.memory.fact_value(handle, "preferred_name") if (handle and self.memory) else None
        if name and name.lower().startswith("none"):
            name = None
        return voice.alert_text(cand.metric, cand.value, cand.baseline, cand.persistence_h, cand.kind, qkey, name=name, first_alert=first)

    async def _deliver(self, cand: Candidate, family: str, decision: dict, text: str, bold: list[str], qkey: str, handle: str | None,
                       event_id: str, receipt: dict, existing_id: int | None = None) -> int:
        status, msg_id = "dry_run", None
        if existing_id is None:
            alert_id = self._store(cand, family, "sending" if (handle and self.send_enabled) else "dry_run", decision, text, handle=handle,
                                   event_id=event_id, receipt=receipt, qkey=qkey)
        else:
            alert_id = existing_id
        if handle and self.send_enabled and self.messenger:
            png = self._card(cand, event_id) if cand.kind != "gap" else None
            try:
                # The key names this alert row (ids are never reused), so a retry of THIS alert is safe, while a later demo run that
                # produces the same event id is a new alert and gets sent.
                skey = f"alert:{alert_id}:{event_id}"
                if png:
                    msg_id = await self.messenger.card(handle, png, text, key=skey, kind="alert", alert_id=alert_id, bold=bold,
                                                       filename=f"{cand.metric}.png")
                else:
                    msg_id = await self.messenger.say(handle, text, key=skey, kind="alert", alert_id=alert_id, bold=bold)
            except Exception:
                log.exception("alert send crashed")
                msg_id = None
            status = "sent" if msg_id else "send_failed"
            if msg_id:
                self._last_sent_wall[handle] = time.monotonic()
                options = voice.QUESTIONS[qkey][1]
                if options:
                    # with polls on this is a tap-to-answer poll; otherwise a one-line "Reply with: ..." hint that typed answers match
                    await self.messenger.poll(handle, options, purpose="alert_q", ref=str(alert_id), key=f"alertq:{alert_id}:{event_id}", alert_id=alert_id)
        self._update_status(alert_id, status, msg_id)
        self.feed(cand.persona, cand.ts, "alert", status, f"{'Texted' if status == 'sent' else 'Drafted (not sent)' if status == 'dry_run' else 'Not sent'}: {text.splitlines()[0]}",
                  {"alert_id": alert_id, "metric": cand.metric, "event_id": event_id})
        if status == "sent" and self.stories:
            self.stories.on_alert_sent(alert_id)
        if status == "dry_run":                                  # nothing was sent, so the messenger did not record it
            self.sync.push("thread", {"alert_id": alert_id, "mid": f"alert-{alert_id}", "direction": "out", "text": text, "kind": "alert",
                                      "created_at": now_iso()})
        return alert_id

    def _card(self, cand: Candidate, event_id: str) -> bytes | None:
        """A picture of the last two days with the person's usual range and the time we are asking about."""
        if not self.replay:
            return None
        try:
            ticks = self.replay.ticks(cand.persona)
            i1 = index_of(cand.ts)
            key = cand.metric
            label, unit = voice.plain(cand.metric)
            if cand.kind == "daily":
                hist = list(self.replay.monitor(cand.persona).daily_hist.get(cand.metric, []))[-14:]
                values, band, mark = [float(v) for v in hist], None, (max(0, len(hist) - 2), len(hist) - 1)
                labels_ok = False
            else:
                i0 = max(0, i1 - 191)
                values = [ticks[i]["v"].get(key) if ticks[i]["quality"] == "good" else None for i in range(i0, i1 + 1)]
                hist = [(t["ts"], t["v"].get(key)) for t in ticks[: max(0, i1 - 191)] if t["quality"] == "good" and t["v"].get(key) is not None]
                by_hour: dict[int, list[float]] = {}
                for ts, v in hist:
                    by_hour.setdefault(int(ts[11:13]), []).append(v)
                bands = {}
                floor = stats.MIN_SCALE.get(key, 0.1)
                for h, vs in by_hour.items():
                    if len(vs) >= 8:
                        med = stats.median(vs)
                        sp = max(stats.MAD_SCALE * stats.mad(vs, med), floor)
                        bands[h] = (med - 2.5 * sp, med + 2.5 * sp)
                band = [bands.get(int(ticks[i]["ts"][11:13])) for i in range(i0, i1 + 1)]
                started = index_of(cand.started)
                mark = (max(0, started - i0), i1 - i0)
            higher = cand.value > cand.baseline
            usual = f"Usual {voice.num(cand.baseline)}"
            if cand.metric == "sleep_hours":
                value_txt, unit_txt = voice.num(cand.value), "hours of sleep"
            elif cand.metric == "spo2":
                value_txt, unit_txt = voice.num(cand.value), "% oxygen"
            else:
                value_txt, unit_txt = voice.num(cand.value), unit
            days = max(1, cand.n_hist // 24) if cand.kind == "hourly" else cand.n_hist
            return cards.metric_card(
                label=voice.cap(label), value=value_txt, unit=unit_txt, status_text=f"{'Higher' if higher else 'Lower'} than your usual",
                usual=usual, values=values, band=band, color=COLORS.get(FAMILY.get(cand.metric, "other"), cards.BRAND), mark=mark,
                when=pretty(cand.ts) + " (simulated)",
                footer=f"Compared with your own usual range at this time of day, over about {days} {'days' if cand.kind=='hourly' else 'nights'}. "
                       f"Demo data, not medical advice.\n{event_id}")
        except Exception:
            log.exception("card failed; sending text only")
            return None

    def _rearm(self, cand: Candidate) -> None:
        if not self.replay:
            return
        streak = self.replay.monitor(cand.persona).streak.get(cand.metric)
        if streak:
            streak["last_emit"] = None

    def _store(self, cand: Candidate, family: str, status: str, decision: dict, text: str | None, *, handle: str | None = None,
               message_id: str | None = None, event_id: str | None = None, receipt: dict | None = None, qkey: str | None = None) -> int:
        evidence = cand.to_dict()
        if receipt:
            evidence["receipt"] = receipt
            evidence["event_id"] = event_id
        row = {"persona": cand.persona, "kind": cand.kind, "metric": cand.metric, "family": family, "ts": cand.ts,
               "started": cand.started, "evidence": json.dumps(evidence), "decision": json.dumps(decision, default=str),
               "status": status, "text": text, "message_id": message_id, "handle": handle, "event_id": event_id,
               "receipt": json.dumps(receipt) if receipt else None, "question_kind": qkey,
               "sent_at": now_iso() if status == "sent" else None, "created_at": now_iso()}
        with self.db.conn() as c:
            cur = c.execute("INSERT INTO alerts(persona,kind,metric,family,ts,started,evidence,decision,status,text,message_id,"
                            "handle,sent_at,created_at,event_id,receipt,question_kind) VALUES(:persona,:kind,:metric,:family,:ts,:started,"
                            ":evidence,:decision,:status,:text,:message_id,:handle,:sent_at,:created_at,:event_id,:receipt,:question_kind)", row)
        public = {k: v for k, v in row.items() if k not in ("handle", "message_id", "receipt")}   # phone numbers stay local
        self.sync.push("alert", {"id": cur.lastrowid, **public})
        return cur.lastrowid

    def _update_status(self, alert_id: int, status: str, msg_id: str | None = None) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE alerts SET status=?, message_id=COALESCE(?, message_id), sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?",
                      (status, msg_id, status, now_iso(), alert_id))
        self.sync.push("alert_status", {"id": alert_id, "status": status})

    # -------------------------------------------------- releasing what was held
    async def release_held(self, handle: str) -> None:
        """Called every few seconds. When the person's gates open, send the most important held alert (one message)."""
        persona = self.persona_for(handle)
        if not persona or not self.send_enabled or not self.messenger:
            return
        async with self._locks.setdefault(persona, asyncio.Lock()):
            with self.db.conn() as c:
                held = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE persona=? AND handle=? AND status LIKE 'held_%' ORDER BY id", (persona, handle))]
                retry = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE persona=? AND handle=? AND status='send_failed' AND created_at>=? ORDER BY id",
                                                    (persona, handle, (datetime.now().astimezone() - timedelta(minutes=15)).isoformat()))]
            cutoff = datetime.now().astimezone() - timedelta(hours=HELD_TTL_H)
            for a in held:
                if datetime.fromisoformat(a["created_at"]) < cutoff:
                    self._update_status(a["id"], "expired")
            held = [a for a in held if datetime.fromisoformat(a["created_at"]) >= cutoff]
            pool = retry + held
            if not pool:
                return
            lead = max(pool, key=lambda a: abs(json.loads(a["evidence"]).get("z", 0)))
            floor = bool(json.loads(lead["decision"] or "{}").get("floor"))
            if self._delivery_gate(handle, floor):
                return
            ev = json.loads(lead["evidence"])
            cand = Candidate(**{k: ev[k] for k in Candidate.__dataclass_fields__ if k in ev})
            decision = json.loads(lead["decision"] or "{}")
            text = lead["text"]
            others = [a for a in pool if a["id"] != lead["id"]]
            if others:
                names = ", ".join(sorted({voice.plain(a["metric"])[0] for a in others}))
                text += f"\n\nI also noticed changes in: {names}. We can talk about those next."
            for a in others:
                self._update_status(a["id"], "digested")
            await self._deliver(cand, lead["family"], decision, text, [text.splitlines()[0]], lead["question_kind"] or "open", handle,
                                lead["event_id"] or self._event_id(cand), json.loads(lead["receipt"] or "{}"), existing_id=lead["id"])

    def fresh_run(self, persona: str) -> None:
        """A new demo run starts from a clean slate: no remembered send times, no queued sends from the last run."""
        handle = self.handle_for(persona)
        if handle:
            self._last_sent_wall.pop(handle, None)
            self.cancel_held(handle)
        for key in [k for k in self._last_wall if k[0] == persona]:
            self._last_wall.pop(key, None)

    def cancel_held(self, handle: str) -> int:
        """STOP cancels everything that was waiting to be sent."""
        with self.db.conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM alerts WHERE handle=? AND (status LIKE 'held_%' OR status='send_failed')", (handle,))]
        for i in ids:
            self._update_status(i, "cancelled")
        return len(ids)

    # ------------------------------------------------------------------ lookups
    def open_for(self, handle: str) -> dict | None:
        """The alert this person is most likely replying to: the latest texted one that is not closed, within 12 real hours."""
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE handle=? AND status IN ('sent','discussed') AND closed_at IS NULL "
                          "AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (handle,)).fetchone()
        if not r:
            return None
        age = (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(r["sent_at"])).total_seconds() / 3600
        return dict(r) if age < 12 else None

    def forget(self, handle: str) -> None:
        """'Forget everything': remove what the person said about alerts, here and on the dashboard service."""
        persona = self.persona_for(handle)
        if not persona:
            return
        with self.db.conn() as c:
            c.execute("DELETE FROM annotations WHERE persona=?", (persona,))
            c.execute("DELETE FROM feed WHERE persona=? AND kind IN ('annotation','story')", (persona,))
            c.execute("UPDATE alerts SET message_id=NULL, poll_message_id=NULL, closed_at=COALESCE(closed_at, ?) WHERE persona=? AND handle=?",
                      (now_iso(), persona, handle))
        self.sync.push("forget", {"persona": persona})

    def forget_story(self, alert_ids: list[int]) -> None:
        for aid in alert_ids:
            self.sync.push("forget_story", {"alert_id": aid})

    # -------------------------------------------------------------------- checks
    def check_factors(self, persona: str, ev: dict, factors: list[str]) -> list[dict]:
        return self._compute(persona, ev, factors)

    def _compute(self, persona: str, ev: dict, factors: list[str]) -> list[dict]:
        out: list[dict] = []
        if not self.replay:
            return out
        ticks = self.replay.ticks(persona)[: self.replay.state(persona)["cursor"]]
        if not ticks:
            return out
        try:
            start_i = index_of(ev["started"])
        except ValueError:
            return out
        shift = ev["value"] - ev["baseline"]
        if ev["kind"] == "hourly":
            for f in factors:
                fn = claims.FACTOR_CHECKS.get(f)
                if fn and ev.get("state") == "sleep":
                    out.append({"factor": f, "verdict": "not_checkable",
                                "note": "The change happened during sleep, and this check compares daytime levels."})
                elif fn:
                    try:
                        out.append(fn(ticks, ev["metric"], shift, start_i))
                    except (KeyError, TypeError, ValueError):
                        out.append({"factor": f, "verdict": "insufficient_history", "note": "This band does not report enough of that signal."})
            mon = self.replay.monitor(persona)
            out.append(claims.pattern_check(mon.last_review, {"heart_rate": 1, "hrv_rmssd": -1, "skin_temp": 1,
                                                              "respiratory_rate": 1, "spo2": -1}))
        for f in factors:
            if f not in claims.FACTOR_CHECKS and f != "other":
                out.append({"factor": f, "verdict": "not_measurable", "note": "The tracker has no channel that can confirm or rule this out."})
        return out
