"""From a detected deviation to a message the person can answer, and back to an annotated time series.

Order of work for each candidate from the monitor:
1. Deterministic pre-gates: cooldown, daily cap, spacing, "already explained".
2. Jev decides from a typed state (never raw user text): alert now, keep watching, ignore, or data problem.
   If Jev is unreachable, plain rules decide.
3. Deterministic floor: a very large, sustained, well-measured deviation is never silenced by a model.
4. The language model words one short question. Code supplies every number and checks the result.
5. The message goes to the linked phone (or is only logged in dry-run mode). Everything is recorded.
Later, the person's replies are summarized into an annotation on the same time series, together with
arithmetic checks of what they said (see claims.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from . import claims
from .config import Settings
from .db import Database
from .jev import Decision, JevClient, JevError, choice, noul, score
from .llm import LLMClient, LLMError, extract_json
from .monitor import Candidate, Monitor
from .sim.personas import LABELS, PERSONAS, START_DATE, TICK_MINUTES, UNITS
from .textutil import now_iso

log = logging.getLogger("s2c.alerts")

LABEL = {**LABELS, "sleep_hours": "Sleep duration", "rhr_sleep": "Resting heart rate during sleep",
         "hrv_sleep": "Overnight HRV", "data_quality": "Tracker data"}
UNIT = {**UNITS, "sleep_hours": "h", "rhr_sleep": "bpm", "hrv_sleep": "ms", "data_quality": ""}
FAMILY = {"heart_rate": "heart", "hrv_rmssd": "heart", "rhr_sleep": "heart", "hrv_sleep": "heart",
          "respiratory_rate": "breathing", "spo2": "breathing", "skin_temp": "temperature",
          "bp_sys": "blood_pressure", "bp_dia": "blood_pressure", "stress": "stress", "eda": "stress",
          "sleep_hours": "sleep", "data_quality": "device"}
QUESTION = {
    "heart": "Has anything been different today, like exercise, caffeine, stress, or not feeling well?",
    "breathing": "Are you feeling okay, and is the sensor sitting snugly?",
    "temperature": "Are you feeling warmer than usual, or have you been somewhere hot?",
    "blood_pressure": "Were you stressed, active, or did you have caffeine just before?",
    "stress": "How are you feeling right now, and what has your day been like?",
    "sleep": "How did you sleep, and did anything keep you up?",
    "device": "Is your tracker on and sitting snugly?",
    "other": "Has anything been different today?",
}
SAFETY_LINE = " If you ever have chest pain, trouble breathing or feel faint, call 911."
SAFETY_FAMILIES = {"breathing", "blood_pressure", "heart"}
PREFIX = "Signal2Care (demo data): "

COOLDOWN_H = 6
ONGOING_COOLDOWN_H = 12
SPACING_MIN = 180          # one text per change, not one per metric
OPEN_HOLD_H = 12           # while a text is still unresolved, new non-extreme findings wait
DAILY_CAP = 3
# Percent of a Celsius reading or of a percentage is not meaningful, so these are told as differences.
ABS_DIFF = {"skin_temp": "°C", "spo2": " percentage points"}
FLOOR_Z = 7.0
FLOOR_PERSIST = 2
BANNED = re.compile(r"diagnos|disease|infection|covid|\bflu\b|sepsis|cancer|heart attack|arrhythmia|medicat|prescri|dose|treat", re.I)
_START = datetime.fromisoformat(START_DATE)

OPTIONS = {
    "alert_now": "Ask the person about this change now.",
    "watch": "Keep watching, do not message yet.",
    "ignore": "Within normal variation, no action.",
    "data_issue": "The reading is unreliable, treat it as a data problem.",
}


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def index_of(ts: str) -> int:
    return int((_t(ts) - _START).total_seconds() // (TICK_MINUTES * 60))


def _fmt(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") if abs(x) < 1000 else f"{x:.0f}"


class AlertEngine:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, jev: JevClient | None, sync):
        self.s = settings
        self.db = db
        self.llm = llm
        self.jev = jev
        self.sync = sync
        self.say = None            # async (handle, text) -> message id | None, set by the app
        self.replay = None         # set by the app
        self._locks: dict[str, asyncio.Lock] = {}
        self._annotating: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        with self.db.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    @property
    def send_enabled(self) -> bool:
        return self.get_setting("send_alerts", "0") == "1"

    def handle_for(self, persona: str) -> str | None:
        if not PERSONAS[persona].linked:
            return None
        return sorted(self.s.allowed_numbers)[0] if self.s.allowed_numbers else None

    def persona_for(self, handle: str) -> str | None:
        for pid, p in PERSONAS.items():
            if p.linked and handle in self.s.allowed_numbers:
                return pid
        return None

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
        if not self.jev or not mon.last_review or tick["ts"][14:16] != "45":
            return
        m, o = max(mon.last_review.items(), key=lambda kv: abs(kv[1]["z"]))
        state = self._jev_state(None, m, o["value"], o["median"], o["z"], 1, o["state"], "good", [], 0, False, "review")
        try:
            d = await self.jev.decide(state, self._questions())
        except JevError as exc:
            self.feed(persona, tick["ts"], "jev", "error", f"Jev unavailable: {exc}")
            return
        pick, conf, _ = d.picked("action")
        self.feed(persona, tick["ts"], "jev", "review",
                  f"Reviewed {LABEL.get(m, m)}: {o['z']:+.1f}σ vs own baseline. p(alert) {d.p_yes('should_alert'):.2f} → {pick}",
                  {"metric": m, "z": round(o["z"], 2), "p_alert": round(d.p_yes("should_alert"), 3), "action": pick,
                   "confidence": round(conf, 3), "cost": d.cost})

    async def note_period(self, persona: str, tick: dict, mon: Monitor) -> None:
        """Every 6 simulated hours the language model writes a one-line observation from computed numbers."""
        if tick["ts"][14:16] != "45" or int(tick["ts"][11:13]) % 6 != 5 or not mon.last_review:
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

    def _recent(self, persona: str, family: str, since: datetime, statuses=("sent", "dry_run")) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE persona=? AND family=? AND ts>=? AND status IN (%s) ORDER BY id DESC"
                             % ",".join("?" * len(statuses)), (persona, family, since.isoformat(timespec="minutes"), *statuses)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _floor(cand: Candidate) -> bool:
        quality_ok = "good" in cand.quality or "sleep samples" in cand.quality
        return abs(cand.z) >= FLOOR_Z and cand.persistence_h >= FLOOR_PERSIST and quality_ok

    def _pregate(self, cand: Candidate, family: str) -> str | None:
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
            n_day = c.execute("SELECT COUNT(*) n FROM alerts WHERE persona=? AND ts>=? AND status IN ('sent','dry_run')",
                              (cand.persona, day_start.isoformat(timespec="minutes"))).fetchone()["n"]
            last = c.execute("SELECT ts FROM alerts WHERE persona=? AND status IN ('sent','dry_run') ORDER BY id DESC LIMIT 1",
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

    def _jev_state(self, cand: Candidate | None, metric: str, value: float, baseline: float, z: float, persist: int,
                   context: str, quality: str, co: list, n_same_24h: int, explained: bool, kind: str) -> dict:
        return {"kind": kind, "metric": LABEL.get(metric, metric), "unit": UNIT.get(metric, ""), "context": context,
                "latest": round(value, 2), "baseline_median": round(baseline, 2), "robust_z": round(z, 2),
                "change_pct": round(100 * (value - baseline) / baseline, 1) if baseline else 0.0,
                "persistence_hours": persist, "data_quality": quality,
                "co_occurring": [f"{LABEL.get(c['metric'], c['metric'])} {c['z']:+.1f}" for c in co],
                "alerts_same_family_last_24h": n_same_24h, "person_already_explained_recently": explained}

    async def _decide(self, cand: Candidate, family: str) -> dict:
        n24 = len(self._recent(cand.persona, family, _t(cand.ts) - timedelta(hours=24)))
        quality = "good" if "good" in cand.quality or "sleep samples" in cand.quality else "poor"
        state = self._jev_state(cand, cand.metric, cand.value, cand.baseline, cand.z, cand.persistence_h, cand.state,
                                quality, cand.co, n24, False, cand.kind + ("_continuing" if cand.ongoing else ""))
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

    async def _handle(self, cand: Candidate) -> None:
        family = FAMILY.get(cand.metric, "other")
        label = LABEL.get(cand.metric, cand.metric)
        reason = self._pregate(cand, family)
        if reason:
            self._store(cand, family, "suppressed", {"reason": reason}, None)
            self.feed(cand.persona, cand.ts, "policy", "suppressed", f"{label}: held back, {reason}.", {"metric": cand.metric})
            return
        if cand.kind == "gap":
            decision = {"source": "rules", "action": "alert_now", "send": True, "reason": "tracker silent", "p_alert": 1.0,
                        "confidence": 1.0, "model": "rules-v1"}
        else:
            decision = await self._decide(cand, family)
            self.feed(cand.persona, cand.ts, "jev", "decision",
                      f"{label} {cand.z:+.1f}σ for {cand.persistence_h}h: p(alert) {decision['p_alert']:.2f}, "
                      f"{decision['action']} ({decision['confidence']:.2f})" + (", floor rule" if decision.get("floor") else ""),
                      {"metric": cand.metric, **{k: decision[k] for k in ("p_alert", "action", "confidence", "source")}})
        if not decision["send"]:
            status = "data_issue" if decision["action"] == "data_issue" else "watching" if decision["action"] == "watch" else "ignored"
            self._store(cand, family, status, decision, None)
            if status == "watching":
                self._rearm(cand)           # look again next hour, with one more hour of evidence
            return
        text = await self._compose(cand, family)
        handle = self.handle_for(cand.persona)
        msg_id, status = None, "dry_run"
        if handle and self.send_enabled and self.say:
            msg_id = await self.say(handle, text)
            status = "sent" if msg_id else "send_failed"
        alert_id = self._store(cand, family, status, decision, text, handle=handle, message_id=msg_id)
        self.feed(cand.persona, cand.ts, "alert", status, f"{'Texted' if status == 'sent' else 'Drafted (not sent)'}: {text}",
                  {"alert_id": alert_id, "metric": cand.metric})

    def _rearm(self, cand: Candidate) -> None:
        if not self.replay:
            return
        streak = self.replay.monitor(cand.persona).streak.get(cand.metric)
        if streak:
            streak["last_emit"] = None

    def _store(self, cand: Candidate, family: str, status: str, decision: dict, text: str | None, *,
               handle: str | None = None, message_id: str | None = None) -> int:
        row = {"persona": cand.persona, "kind": cand.kind, "metric": cand.metric, "family": family, "ts": cand.ts,
               "started": cand.started, "evidence": json.dumps(cand.to_dict()), "decision": json.dumps(decision, default=str),
               "status": status, "text": text, "message_id": message_id, "handle": handle,
               "sent_at": now_iso() if status == "sent" else None, "created_at": now_iso()}
        with self.db.conn() as c:
            cur = c.execute("INSERT INTO alerts(persona,kind,metric,family,ts,started,evidence,decision,status,text,message_id,"
                            "handle,sent_at,created_at) VALUES(:persona,:kind,:metric,:family,:ts,:started,:evidence,:decision,"
                            ":status,:text,:message_id,:handle,:sent_at,:created_at)", row)
        public = {k: v for k, v in row.items() if k not in ("handle", "message_id")}   # phone numbers stay local
        self.sync.push("alert", {"id": cur.lastrowid, **public})
        if text and status in ("sent", "dry_run"):
            self.sync.push("thread", {"alert_id": cur.lastrowid, "mid": f"alert-{cur.lastrowid}", "direction": "out",
                                      "text": text, "created_at": row["created_at"]})
        return cur.lastrowid

    # --------------------------------------------------------------- the message
    def _facts(self, cand: Candidate) -> dict:
        label, unit = LABEL.get(cand.metric, cand.metric), UNIT.get(cand.metric, "")
        diff_unit = ABS_DIFF.get(cand.metric)
        if diff_unit:
            diff = f"{_fmt(abs(cand.value - cand.baseline))}{diff_unit if diff_unit.startswith(' ') else ' ' + diff_unit}"
        else:
            diff = f"{_fmt(abs(cand.pct))}%"
        return {"label": label, "unit": unit, "value": _fmt(cand.value), "baseline": _fmt(cand.baseline),
                "pct": _fmt(abs(cand.pct)), "diff": diff, "direction": "above" if cand.value > cand.baseline else "below",
                "hours": cand.persistence_h}

    def _template(self, cand: Candidate, family: str) -> str:
        f = self._facts(cand)
        if cand.kind == "gap":
            return f"Your tracker hasn't sent usable data for about {f['hours']} hours. {QUESTION['device']}"
        dur = f" for about {f['hours']} hours" if cand.kind == "hourly" and f["hours"] > 1 else ""
        return (f"{f['label']} is {f['value']} {f['unit']}, about {f['diff']} {f['direction']} your usual "
                f"{f['baseline']} {f['unit']}{dur}. {QUESTION[family]}").replace("  ", " ")

    @staticmethod
    def _numbers_ok(text: str, cand: Candidate) -> bool:
        allowed = {cand.value, cand.baseline, abs(cand.pct), float(cand.persistence_h), abs(cand.value - cand.baseline)}
        allowed |= {round(x) for x in list(allowed)}
        for tok in re.findall(r"\d+(?:\.\d+)?", text):
            v = float(tok)
            if not any(abs(v - a) <= max(0.06, 0.006 * abs(a)) for a in allowed):
                return False
        return True

    async def _compose(self, cand: Candidate, family: str) -> str:
        base = self._template(cand, family)
        if cand.kind == "gap":                      # nothing to word: the template says it exactly
            return PREFIX + base
        f = self._facts(cand)
        facts = {"metric": f["label"], "latest": f"{f['value']} {f['unit']}".strip(), "usual": f"{f['baseline']} {f['unit']}".strip(),
                 "difference": f"{f['diff']} {f['direction']} usual", "lasting_hours": f["hours"],
                 "also_moving": [LABEL.get(c["metric"], c["metric"]) for c in cand.co]}
        prompt = [
            {"role": "system", "content": "You write one short iMessage from a wearable-monitoring assistant to its user. "
             "Rules: at most 3 sentences and 300 characters. Plain text, warm and natural, no emojis. State the change using ONLY "
             "the numbers in FACTS, then ask exactly one question about this topic: " + QUESTION[family] +
             " Do not name causes, diagnoses, conditions or treatments, and do not give advice."},
            {"role": "user", "content": "FACTS: " + json.dumps(facts)},
        ]
        try:
            text, _ = await self.llm.chat(prompt, self.s.text_models, max_tokens=140, temperature=0.4)
            text = re.sub(r"\s+", " ", text).strip()
            ok = (text.count("?") >= 1 and len(text) <= 340 and not BANNED.search(text) and self._numbers_ok(text, cand))
        except LLMError:
            ok, text = False, ""
        out = text if ok else base
        if family in SAFETY_FAMILIES and cand.kind != "gap":
            out += SAFETY_LINE
        return PREFIX + out

    # ------------------------------------------------------- replies and notes
    def open_for(self, handle: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE handle=? AND status IN ('sent','discussed') AND closed_at IS NULL "
                          "AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (handle,)).fetchone()
        if not r:
            return None
        age = (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(r["sent_at"])).total_seconds() / 3600
        return dict(r) if age < 12 else None

    def alert_context(self, alert: dict) -> str:
        ev = json.loads(alert["evidence"])
        dec = json.loads(alert["decision"] or "{}")
        return (f"<open_alert>\nYou texted the person about this wearable change and they are likely replying to it.\n"
                f"Metric: {LABEL.get(ev['metric'], ev['metric'])}. Latest {ev['value']} {UNIT.get(ev['metric'], '')} vs usual "
                f"{ev['baseline']} ({ev['pct']:+.1f}%), {ev['z']:+.1f} robust z, lasting about {ev['persistence_h']} h, simulated time {ev['ts']}. "
                f"Decision: {dec.get('action')} (p {dec.get('p_alert')}). You asked: {alert['text']}\n"
                "Listen first. Acknowledge what they say. Use tools to check numbers or test what they describe against their own history. "
                "Describe what the data shows, never what caused it.\n</open_alert>")

    def after_reply(self, handle: str) -> None:
        alert = self.open_for(handle)
        if not alert:
            return
        with self.db.conn() as c:
            changed = c.execute("UPDATE alerts SET status='discussed' WHERE id=? AND status='sent'", (alert["id"],)).rowcount
        if changed:
            self.sync.push("alert_status", {"id": alert["id"], "status": "discussed"})
        if alert["id"] in self._annotating:
            return
        task = asyncio.create_task(self.annotate(alert["id"]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def annotate(self, alert_id: int) -> None:
        self._annotating.add(alert_id)
        try:
            await asyncio.sleep(1.0)
            with self.db.conn() as c:
                a = c.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                if not a or not a["message_id"]:
                    return
                start = c.execute("SELECT rowid FROM messages WHERE id=?", (a["message_id"],)).fetchone()
                if not start:
                    return
                msgs = c.execute("SELECT id,direction,text,created_at FROM messages WHERE handle=? AND rowid>=? ORDER BY rowid",
                                 (a["handle"], start["rowid"])).fetchall()
            if not any(m["direction"] == "in" for m in msgs):
                return
            for m in msgs[1:]:                                   # the first row is the alert message itself
                self.sync.push("thread", {"alert_id": alert_id, "mid": m["id"], "direction": m["direction"],
                                          "text": m["text"][:1200], "created_at": m["created_at"]})
            ev = json.loads(a["evidence"])
            transcript = "\n".join(f"{'PERSON' if m['direction'] == 'in' else 'ASSISTANT'}: {m['text'][:600]}" for m in msgs)[-6000:]
            prompt = [
                {"role": "system", "content": "You write a factual annotation for a time-series chart from a short chat about a wearable alert. "
                 "Return JSON only: {\"summary\": str (max 240 chars, third person, what the person reported, no causes asserted), "
                 "\"factors\": [one or more of workout, poor_sleep, caffeine, alcohol, stress, illness, travel, medication, device_fit, hydration, other], "
                 "\"resolved\": bool (true if the conversation reached a natural end)}. Only include factors the person actually mentioned."},
                {"role": "user", "content": f"ALERT: {LABEL.get(ev['metric'], ev['metric'])} {ev['value']} vs usual {ev['baseline']} at {ev['ts']}\nCHAT:\n{transcript}"},
            ]
            try:
                text, _ = await self.llm.chat(prompt, self.s.memory_models, max_tokens=300, temperature=0.0, json_mode=True)
                data = extract_json(text)
            except (LLMError, ValueError):
                return
            summary = str(data.get("summary") or "")[:300]
            factors = [f for f in (data.get("factors") or []) if isinstance(f, str)][:4]
            computed = self._compute(a["persona"], ev, factors)
            now = now_iso()
            with self.db.conn() as c:
                c.execute("INSERT INTO annotations(alert_id,persona,metric,start_ts,end_ts,summary,factors,computed,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(alert_id) DO UPDATE SET summary=excluded.summary, "
                          "factors=excluded.factors, computed=excluded.computed, updated_at=excluded.updated_at, end_ts=excluded.end_ts",
                          (alert_id, a["persona"], ev["metric"], ev["started"], ev["ts"], summary, json.dumps(factors),
                           json.dumps(computed), now, now))
                if data.get("resolved"):
                    c.execute("UPDATE alerts SET closed_at=?, status='resolved' WHERE id=?", (now, alert_id))
                row = c.execute("SELECT * FROM annotations WHERE alert_id=?", (alert_id,)).fetchone()
            self.sync.push("annotation", dict(row))
            self.sync.push("alert_status", {"id": alert_id, "status": "resolved" if data.get("resolved") else "discussed"})
            self.feed(a["persona"], ev["ts"], "llm", "annotation", f"Linked the reply to the chart: {summary}", {"alert_id": alert_id})
        finally:
            self._annotating.discard(alert_id)

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
                    out.append(fn(ticks, ev["metric"], shift, start_i))
            mon = self.replay.monitor(persona)
            out.append(claims.pattern_check(mon.last_review, {"heart_rate": 1, "hrv_rmssd": -1, "skin_temp": 1,
                                                              "respiratory_rate": 1, "spo2": -1}))
        for f in factors:
            if f not in claims.FACTOR_CHECKS and f != "other":
                out.append({"factor": f, "verdict": "not_measurable", "note": "The tracker has no channel that can confirm or rule this out."})
        return out
