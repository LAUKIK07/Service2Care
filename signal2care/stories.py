"""A check-in conversation, kept as a story attached to the alert (the event) that started it.

Lifecycle
- The alert text goes out. When the person first replies (or taps an answer), a conversation window opens.
- Every message they send inside the window is recorded against the event and extends the window. After
  `story_idle_min` quiet minutes the window closes.
- On every turn: what they said is read into structured claims, plain arithmetic checks their own history, and the
  interpretation is rewritten from those check results (never by a model). If the interpretation changed, a clear
  "Before / Now / Why" correction is sent. Jev then chooses the next step (ask one more question, wrap up, or gently
  suggest their care team).
- On close: the story is summarized, validated, saved with the questions and answers and every interpretation version,
  pushed to the dashboard, and a short "saved to your record" card is sent.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import cards, voice
from .alerts import AlertEngine, LABEL, pretty
from .config import Settings
from .db import Database
from .jev import JevClient, JevError, choice
from .llm import LLMClient, LLMError, extract_json
from .messenger import Messenger
from .textutil import now_iso

log = logging.getLogger("s2c.stories")

FACTORS = ["workout", "poor_sleep", "caffeine", "alcohol", "stress", "illness", "travel", "medication", "device_fit", "hydration", "other"]
BANNED = re.compile(r"diagnos|disease|infection|covid|\bflu\b|sepsis|cancer|heart attack|arrhythmia|prescri|\bdose\b|treat", re.I)
MAX_FOLLOWUPS = 2
REPLY_WINDOW_H = 12          # a first reply to an alert is accepted for this long

# A tap on an approved option means exactly one thing, so no model is needed to read it.
OPTION_MAP = {
    "yes, i was active": {"factors": ["workout"], "note": "was active"},
    "no, i was resting": {"factors": [], "note": "was resting"},
    "fine": {"feeling": "same", "note": "feels fine"},
    "a little off": {"symptoms": ["feels a little off"], "feeling": "worse"},
    "unwell": {"symptoms": ["feels unwell"], "feeling": "worse"},
    "well": {"note": "slept well"},
    "not well": {"factors": ["poor_sleep"], "note": "slept badly"},
    "hardly at all": {"factors": ["poor_sleep"], "note": "barely slept"},
    "calm": {"note": "day was calm"},
    "a bit stressful": {"factors": ["stress"], "note": "day was a bit stressful"},
    "very stressful": {"factors": ["stress"], "note": "day was very stressful"},
    "yes, it is snug": {"note": "band was snug"},
    "it was loose": {"factors": ["device_fit"], "note": "band was loose"},
    "i took it off": {"factors": ["device_fit"], "note": "had taken the band off"},
}
NEXT = {
    "ask_followup": "Ask one more short question that would add to the story.",
    "close_with_summary": "There is enough. Wrap up and save the story.",
    "suggest_care_team": "Gently suggest they mention this to their care team if it continues or gets worse.",
    "keep_listening": "Just acknowledge what they said and wait.",
}


@dataclass
class TurnPlan:
    alert: dict
    story: dict
    claims: dict
    checks: list
    interpretation: str
    changed: bool
    update_text: str | None
    next_action: str
    followup: str | None = None
    notes: list = field(default_factory=list)


def _loads(s, d):
    try:
        return json.loads(s) if s else d
    except ValueError:
        return d


class StoryEngine:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, jev: JevClient | None, alerts: AlertEngine, messenger: Messenger, memory, sync):
        self.s, self.db, self.llm, self.jev = settings, db, llm, jev
        self.alerts, self.msg, self.memory, self.sync = alerts, messenger, memory, sync
        self.idle = timedelta(minutes=settings.story_idle_min)

    def set_idle_minutes(self, minutes: float) -> None:
        self.idle = timedelta(minutes=max(0.5, minutes))

    # ----------------------------------------------------------------- lookup
    def _alert(self, alert_id: int) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return dict(r) if r else None

    def story_row(self, alert_id: int) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM annotations WHERE alert_id=?", (alert_id,)).fetchone()
        return dict(r) if r else None

    def open_story(self, handle: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT a.*, l.handle FROM annotations a JOIN alerts l ON l.id=a.alert_id WHERE l.handle=? AND a.status='open' "
                          "ORDER BY a.alert_id DESC LIMIT 1", (handle,)).fetchone()
        return dict(r) if r else None

    def target_alert(self, handle: str, reply_to: str | None = None) -> dict | None:
        """The alert this message belongs to: the open conversation, an alert being replied to, or a fresh alert nobody has answered yet."""
        s = self.open_story(handle)
        if s:
            return self._alert(s["alert_id"])
        with self.db.conn() as c:
            if reply_to:
                r = c.execute("SELECT * FROM alerts WHERE handle=? AND message_id=?", (handle, reply_to)).fetchone()
                if r:
                    return dict(r)
            r = c.execute("SELECT * FROM alerts WHERE handle=? AND status='sent' AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (handle,)).fetchone()
        if not r:
            return None
        age = (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(r["sent_at"])).total_seconds() / 3600
        return dict(r) if age < REPLY_WINDOW_H else None

    def on_alert_sent(self, alert_id: int) -> None:
        """Nothing to open yet: the window starts with the person's first reply."""
        a = self._alert(alert_id)
        if a:
            self.alerts.feed(a["persona"], a["ts"], "alert", "awaiting_reply", f"Waiting for a reply to {LABEL.get(a['metric'], a['metric'])} (event {a['event_id']})",
                             {"alert_id": alert_id})

    # ------------------------------------------------------------------ window
    def touch(self, handle: str, msg_id: str, alert: dict) -> dict:
        """Open or extend the conversation window and link this message to the event."""
        now = datetime.now(timezone.utc)
        closes = (now + self.idle).isoformat(timespec="seconds")
        ev = _loads(alert["evidence"], {})
        with self.db.conn() as c:
            c.execute("UPDATE messages SET alert_id=? WHERE id=?", (alert["id"], msg_id))
            m = c.execute("SELECT text, kind, created_at FROM messages WHERE id=?", (msg_id,)).fetchone()
            row = c.execute("SELECT * FROM annotations WHERE alert_id=?", (alert["id"],)).fetchone()
            if row:
                c.execute("UPDATE annotations SET status='open', closes_at=?, updated_at=? WHERE alert_id=?", (closes, now_iso(), alert["id"]))
            else:
                c.execute("INSERT INTO annotations(alert_id,persona,metric,start_ts,end_ts,summary,factors,computed,created_at,updated_at,event_id,"
                          "status,opened_at,closes_at,qa,versions) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (alert["id"], alert["persona"], alert["metric"], alert["started"], alert["ts"], "", "[]", "[]", now_iso(), now_iso(),
                           alert["event_id"], "open", now_iso(), closes, "[]", json.dumps([self._initial_version(alert, ev)])))
            c.execute("UPDATE alerts SET status='discussed' WHERE id=? AND status='sent'", (alert["id"],))
        self.sync.push("alert_status", {"id": alert["id"], "status": "discussed"})
        if m and getattr(self.alerts, "push_thread", None):
            self.alerts.push_thread(alert["id"], msg_id, "in", m["text"], m["kind"] or "reply_in", m["created_at"], None)
        row = self.story_row(alert["id"])
        self._push(row)
        return row

    def _initial_version(self, alert: dict, ev: dict) -> dict:
        label = voice.plain(alert["metric"])[0]
        higher = ev.get("value", 0) > ev.get("baseline", 0)
        hrs = ev.get("persistence_h", 1)
        text = f"Your {label} was {'higher' if higher else 'lower'} than usual{'' if alert['kind'] == 'gap' else ' for ' + voice.hours_phrase(hrs)}. I do not know why yet."
        return {"v": 1, "kind": "from_data", "text": text, "at": now_iso(), "changed": False, "basis": {"signature": []}}

    def add_note(self, alert_id: int, msg_id: str, text: str, claims: dict) -> None:
        """Record something that is not a typed message (a tapback, a tap on feedback) in the story, without reading it with a model."""
        row = self.story_row(alert_id)
        if not row:
            return
        qa = _loads(row.get("qa"), [])
        qa.append({"q": "(tapback)", "a": text[:200], "at": now_iso(), "claims": claims, "msg_id": msg_id})
        with self.db.conn() as c:
            c.execute("UPDATE annotations SET qa=?, updated_at=? WHERE alert_id=?", (json.dumps(qa), now_iso(), alert_id))
        self._push(self.story_row(alert_id))

    def record_urgent(self, handle: str, msg_id: str, text: str, alert: dict) -> None:
        """The person reported something urgent. The fixed 911 text has already gone out; the story just keeps a record."""
        self.touch(handle, msg_id, alert)
        self.add_note(alert["id"], msg_id, text, {"factors": [], "symptoms": ["reported an urgent symptom"], "feeling": "worse", "notes": ["reported an urgent symptom"]})

    # ---------------------------------------------------------- reading a turn
    def read_option(self, text: str) -> dict | None:
        return OPTION_MAP.get(re.sub(r"\s+", " ", text.strip().lower()))

    async def extract(self, text: str, alert: dict) -> dict:
        """What the person said, as structured claims. Taps are read by table; free text is read by the model and validated."""
        opt = self.read_option(text)
        if opt is not None:
            return {"factors": [{"name": f, "when": None, "certainty": "stated"} for f in opt.get("factors", [])],
                    "symptoms": opt.get("symptoms", []), "feeling": opt.get("feeling", "unknown"), "asks_question": False,
                    "unrelated": False, "notes": [opt["note"]] if opt.get("note") else [], "via": "tap"}
        prompt = [
            {"role": "system", "content": "Read ONE message from a person replying to a wearable alert. Return JSON only: "
             "{\"factors\":[{\"name\": one of " + "|".join(FACTORS) + ", \"when\": string or null, \"certainty\": \"stated\" or \"unsure\"}], "
             "\"symptoms\":[short phrases the person says they feel], \"feeling\": \"better\"|\"same\"|\"worse\"|\"unknown\", "
             "\"asks_question\": bool, \"unrelated\": bool (true if it has nothing to do with how they feel or what they did)}. "
             "Include a factor only if the person actually said it. Do not guess. Do not add causes."},
            {"role": "user", "content": f"Alert was about: {LABEL.get(alert['metric'], alert['metric'])}\nMessage: {text[:800]}"},
        ]
        try:
            raw, _ = await self.llm.chat(prompt, self.s.memory_models, max_tokens=250, temperature=0.0, json_mode=True)
            d = extract_json(raw)
        except (LLMError, ValueError):
            return {"factors": [], "symptoms": [], "feeling": "unknown", "asks_question": "?" in text, "unrelated": False, "notes": [], "via": "none"}
        low = text.lower()
        factors = []
        for f in d.get("factors") or []:
            if isinstance(f, dict) and f.get("name") in FACTORS:
                factors.append({"name": f["name"], "when": (str(f.get("when"))[:40] if f.get("when") else None),
                                "certainty": "unsure" if f.get("certainty") == "unsure" else "stated"})
        return {"factors": factors[:4], "symptoms": [str(x)[:60] for x in (d.get("symptoms") or []) if isinstance(x, str)][:4],
                "feeling": d.get("feeling") if d.get("feeling") in ("better", "same", "worse") else "unknown",
                "asks_question": bool(d.get("asks_question")) or "?" in text, "unrelated": bool(d.get("unrelated")), "notes": [], "via": "model"}

    # --------------------------------------------------------- interpretation
    def _gather(self, story: dict) -> dict:
        """All claims across the story so far, merged."""
        merged = {"factors": {}, "symptoms": [], "feeling": "unknown", "notes": []}
        for q in _loads(story.get("qa"), []):
            cl = q.get("claims") or {}
            for f in cl.get("factors", []):
                merged["factors"][f["name"]] = f
            merged["symptoms"] += [s for s in cl.get("symptoms", []) if s not in merged["symptoms"]]
            if cl.get("feeling", "unknown") != "unknown":
                merged["feeling"] = cl["feeling"]
            merged["notes"] += [n for n in cl.get("notes", []) if n not in merged["notes"]]
        return merged

    def interpret(self, alert: dict, merged: dict, checks: list) -> tuple[str, list]:
        """The current reading of the situation, built only from check results. Returns (text, signature)."""
        label = voice.plain(alert["metric"])[0]
        ev = _loads(alert["evidence"], {})
        higher = ev.get("value", 0) > ev.get("baseline", 0)
        parts = [f"Your {label} was {'higher' if higher else 'lower'} than usual."]
        sig: list = []

        def signed(x):
            return f"{x:+.1f}".replace("+", "up ").replace("-", "down ") if x is not None else "?"

        for c in checks:
            f, v = c.get("factor"), c.get("verdict")
            if f == "pattern":
                if c.get("metrics_in_direction", 0) >= 2:
                    parts.append(f"{c['metrics_in_direction']} of {c['metrics_checked']} related signals moved the same way.")
                    sig.append(("pattern", c["metrics_in_direction"]))
                continue
            sig.append((f, v))
            if f == "workout":
                if v == "within_typical_range":
                    parts.append(f"A workout fits the size of this change: your workouts usually move it {signed(c.get('typical_shift'))}, and this time it moved {signed(c.get('observed_shift'))}.")
                elif v == "larger_than_typical":
                    parts.append(f"The change is bigger than your workouts usually cause (usual {signed(c.get('typical_shift'))}, this time {signed(c.get('observed_shift'))}), so exercise alone does not account for all of it.")
                elif v == "smaller_than_typical":
                    parts.append("The change is smaller than your workouts usually cause.")
                elif v == "insufficient_history":
                    parts.append("I do not have enough past workouts to compare.")
                else:
                    parts.append(f"I cannot check a workout here: {c.get('note', 'not possible for this signal')}")
            elif f == "poor_sleep":
                if v == "weak_relationship":
                    parts.append("In your own history, how long you sleep has not predicted this reading, so I cannot tie it to your sleep.")
                elif v == "within_predicted_range":
                    parts.append("The change is close to what your sleep length would predict.")
                elif v == "larger_than_predicted":
                    parts.append("The change is bigger than your sleep length would predict.")
                elif v == "insufficient_history":
                    parts.append("I do not have enough nights to compare your sleep.")
                else:
                    parts.append(f"I cannot check sleep here: {c.get('note', 'not possible for this signal')}")
            elif v == "not_measurable":
                parts.append(f"I cannot check {f.replace('_', ' ')} with your band. I am only noting that you mentioned it.")
        if merged["symptoms"]:
            sig.append(("symptoms", True))
            parts.append(f"You also said you {merged['symptoms'][0]}. I cannot tell what that means. If it continues or gets worse, mention it to your care team.")
        elif merged["feeling"] in ("same",) and not merged["factors"]:
            parts.append("You said you feel fine.")
        if len(parts) == 1:
            parts.append("I do not know why yet.")
        else:
            parts.append("This describes your numbers. It does not prove a cause.")
        return " ".join(parts), sorted(map(str, sig))

    def _next_version(self, story: dict, text: str, sig: list, basis_note: str) -> tuple[list, bool, str | None]:
        versions = _loads(story.get("versions"), [])
        last = versions[-1] if versions else None
        changed = bool(last) and (last["text"] != text) and (last.get("basis", {}).get("signature") != sig)
        if changed:
            versions.append({"v": len(versions) + 1, "kind": "recheck", "text": text, "at": now_iso(), "changed": True,
                             "basis": {"signature": sig, "note": basis_note}, "before": last["text"]})
        return versions, changed, (last["text"] if last else None)

    # --------------------------------------------------------------- one turn
    async def process_turn(self, handle: str, text: str, msg_id: str, alert: dict) -> TurnPlan:
        story = self.touch(handle, msg_id, alert)
        claims_now = await self.extract(text, alert)
        qa = _loads(story.get("qa"), [])
        qa.append({"q": self._last_question(handle, msg_id), "a": text[:500], "at": now_iso(), "claims": claims_now, "msg_id": msg_id})
        merged_story = {**story, "qa": json.dumps(qa)}
        merged = self._gather(merged_story)
        names = list(merged["factors"].keys())
        ev = _loads(alert["evidence"], {})
        checks = self.alerts.check_factors(alert["persona"], ev, names) if (names and alert["kind"] == "hourly") else []
        if alert["kind"] == "hourly" and not names:
            checks = []
        interp, sig = self.interpret(alert, merged, checks)
        note = ", ".join(names) if names else "what you shared"
        versions, changed, before = self._next_version(merged_story, interp, sig, note)
        update_text = None
        if changed:
            why = f"You told me about {note.replace('_', ' ')}, and I compared it with your own history."
            nm = self.memory.fact_value(alert["handle"], "preferred_name") if alert.get("handle") else None
            nm = None if (nm and nm.lower().startswith("none")) else nm
            update_text, _ = voice.update_text(alert["metric"], before or "No explanation yet.", interp, why,
                                               lead=f"Thanks{', ' + nm if nm else ''}. Here is what changed.")
        action = await self._next_action(alert, merged, checks, qa, claims_now)
        followup = None
        if action == "ask_followup":
            followup = await self._pick_followup(alert, qa)
            if not followup:
                action = "keep_listening"
        with self.db.conn() as c:
            c.execute("UPDATE annotations SET qa=?, versions=?, factors=?, computed=?, updated_at=? WHERE alert_id=?",
                      (json.dumps(qa), json.dumps(versions), json.dumps(names), json.dumps(checks), now_iso(), alert["id"]))
        story = self.story_row(alert["id"])
        self._push(story)
        self.alerts.feed(alert["persona"], alert["ts"], "llm", "story",
                         f"Read the reply for {LABEL.get(alert['metric'], alert['metric'])}: " +
                         (", ".join(names) if names else "no cause mentioned") + (" \u2192 interpretation updated" if changed else ""),
                         {"alert_id": alert["id"], "changed": changed, "next": action})
        return TurnPlan(alert, story, claims_now, checks, interp, changed, update_text, action, followup)

    def _last_question(self, handle: str, before_msg_id: str) -> str:
        with self.db.conn() as c:
            r = c.execute("SELECT text, kind FROM messages WHERE handle=? AND direction='out' AND kind IN ('question','alert','update') "
                          "AND rowid < (SELECT rowid FROM messages WHERE id=?) ORDER BY rowid DESC LIMIT 1", (handle, before_msg_id)).fetchone()
        if not r:
            return "(you wrote)"
        lines = [ln for ln in r["text"].split("\n") if "?" in ln]
        return (lines[-1] if lines else r["text"].split("\n")[0])[:160]

    async def _next_action(self, alert: dict, merged: dict, checks: list, qa: list, claims_now: dict) -> str:
        asked = sum(1 for q in qa if q.get("q") and q["q"] != "(you wrote)") - 1
        # Deterministic first: how the person feels outranks everything.
        if merged["feeling"] == "worse" or merged["symptoms"]:
            return "suggest_care_team"
        if claims_now.get("unrelated"):
            return "keep_listening"
        ev = _loads(alert["evidence"], {})
        flags = self.memory.risk_flags(alert["handle"]) if alert.get("handle") else []
        state = {"metric": LABEL.get(alert["metric"], alert["metric"]), "robust_z": ev.get("z"), "lasting_hours": ev.get("persistence_h"),
                 "causes_mentioned": list(merged["factors"].keys()), "checks": [f"{c.get('factor')}:{c.get('verdict')}" for c in checks],
                 "person_says_feels": merged["feeling"], "follow_ups_already_asked": max(0, asked), "turns": len(qa), "person_health_notes": flags}
        if max(0, asked) >= MAX_FOLLOWUPS:
            return "close_with_summary"
        if self.jev:
            try:
                d = await self.jev.decide(state, {"next": choice("What is the best next step in this conversation?", NEXT)})
                pick, conf, _ = d.picked("next")
                if pick in NEXT and conf >= 0.4:
                    self.alerts.feed(alert["persona"], alert["ts"], "jev", "next_step", f"Jev chose next step \u201c{pick}\u201d ({conf:.2f})", {"alert_id": alert["id"]})
                    return pick
            except JevError as exc:
                log.info("jev next step unavailable: %s", exc)
        return "ask_followup" if not merged["factors"] and max(0, asked) < 1 else "close_with_summary"

    async def _pick_followup(self, alert: dict, qa: list) -> str | None:
        family = voice.FAMILY.get(alert["metric"], "other")
        allowed = [q for q in voice.QUESTIONS_FOR.get(family, ["open"]) if q != "open"]
        with self.db.conn() as c:
            asked_kinds = {alert["question_kind"]} if alert.get("question_kind") else set()
            asked_kinds |= {r["question_kind"] for r in c.execute("SELECT question_kind FROM alerts WHERE id=?", (alert["id"],)) if r["question_kind"]}
        asked_texts = " ".join(q.get("q", "") for q in qa)
        fresh = [q for q in allowed if q not in asked_kinds and voice.QUESTIONS[q][0] not in asked_texts]
        return fresh[0] if fresh else None

    # ---------------------------------------------------------- tapbacks
    def explain_card(self, alert: dict) -> tuple[bytes, str]:
        """A plain-words 'how I decided' card from the evidence receipt."""
        r = _loads(alert.get("receipt"), {})
        ev = _loads(alert["evidence"], {})
        label = voice.plain(alert["metric"])[0]
        z = abs(ev.get("z", 0))
        unit = voice.plain(alert["metric"])[1]
        rows = [
            ("What I looked at", f"Your {label}, as an average for each hour, around {pretty(alert['ts'])} (simulated time)."),
            ("Your usual", f"{voice.num(ev.get('baseline', 0))} {unit}. That is the middle of your own earlier readings in the same situation."),
            ("What I saw", f"{voice.num(ev.get('value', 0))} {unit}, which is about {z:.0f} times your normal ups and downs."),
            ("Why I texted", r.get("rule", "A big change that lasts is worth a quick check-in.").capitalize() + "."),
        ]
        png = cards.story_card(title="How I decided", when=pretty(alert["ts"]), rows=rows, note="Demo data. This is not medical advice.",
                               event_id=alert.get("event_id") or "", tag="Evidence")
        return png, "Here is how I came to that."

    # ------------------------------------------------------------------ closing
    async def sweep(self) -> None:
        """Close conversations that have been quiet for the whole window."""
        now = datetime.now(timezone.utc)
        with self.db.conn() as c:
            due = [r["alert_id"] for r in c.execute("SELECT alert_id, closes_at FROM annotations WHERE status='open'")
                   if r["closes_at"] and datetime.fromisoformat(r["closes_at"]) <= now]
        for aid in due:
            try:
                await self.close(aid)
            except Exception:
                log.exception("closing story %s failed", aid)

    async def close(self, alert_id: int, *, send: bool = True) -> None:
        alert = self._alert(alert_id)
        story = self.story_row(alert_id)
        if not alert or not story or story["status"] != "open":
            return
        qa = _loads(story["qa"], [])
        with self.db.conn() as c:
            c.execute("UPDATE annotations SET status='closed' WHERE alert_id=?", (alert_id,))     # claim it first so a second sweep skips it
        merged = self._gather(story)
        user_words = " ".join(q["a"] for q in qa)
        summary, relevant = await self._summarize(alert, qa, merged)
        versions = _loads(story["versions"], [])
        names = list(merged["factors"].keys())
        with self.db.conn() as c:
            c.execute("UPDATE annotations SET summary=?, status='closed', closes_at=?, updated_at=? WHERE alert_id=?",
                      (summary if relevant else "", now_iso(), now_iso(), alert_id))
            c.execute("UPDATE alerts SET closed_at=?, status='resolved' WHERE id=?", (now_iso(), alert_id))
        self.sync.push("alert_status", {"id": alert_id, "status": "resolved"})
        story = self.story_row(alert_id)
        self._push(story)
        self.alerts.feed(alert["persona"], alert["ts"], "llm", "annotation", f"Linked the conversation to the chart: {summary}" if relevant else "Conversation closed with nothing to attach",
                         {"alert_id": alert_id})
        if relevant and send and alert.get("handle"):
            await self._send_closing(alert, story, summary, versions, merged)

    async def _summarize(self, alert: dict, qa: list, merged: dict) -> tuple[str, bool]:
        said = [q for q in qa if not (q.get("claims") or {}).get("unrelated")]
        if not said:
            return "", False
        transcript = "\n".join(f"Q: {q['q']}\nA: {q['a']}" for q in said)[-3500:]
        prompt = [{"role": "system", "content": "Write ONE plain sentence (max 220 characters, third person, past tense) saying what the person reported in this chat about an "
                   "alert. Use only what they said. Do not name causes, conditions or advice. Return JSON {\"summary\": str}."},
                  {"role": "user", "content": f"Alert: {LABEL.get(alert['metric'], alert['metric'])}\n{transcript}"}]
        text = ""
        try:
            raw, _ = await self.llm.chat(prompt, self.s.memory_models, max_tokens=160, temperature=0.0, json_mode=True)
            text = str(extract_json(raw).get("summary") or "").strip()
        except (LLMError, ValueError):
            pass
        user_words = " ".join(q["a"] for q in said)
        nums_ok = all(n in user_words or n in json.dumps(_loads(alert["evidence"], {})) for n in re.findall(r"\d+(?:\.\d+)?", text))
        if not text or len(text) > 260 or BANNED.search(text) or not nums_ok:
            # fall back to a sentence built from the structured claims
            bits = [n for n in merged["notes"]] + [f"mentioned {f.replace('_', ' ')}" for f in merged["factors"]] + [f"said they {s}" for s in merged["symptoms"]]
            text = "The person " + (", ".join(bits) if bits else "replied") + "."
        return text[:260], True

    async def _send_closing(self, alert: dict, story: dict, summary: str, versions: list, merged: dict) -> None:
        handle = alert["handle"]
        ev = _loads(alert["evidence"], {})
        label = voice.plain(alert["metric"])[0]
        rows = [("What I noticed", f"Your {label} was {'higher' if ev.get('value', 0) > ev.get('baseline', 0) else 'lower'} than usual (event {alert.get('event_id', '')[-24:]})."),
                ("What you told me", summary),
                ("What the numbers show", versions[-1]["text"] if versions else "I do not know why yet."),
                ("What happens next", "I will keep watching. If you feel worse or it keeps going, talk to your care team. In an emergency call 911.")]
        png = cards.story_card(title="Your check-in", when=pretty(alert["ts"]), rows=rows, note="Say \"correct that\" or \"forget that\" to change this record.",
                               event_id=alert.get("event_id") or "")
        text, bold = voice.closing_text(summary)
        mid = await self.msg.card(handle, png, text, kind="closing", alert_id=alert["id"], key=f"close:{alert['id']}:{alert['event_id']}", reply_to=alert.get("message_id"),
                                  filename="check-in.png", bold=bold)
        if mid:
            self.memory.remember_listing(handle, [f"story:{alert['id']}"])

    # ------------------------------------------------------------------ cloud
    def _push(self, row: dict | None) -> None:
        if not row:
            return
        out = {k: row.get(k) for k in ("alert_id", "persona", "metric", "start_ts", "end_ts", "summary", "factors", "computed", "created_at",
                                       "updated_at", "event_id", "status", "opened_at", "closes_at", "qa", "versions")}
        qa = _loads(out["qa"], [])
        out["qa"] = json.dumps([{k: q.get(k) for k in ("q", "a", "at")} for q in qa])        # claims stay local
        self.sync.push("annotation", out)

    def context_block(self, plan_or_alert, story: dict | None = None, plan: TurnPlan | None = None) -> str:
        """What the chat model is told about the open conversation. Numbers and verdicts are from code."""
        alert = plan_or_alert
        ev = _loads(alert["evidence"], {})
        st = story or self.story_row(alert["id"]) or {}
        versions = _loads(st.get("versions"), [])
        said = [f"- {q['a']}" for q in _loads(st.get("qa"), [])][-6:]
        lines = [f"<open_alert event_id=\"{alert.get('event_id')}\">",
                 f"You texted this person about their {voice.plain(alert['metric'])[0]}: now {voice.num(ev.get('value', 0))}, usual {voice.num(ev.get('baseline', 0))}, "
                 f"for about {ev.get('persistence_h', 1)} hours (simulated time {alert['ts']}).",
                 "What they have told you in this conversation:", *(said or ["- (nothing yet)"]),
                 ]
        if not (plan and plan.changed):
            lines.append("Current reading of the situation (computed by code, use these words and numbers, add nothing): " + (versions[-1]["text"] if versions else "I do not know why yet."))
        if plan:
            lines.append("This turn: " + NEXT.get(plan.next_action, ""))
            if plan.changed:
                lines.append("IMPORTANT: a separate message right after yours shows the person exactly what changed (before and now). So reply with ONE short, warm sentence "
                             "acknowledging what they told you, with no numbers and no explanation of the data. If they asked a question, answer only that.")
            if plan.followup:
                lines.append("A separate follow-up question will be sent. Do not ask any question yourself.")
            else:
                lines.append("Do not ask a new question unless they asked you something first.")
        lines.append("Never say what caused the change. Describe the data and the checks only.\n</open_alert>")
        return "\n".join(lines)
