"""One conversation turn: consent, safety, commands, onboarding, check-in stories, attachments, memory, model, fact-check, reply.

Order of business for a message from a person:
1. Record it. Mark the chat read. Link it to the event it is about (if any).
2. Deterministic commands (STOP, START, PAUSE, RESUME, HELP, demo ..., remember/forget/correct) and the safety gate. No model.
3. Setup (welcome, consent, health notes) while it is unfinished.
4. Otherwise the chat model answers. If the message belongs to an alert, the story engine reads it first (checks, interpretation,
   next step) and the model is told the results. Every reply is checked before it is sent: no diagnosis or medicine advice, no guessed
   causes, no numbers or studies that were not provided.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import cards, guard, voice
from .config import Settings, mask
from .linq import Inbound, LinqClient, PollVote, Reaction, Receipt
from .llm import LLMClient, LLMError, LLMUnavailable
from .media import ext_for, to_data_url
from .memory import Memory
from .messenger import Messenger
from .ocr import DocumentReader
from .prompts import (
    DESCRIBE_PROMPT, FORGET_CONFIRM_TEXT, FORGET_DONE_TEXT, SYSTEM_PROMPT, UNAVAILABLE_TEXT, WEARABLE_OFF, WEARABLE_ON,
)
from .safety import classify
from .textutil import clean_reply, fmt_ts, normalize_word, now_iso, parse_ts, split_reply

log = logging.getLogger("s2c.agent")

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop"}
HELP_WORDS = {"help", "commands"}
RECALL_CMD = re.compile(r"what (do|did) you (remember|know) about me|what do you remember|show (me )?(my )?(memory|notes)|what have you (saved|noted)", re.I)
FORGET_CMD = re.compile(r"forget (everything|all|it all)")
FORGET_CONFIRM = re.compile(r"confirm forget( everything)?")
FORGET_ITEM = re.compile(r"^(please )?forget (that|it|this|number \d+|item \d+|\d+)$")
CORRECT_ITEM = re.compile(r"^(please )?correct (that|it|this|number \d+|item \d+|\d+)\s*[:,\-]?\s*(.*)$", re.I | re.S)
DAY_CMD = re.compile(r"^(please )?(show|give|send) (me )?(my )?(day|today|daily summary)|^my day$|^daily summary$")
DOCTOR_CMD = re.compile(r"(summary|report|record|notes) (for|to) (my |the )?(doctor|clinician|care team|nurse)|doctor summary|for my doctor")
PAUSE_CMD = re.compile(r"^pause( (alerts|check ?ins))?( for)?( (\d{1,2}) ?(h|hr|hrs|hours?))?( until tomorrow)?$")
RESUME_CMD = re.compile(r"^(resume|unpause)( (alerts|check ?ins))?$")
YES_CONFIRM = re.compile(r"^(yes|yep|yeah|y|sure|ok|okay|confirm|do it|go ahead|yes remove it|yes please)$")
NO_CONFIRM = re.compile(r"^(no|nope|n|cancel|keep it|don'?t|do not|no keep it)$")
DONE_WORDS = re.compile(r"^(that'?s all|thats all|thanks?|thank you|thx|ok(ay)?|got it|done|bye|all good|nothing else|no more)\b", re.I)
# Typed controls for the simulated wearable replay and conversation settings (deterministic, no model involved).
DEMO_CMD = re.compile(r"demo (play|start|resume|restart|again|pause|stop|status|reset|day \d{1,2}|speed \d{1,2}(\.\d)?|alerts (on|off)|link|persona [a-z0-9_\-]+"
                      r"|window \d{1,2}(\.\d)?|quiet (on|off)|mode (on|off)|onboarding|persona)")
IMAGE_HINT = re.compile(r"\b(photo|picture|image|screenshot|pic|selfie|receipt|scan|that one)\b", re.I)
# When someone wants the text out of an image, OCR runs as well and the model gets both views.
TEXT_HINT = re.compile(r"\b(read|scan|extract|transcribe|ocr|text|says?|written|handwrit\w*|copy|total|invoice|receipt)\b", re.I)
MEASURE_KINDS = {"blood_pressure": "blood pressure", "glucose": "blood sugar", "weight": "weight", "temperature": "temperature",
                 "pulse": "pulse", "oxygen": "blood oxygen", "lab_report": "lab result", "medication_label": "medicine label"}

WINDOW_CHARS = 40_000
ATTACHMENT_HISTORY_CHARS = 24_000
MAX_IMAGES = 4



def guard_json(raw: str) -> dict:
    from .llm import extract_json
    return extract_json(raw)


class Agent:
    def __init__(self, settings: Settings, memory: Memory, llm: LLMClient, linq: LinqClient, docs: DocumentReader, messenger: Messenger):
        self.s = settings
        self.memory = memory
        self.llm = llm
        self.linq = linq
        self.docs = docs
        self.msg = messenger
        self._timers: dict[str, asyncio.Task] = {}
        self._active: set[str] = set()
        self._consolidating: set[str] = set()
        self._to_describe: dict[str, list] = {}
        self._notice_at: dict[str, float] = {}
        self._sweep_tries: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        # Wired by the app after construction (they need the agent to exist first).
        self.alerts = None          # AlertEngine
        self.tools = None           # ToolBox
        self.replay = None          # Replay
        self.demo = None            # async (handle, word) -> reply text, or None   (play/pause/status ...)
        self.stories = None         # StoryEngine
        self.onboarding = None      # Onboarding
        self.push_profile = None    # (handle) -> None
        self.pipeline = None

    async def _say(self, handle: str, text: str, **kw) -> str | None:
        return await self.msg.say(handle, text, **kw)

    # ----------------------------------------------------------------- events
    async def on_event(self, ev) -> None:
        if isinstance(ev, Inbound):
            await self.on_inbound(ev)
        elif isinstance(ev, PollVote):
            await self.on_poll_vote(ev)
        elif isinstance(ev, Reaction):
            await self.on_reaction(ev)
        elif isinstance(ev, Receipt):
            self.msg.on_receipt(ev.message_id, ev.state, ev.reason)

    # ------------------------------------------------------------------ inbound
    async def on_inbound(self, ev: Inbound) -> None:
        h = ev.handle
        if h not in self.s.allowed_numbers:
            log.warning("ignored inbound from non-allowlisted handle %s", mask(h))
            return
        is_new = self.memory.ensure_user(h)

        stored_text = ev.text
        for m in ev.media:
            stored_text += f"\n[attachment: {m.get('filename') or 'file'} ({m.get('mime_type') or 'unknown type'})]"
        stored = self.memory.record_message(
            msg_id=ev.message_id, event_id=ev.event_id, chat_id=ev.chat_id, handle=h, direction="in",
            text=stored_text.strip() or "[empty message]", created_at=ev.sent_at or now_iso(),
        )
        if not stored:
            log.info("duplicate delivery ignored")
            return
        if ev.reply_to:
            with self.memory.db.conn() as c:
                c.execute("UPDATE messages SET reply_to=? WHERE id=?", (ev.reply_to, ev.message_id))
        for m in ev.media:
            self.memory.add_attachment(
                att_id=str(m.get("id") or f"{ev.message_id}-{m.get('filename')}"), message_id=ev.message_id,
                handle=h, filename=m.get("filename"), mime=m.get("mime_type"),
                size_bytes=m.get("size_bytes"), url=m.get("url"),
            )
        self._spawn(self.linq.mark_read(ev.chat_id))                 # the person sees "Read"

        word = normalize_word(ev.text)
        if self.memory.is_opted_out(h):
            if word in START_WORDS:
                self.memory.set_opt_out(h, False)
                self.memory.set_consent(h, True)
                await self._say(h, "Welcome back. Check-ins are on again. Say PAUSE or STOP any time.", kind="system")
            return
        if word in STOP_WORDS:
            self.memory.set_opt_out(h, True)             # Linq also blocks further sends to this person
            self.memory.set_consent(h, False)
            if self.alerts:
                n = self.alerts.cancel_held(h)           # nothing that was waiting to be sent will go out
                log.info("opt-out: cancelled %d queued alerts", n)
            return
        state, _ = self.memory.onboarding(h)
        if word in START_WORDS or word == "resume" and state in ("new", "consent"):
            if state == "new":
                await self.onboarding.begin(h)
                return
            if state == "consent" and await self.onboarding.on_text(h, "start", ev.message_id):
                return
            self.memory.set_consent(h, True)
            self.memory.resume(h)
            await self._say(h, voice.RESUMED_TEXT, kind="system")
            return
        if word in HELP_WORDS:
            await self._say(h, voice.HELP_TEXT, kind="system", bold=["Here is what I can do:"])
            return
        m = PAUSE_CMD.match(word)
        if m:
            hours = float(m.group(5)) if m.group(5) else (16 if "until tomorrow" in word else 8)
            self.memory.pause(h, hours)
            await self._say(h, voice.PAUSED_TEXT.format(hours=int(hours)), kind="system")
            return
        if RESUME_CMD.match(word):
            self.memory.resume(h)
            await self._say(h, voice.RESUMED_TEXT, kind="system")
            return

        guidance = classify(ev.text)
        if guidance:
            await self._say(h, guidance, kind="safety")  # fixed text, the model is bypassed
            if self.stories:
                alert = self.stories.target_alert(h, ev.reply_to)
                if alert:
                    self.stories.record_urgent(h, ev.message_id, ev.text, alert)
            return

        if await self._memory_commands(h, ev, word):
            return
        if DAY_CMD.match(word) or DOCTOR_CMD.search(word):
            built = self.pipeline_card(h, doctor=bool(DOCTOR_CMD.search(word)))
            if built:
                png, caption = built
                await self.msg.card(h, png, caption, kind="card", key=f"card:{ev.message_id}", filename="summary.png")
            else:
                await self._say(h, "I do not have enough yet to make that. Start the demo data first, or ask me again after a check-in.", kind="system")
            return
        if self.demo and DEMO_CMD.fullmatch(word):
            reply = await self._demo(h, word)
            if reply is not None:                     # "" means handled with nothing more to say
                if reply:
                    await self._say(h, reply, kind="system")
                return

        if state != "new" and await self._typed_answer(h, ev):
            return
        if is_new or state == "new":
            await self.onboarding.begin(h)                # welcome, then consent; the first message is kept in the log
            return
        if state in ("consent", "profile"):
            if await self.onboarding.on_text(h, ev.text, ev.message_id):
                return
        if await self._image_confirm_reply(h, word, ev.message_id):
            return
        self.schedule(h)

    def pipeline_card(self, handle: str, *, doctor: bool):
        pl = self.pipeline
        return (pl.doctor_summary_card(handle) if doctor else pl.my_day_card(handle)) if pl else None

    # ----------------------------------------------- typed answers to options
    @staticmethod
    def _match_option(text: str, options: dict) -> str | None:
        w = normalize_word(text)
        names = {normalize_word(o): o for o in options.values()}
        if w in names:
            return names[w]
        first = w.split(" ")[0] if w else ""
        starts = [o for n, o in names.items() if n.split(" ")[0] == first]
        if first in ("yes", "no", "skip", "fine", "well", "calm", "unwell", "yeah", "yep", "nope") and len(starts) == 1:
            return starts[0]
        if first in ("yeah", "yep", "yup", "sure"):
            yes = [o for n, o in names.items() if n.startswith("yes")]
            return yes[0] if len(yes) == 1 else None
        if first in ("nope", "nah"):
            no = [o for n, o in names.items() if n.startswith("no")]
            return no[0] if len(no) == 1 else None
        return None

    async def _typed_answer(self, h: str, ev: Inbound) -> bool:
        """If the last question was a list of options and the person typed one of them, treat it as that answer."""
        with self.memory.db.conn() as c:
            row = c.execute("SELECT * FROM pending_polls WHERE handle=? AND message_id LIKE 'txt-%' AND answered_at IS NULL "
                            "AND created_at >= datetime('now','-45 minutes') ORDER BY created_at DESC LIMIT 1", (h,)).fetchone()
        if not row:
            return False
        option = self._match_option(ev.text, json.loads(row["options"]))
        if not option:
            return False
        with self.memory.db.conn() as c:
            c.execute("UPDATE pending_polls SET answered_at=? WHERE message_id=?", (now_iso(), row["message_id"]))
        purpose, ref = row["purpose"], row["ref"]
        if purpose in ("consent", "profile", "start_demo"):
            await self.onboarding.on_poll(h, purpose, ref, option, ev.message_id)
            return True
        if purpose == "confirm":
            with self.memory.db.conn() as c:
                pc = c.execute("SELECT * FROM pending_confirms WHERE id=? AND handle=? AND status='open'", (ref, h)).fetchone()
            if pc and option.lower().startswith("yes"):
                await (self._do_forget(h, pc) if pc["kind"] == "forget_item" else self._confirm_image(h, pc, True, ev.message_id))
            elif pc:
                self._close_confirm(pc["id"], "cancelled")
                await self._say(h, "OK. I kept it." if pc["kind"] == "forget_item" else "OK. I did not save it. Tell me the right value and I will.", kind="system")
            return True
        if purpose == "alert_q" and self.stories:
            # the typed words themselves are the reply; the option text makes the story reader's job exact
            with self.memory.db.conn() as c:
                c.execute("UPDATE messages SET text=? WHERE id=?", (option, ev.message_id))
            self.schedule(h)
            return True
        return False

    # ------------------------------------------------------- memory commands
    async def _memory_commands(self, h: str, ev: Inbound, word: str) -> bool:
        if self.memory.pending_forget(h):
            if FORGET_CONFIRM.fullmatch(word):
                self.memory.wipe(h)
                if self.alerts:
                    self.alerts.forget(h)
                await self._say(h, FORGET_DONE_TEXT, kind="system", record_text=FORGET_DONE_TEXT)
                if self.push_profile:
                    self.push_profile(h)
                return True
            self.memory.set_pending_forget(h, False)  # anything else cancels
        if FORGET_CMD.fullmatch(word):
            self.memory.set_pending_forget(h, True)
            await self._say(h, FORGET_CONFIRM_TEXT, kind="system")
            return True
        if RECALL_CMD.search(ev.text):
            items = self.memory.list_items(h)
            self.memory.remember_listing(h, [it["ref"] for it in items])
            await self._say(h, self.memory.render_items(items), kind="system")
            return True
        fm = FORGET_ITEM.match(word)
        if fm:
            await self._ask_forget(h, word)
            return True
        cm = CORRECT_ITEM.match(ev.text.strip())
        if cm:
            await self._correct_item(h, cm.group(2).lower(), cm.group(3).strip(), ev.message_id)
            return True
        pc = self._open_confirm(h, "forget_item")
        if pc and YES_CONFIRM.match(word):
            await self._do_forget(h, pc)
            return True
        if pc and NO_CONFIRM.match(word):
            self._close_confirm(pc["id"], "cancelled")
            await self._say(h, "OK. I kept it.", kind="system")
            return True
        return False

    def _resolve_ref(self, h: str, word: str) -> str | None:
        refs = self.memory.last_refs(h)
        m = re.search(r"(\d+)$", word)
        if m and word not in ("that", "it", "this"):
            n = int(m.group(1))
            items = self.memory.list_items(h)
            if 1 <= n <= len(items):
                return items[n - 1]["ref"]
            return None
        return refs[0] if len(refs) == 1 else None

    def _open_confirm(self, h: str, kind: str):
        with self.memory.db.conn() as c:
            return c.execute("SELECT * FROM pending_confirms WHERE handle=? AND kind=? AND status='open' ORDER BY id DESC LIMIT 1", (h, kind)).fetchone()

    def _close_confirm(self, cid: int, status: str) -> None:
        with self.memory.db.conn() as c:
            c.execute("UPDATE pending_confirms SET status=? WHERE id=?", (status, cid))

    async def _ask_forget(self, h: str, word: str) -> None:
        ref = self._resolve_ref(h, word)
        if not ref:
            items = self.memory.list_items(h)
            self.memory.remember_listing(h, [it["ref"] for it in items])
            await self._say(h, "Which one should I forget? Tell me the number.\n\n" + self.memory.render_items(items), kind="system")
            return
        label = next((it["text"] for it in self.memory.list_items(h) if it["ref"] == ref), ref)
        with self.memory.db.conn() as c:
            cid = c.execute("INSERT INTO pending_confirms(handle,kind,payload,status,created_at) VALUES(?,?,?,?,?)",
                            (h, "forget_item", json.dumps({"ref": ref, "label": label}), "open", now_iso())).lastrowid
        await self.msg.ask(h, f"I will remove this and everything I built from it:\n\n{label[:200]}\n\nShould I go ahead?",
                           ["Yes, remove it", "No, keep it"], purpose="confirm", ref=str(cid), key=f"forget:{cid}")

    async def _do_forget(self, h: str, pc) -> None:
        payload = json.loads(pc["payload"])
        self._close_confirm(pc["id"], "done")
        report = await self.memory.forget_ref(h, payload["ref"])
        if report["alert_ids"] and self.alerts:
            self.alerts.forget_story(report["alert_ids"])
        if self.push_profile:
            self.push_profile(h)
        if report["ok"]:
            await self._say(h, "Done. I removed it, the notes built from it, the messages it came from, and the search entries. "
                               "Your raw wearable readings are separate and are still there.", kind="system")
        else:
            await self._say(h, "I removed most of it, but could not remove: " + "; ".join(report["leftovers"][:3]) + ". "
                               "I will not say it is gone until it is.", kind="system")

    async def _correct_item(self, h: str, word: str, new_text: str, msg_id: str) -> None:
        ref = self._resolve_ref(h, word)
        if not ref:
            items = self.memory.list_items(h)
            self.memory.remember_listing(h, [it["ref"] for it in items])
            await self._say(h, "Which one should I fix? Say it like this: correct 2: the right answer\n\n" + self.memory.render_items(items), kind="system")
            return
        if len(new_text) < 2:
            await self._say(h, "What is the right answer? Say it like this: correct 2: the right answer", kind="system")
            return
        before = next((it["text"] for it in self.memory.list_items(h) if it["ref"] == ref), "")
        res = self.memory.correct_ref(h, ref, new_text, msg_id)
        if not res.get("ok"):
            await self._say(h, "I could not find that one to fix.", kind="system")
            return
        if self.push_profile:
            self.push_profile(h)
        if res["kind"] == "story" and self.stories:
            self.stories._push(self.stories.story_row(res["alert_id"]))
        await self._say(h, f"Fixed.\n\nBefore: {before[:160]}\n\nNow: {new_text[:160]}\n\nI keep the earlier version as history. Say \"forget that\" if you want it gone.", kind="update")

    # ------------------------------------------------------------- demo commands
    async def _demo(self, h: str, word: str) -> str | None:
        arg = word.removeprefix("demo ").strip()
        if arg.startswith("persona"):
            pid = arg.removeprefix("persona").strip()
            from .registry import PERSONAS
            if not pid:
                return "People you can follow: " + ", ".join(PERSONAS) + ". Say \"demo persona maya\"."
            if pid not in PERSONAS:
                return f"I do not know {pid}. Try: " + ", ".join(PERSONAS)
            self.alerts.set_setting("linked_persona", pid)
            return f"You are now linked to {PERSONAS[pid].name} ({PERSONAS[pid].tag}). Say \"demo play\" to start their replay."
        if arg.startswith("window"):
            minutes = float(arg.removeprefix("window").strip())
            self.stories.set_idle_minutes(minutes)
            return f"Check-in conversations now close after {minutes:g} quiet minutes."
        if arg.startswith("mode"):
            on = arg.endswith("on")
            self.alerts.set_setting("demo_mode", "1" if on else "0")
            return ("Demo mode ON: no quiet hours, and check-ins can come close together." if on
                    else f"Demo mode OFF: quiet hours ({self.s.quiet_start}:00 to {self.s.quiet_end}:00), and check-ins are spaced out.")
        if arg.startswith("quiet"):
            off = arg.endswith("off")
            with self.memory.db.conn() as c:
                c.execute("UPDATE users SET quiet_off=? WHERE handle=?", (int(off), h))
            return "Quiet hours are OFF for the demo." if off else f"Quiet hours are ON ({self.s.quiet_start}:00 to {self.s.quiet_end}:00 local)."
        if arg == "onboarding":
            self.memory.set_onboarding(h, "new", 0)
            await self.onboarding.begin(h)
            return ""
        if arg == "link":
            link = self.onboarding.link() if self.onboarding.link else None
            if link:
                await self.msg.link(h, link, key=f"link:{time.time_ns()}")
                return "That is your private dashboard. Keep the link to yourself."
            return "The dashboard is not configured yet."
        return await self.demo(h, word)

    # --------------------------------------------------------------- reactions
    async def on_reaction(self, ev: Reaction) -> None:
        """A tapback on one of our messages is a quick answer. A thumbs-up on an alert means 'that looks fine'."""
        h = ev.handle
        if h not in self.s.allowed_numbers or not ev.added or self.memory.is_opted_out(h):
            return
        with self.memory.db.conn() as c:
            row = c.execute("SELECT * FROM alerts WHERE handle=? AND (message_id=? OR poll_message_id=?) ORDER BY id DESC LIMIT 1", (h, ev.message_id, ev.message_id)).fetchone()
        if not row or not self.stories:
            return
        alert = dict(row)
        label = {"like": "gave a thumbs-up", "love": "gave a heart", "dislike": "gave a thumbs-down", "question": "asked for more explanation",
                 "emphasize": "marked it as important", "laugh": "laughed"}.get(ev.kind)
        if not label:
            return
        mid = f"tap-{ev.event_id or time.time_ns()}"
        self.memory.record_message(msg_id=mid, event_id=None, chat_id=ev.chat_id, handle=h, direction="in",
                                   text=f"[tapback] {label} on the {voice.plain(alert['metric'])[0]} alert", created_at=now_iso())
        with self.memory.db.conn() as c:
            c.execute("UPDATE messages SET kind='tapback' WHERE id=?", (mid,))        # a tapback is data for the story, not a message to answer
        self.stories.touch(h, mid, alert)
        self.stories.add_note(alert["id"], mid, f"[tapback] {label}", {"notes": [f"{label} on the alert"], "factors": [], "symptoms": [], "feeling": "unknown"})
        if ev.kind == "question":
            png, caption = self.stories.explain_card(alert)
            await self.msg.card(h, png, caption, kind="explain", alert_id=alert["id"], reply_to=alert.get("message_id"), key=f"explain:{alert['id']}:{alert['event_id']}:{ev.event_id}")
        elif ev.kind == "dislike":
            await self.msg.ask(h, "Thanks for telling me. What looks wrong?", ["The numbers look wrong", "I'm fine, no need to text", "Too many texts"],
                               purpose="feedback", ref=str(alert["id"]), key=f"fb:{alert['id']}:{alert['event_id']}:{ev.event_id}", alert_id=alert["id"], reply_to=alert.get("message_id"))

    # -------------------------------------------------------------- poll votes
    async def on_poll_vote(self, ev: PollVote) -> None:
        h = ev.handle
        if h not in self.s.allowed_numbers or not ev.added or self.memory.is_opted_out(h):
            return
        with self.memory.db.conn() as c:
            row = c.execute("SELECT * FROM pending_polls WHERE message_id=? AND handle=?", (ev.message_id, h)).fetchone()
        if not row:
            return
        options = json.loads(row["options"])
        option = options.get(ev.option_id)
        if not option:
            return
        with self.memory.db.conn() as c:
            c.execute("UPDATE pending_polls SET answered_at=? WHERE message_id=?", (now_iso(), ev.message_id))
        purpose, ref = row["purpose"], row["ref"]
        mid = f"vote-{ev.event_id or time.time_ns()}"
        self.memory.record_message(msg_id=mid, event_id=None, chat_id=ev.chat_id, handle=h, direction="in", text=option, created_at=now_iso())
        self._spawn(self.linq.mark_read(ev.chat_id))
        if purpose in ("consent", "profile", "start_demo"):
            await self.onboarding.on_poll(h, purpose, ref, option, mid)
        elif purpose == "confirm":
            pc = None
            with self.memory.db.conn() as c:
                pc = c.execute("SELECT * FROM pending_confirms WHERE id=? AND handle=? AND status='open'", (ref, h)).fetchone()
            if pc and option.lower().startswith("yes"):
                if pc["kind"] == "forget_item":
                    await self._do_forget(h, pc)
                elif pc["kind"] == "image_measure":
                    await self._confirm_image(h, pc, True, mid)
            elif pc:
                self._close_confirm(pc["id"], "cancelled")
                await self._say(h, "OK. I did not save it. Tell me the right value and I will.", kind="system") if pc["kind"] == "image_measure" else await self._say(h, "OK. I kept it.", kind="system")
        elif purpose == "feedback" and self.stories:
            alert = self.stories._alert(int(ref)) if ref else None
            if alert:
                self.stories.touch(h, mid, alert)
                self.stories.add_note(alert["id"], mid, option, {"notes": [f"feedback: {option}"], "factors": [], "symptoms": [], "feeling": "unknown"})
                if option.startswith("Too many"):
                    self.memory.pause(h, 8)
                    await self._say(h, "Sorry about that. I paused check-ins for 8 hours. Say RESUME any time.", kind="system")
                else:
                    await self._say(h, "Thank you. I noted it.", kind="system")
        elif purpose == "alert_q":
            self.schedule(h, delay=0.5)

    # --------------------------------------------------------------- scheduling
    def schedule(self, handle: str, delay: float | None = None) -> None:
        if handle in self._active:
            return  # the running loop will pick up anything new
        old = self._timers.get(handle)
        if old and not old.done():
            old.cancel()
        wait = self.s.debounce_seconds if delay is None else delay
        self._timers[handle] = self._spawn(self._delayed(handle, wait))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _delayed(self, handle: str, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        await self._respond(handle)

    async def _respond(self, handle: str) -> None:
        self._active.add(handle)
        answered = False
        try:
            for _ in range(3):
                pending = self.memory.pending_user_messages(handle)
                if not pending:
                    self._sweep_tries.pop(handle, None)
                    break
                try:
                    answered = await self._answer(handle, pending)
                except LLMUnavailable as exc:
                    log.warning("language model unavailable: %s", exc)
                    await self._notice(handle)
                    answered = False
                except Exception:
                    log.exception("turn failed")
                    answered = False
                if not answered:
                    break
        finally:
            self._active.discard(handle)
        if answered and self.memory.pending_user_messages(handle):
            self.schedule(handle, delay=0)

    async def _notice(self, handle: str) -> None:
        if time.monotonic() - self._notice_at.get(handle, -1e9) < 900:
            return
        self._notice_at[handle] = time.monotonic()
        await self._say(handle, UNAVAILABLE_TEXT, kind="system", record=False, key=f"unavail:{int(time.time() // 900)}")

    async def sweeper(self) -> None:
        """Retry recent unanswered messages (for example after the model comes back), close quiet check-ins, release held alerts."""
        tick = 0
        while True:
            await asyncio.sleep(15)
            tick += 1
            try:
                if self.stories:
                    await self.stories.sweep()
                for handle in self.s.allowed_numbers:
                    if self.alerts:
                        await self.alerts.release_held(handle)
                    if tick % 3 or handle in self._active or self.memory.is_opted_out(handle):
                        continue
                    pending = self.memory.pending_user_messages(handle)
                    if not pending:
                        self._sweep_tries.pop(handle, None)
                        continue
                    age = (parse_ts(now_iso()) - parse_ts(pending[-1]["created_at"])).total_seconds()
                    if 8 < age < 1800 and self._sweep_tries.get(handle, 0) < 5:
                        self._sweep_tries[handle] = self._sweep_tries.get(handle, 0) + 1
                        self.schedule(handle, delay=0)
            except Exception:
                log.exception("sweeper")

    # --------------------------------------------------------------------- turn
    async def _answer(self, handle: str, pending: list) -> bool:
        self._spawn(self.linq.typing(pending[-1]["chat_id"]))
        images, doc_blocks, notes, to_describe = await self._prepare_attachments(handle, pending)
        user_text = " ".join(r["text"] for r in pending)
        last = pending[-1]

        if images and await self._maybe_confirm_measurement(handle, images, user_text, last["id"]):
            self._kick_consolidation(handle, to_describe)
            return True

        # Does this message belong to an alert? Then the story engine reads it first.
        plan = None
        alert = None
        if self.stories and not images:
            alert = self.stories.target_alert(handle, self._reply_to(last["id"]))
            if alert:
                first_turn = not self.stories.open_story(handle)
                plans = []
                for r in pending:                                   # each message is read on its own, in order
                    plans.append(await self.stories.process_turn(handle, r["text"], r["id"], alert))
                plan = plans[-1]
                changed = [p for p in plans if p.changed]
                if changed:                                          # a change in any message of the burst must still be shown
                    plan.changed, plan.update_text = True, changed[-1].update_text
                if first_turn:
                    await self.msg.react(handle, last["id"], "like")      # a tapback shows the message was seen

        query = user_text + " " + " ".join(doc_blocks)
        history = self.memory.recent_transcript(handle, WINDOW_CHARS)
        window_min = history[0]["rowid"] if history else pending[0]["rowid"]
        recalled = await self.memory.retrieve(handle, query, window_min_rowid=window_min)

        wearable = bool(self.tools and self.tools.has_wearable(handle))
        tool_specs = self.tools.specs_for(handle) if self.tools else None
        blocks = [self.stories.context_block(alert, plan=plan)] if plan else []
        messages = self._build_messages(handle, history, pending, recalled, images, doc_blocks, notes, blocks=blocks, wearable=wearable)
        if self.tools:
            self.tools.begin_turn(handle)
        models = self.s.vision_models if images else self.s.text_models
        text, usage = await self._complete(handle, messages, models, tool_specs)
        log.info("reply generated model=%s tokens=%s/%s images=%d tools=%s story=%s", usage.get("model"), usage.get("prompt_tokens"),
                 usage.get("completion_tokens"), len(images), [t["name"] for t in usage.get("tools", [])] or "-", bool(plan))
        reply = clean_reply(text)
        if not reply:
            return False
        reply = await self._checked(handle, reply, messages, models, user_text, plan, alert)

        parts = split_reply(reply)
        sent = False
        for i, part in enumerate(parts):
            if i:
                await asyncio.sleep(0.8)
            sent = bool(await self._say(handle, part, kind="reply", alert_id=alert["id"] if alert else None, key=f"reply:{last['id']}:{i}")) or sent
        if plan and sent:
            if plan.changed and plan.update_text:
                text_u, bold = plan.update_text, [plan.update_text.split("\n")[0]]
                await self._say(handle, text_u, kind="update", alert_id=alert["id"], reply_to=alert.get("message_id"), bold=bold,
                                key=f"update:{alert['id']}:{alert['event_id']}:{len(json.loads(plan.story['versions']))}")
            elif plan.followup:
                q, opts = voice.QUESTIONS[plan.followup]
                with self.memory.db.conn() as c:
                    c.execute("UPDATE alerts SET question_kind=COALESCE(question_kind, ?) WHERE id=?", (plan.followup, alert["id"]))
                await self.msg.ask(handle, q, opts, purpose="alert_q", ref=str(alert["id"]), key=f"fu:{alert['id']}:{alert['event_id']}:{last['id']}", alert_id=alert["id"])
            if DONE_WORDS.match(user_text.strip()) and plan.next_action in ("close_with_summary", "keep_listening"):
                await self.stories.close(alert["id"])                         # they said they are done: save the story now
        if sent:
            self._kick_consolidation(handle, to_describe)
            if state_not_done := (self.memory.onboarding(handle)[0] in ("consent", "profile")):
                await self.onboarding.nudge(handle)
        return sent

    def _reply_to(self, msg_id: str) -> str | None:
        with self.memory.db.conn() as c:
            r = c.execute("SELECT reply_to FROM messages WHERE id=?", (msg_id,)).fetchone()
        return r["reply_to"] if r else None

    async def _checked(self, handle: str, reply: str, messages: list, models, user_text: str, plan, alert) -> str:
        """The last gate before sending. Two checks, one rewrite each, then a fixed answer: nothing unchecked is sent."""
        found = guard.violations(reply)
        if found:
            log.warning("reply held by guard: %s", found)
            retry = messages + [{"role": "assistant", "content": reply}, {"role": "user", "content": "[system] " + guard.REWRITE_NOTE}]
            try:
                text2, _ = await self.llm.chat(retry, models, max_tokens=500, temperature=0.3)
                reply = clean_reply(text2)
            except LLMError:
                reply = ""
            if not reply or guard.violations(reply):
                return guard.fallback_for(found)
        trace = (self.tools.results_text(handle) if self.tools else "") + (self.stories.context_block(alert, plan=plan) if plan else "")
        used_evidence = bool(self.tools and self.tools.used(handle, "find_evidence"))
        data_ctx = bool(plan) or bool(self.tools and self.tools.trace.get(handle))
        problems = guard.grounding_problems(reply, trace_text=trace, used_evidence=used_evidence, data_context=data_ctx, user_text=user_text)
        if problems:
            log.warning("reply held by fact-check: %s", problems)
            note = " ".join(guard.GROUNDING_NOTE[p] for p in problems)
            retry = messages + [{"role": "assistant", "content": reply}, {"role": "user", "content": "[system] " + note + " Rewrite the reply."}]
            try:
                text2, _ = await self.llm.chat(retry, models, max_tokens=500, temperature=0.2)
                reply2 = clean_reply(text2)
            except LLMError:
                reply2 = ""
            again = guard.grounding_problems(reply2, trace_text=trace, used_evidence=used_evidence, data_context=data_ctx, user_text=user_text) if reply2 else ["empty"]
            if again or guard.violations(reply2):
                summary = self.tools.evidence_summary(handle) if (self.tools and used_evidence) else None
                return summary or (plan.interpretation if plan else guard.GROUNDING_FALLBACK)
            return reply2
        return reply

    async def _complete(self, handle: str, messages: list[dict], models: tuple[str, ...], tool_specs):
        if tool_specs:
            try:
                return await self.llm.chat_with_tools(messages, models, tool_specs, functools.partial(self.tools.run, handle),
                                                      max_tokens=700, temperature=0.4)
            except LLMError as exc:
                log.warning("tool turn failed (%s), answering without tools", exc)
        return await self.llm.chat(messages, models, max_tokens=700, temperature=0.4)

    def _build_messages(self, handle: str, history: list, pending: list, recalled: dict,
                        images: list[str], doc_blocks: list[str], notes: list[str], *,
                        blocks: list[str] | None = None, wearable: bool = False) -> list[dict]:
        now = datetime.now().astimezone().strftime("%A, %B %d, %Y, %I:%M %p %Z")
        persona = self.alerts.persona_for(handle) if self.alerts else None
        sim = (self.replay.state(persona)["ts"] if (wearable and persona and self.replay) else None) or "not started"
        style = (self.memory.fact_value(handle, "message_style") or "").lower()
        name = self.memory.fact_value(handle, "preferred_name")
        extra = []
        if name and not name.lower().startswith("none"):
            extra.append(f"The person's name is {name}. Use it sometimes, not in every message.")
        extra.append("The person asked for short, simple messages: 1 to 3 short sentences." if "short" in style
                     else "The person likes a bit more detail, but keep sentences short." if "detail" in style
                     else "Write in short, plain sentences an older adult would find easy.")
        sys_blocks = [SYSTEM_PROMPT, f"Current time: {now}.", " ".join(extra),
                      WEARABLE_ON.format(sim_time=sim) if wearable else WEARABLE_OFF, *(blocks or []), self.memory.render(handle, recalled)]
        system = "\n\n".join(sys_blocks)
        msgs: list[dict] = [{"role": "system", "content": system}]
        pending_ids = {r["rowid"] for r in pending}
        # Documents and images the person sent earlier stay readable: their extracted text rides along
        # with the message that carried them, newest first, within a budget.
        attached = self.memory.attachment_texts([r["id"] for r in history if r["rowid"] not in pending_ids])
        budget = ATTACHMENT_HISTORY_CHARS
        keep: dict[str, str] = {}
        for r in reversed(history):
            for fname, text in attached.get(r["id"], []):
                piece = text[:4000]
                if budget - len(piece) < 0:
                    break
                budget -= len(piece)
                keep[r["id"]] = keep.get(r["id"], "") + f'\n<attachment_text file="{fname}">\n{piece}\n</attachment_text>'
        for r in history:
            if r["rowid"] in pending_ids:
                continue
            role = "user" if r["direction"] == "in" else "assistant"
            if role == "assistant" and len(msgs) == 1:
                continue  # the first turn after the system prompt must be the user's
            content = f"[{fmt_ts(r['created_at'])}] {r['text']}" if role == "user" else r["text"]
            content += keep.get(r["id"], "")
            if msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content
            else:
                msgs.append({"role": role, "content": content})

        body = "\n".join(f"[{fmt_ts(r['created_at'])}] {r['text']}" for r in pending)
        if doc_blocks:
            body += "\n\n" + "\n\n".join(doc_blocks)
        if notes:
            body += "\n\n" + "\n".join(notes)
        content: str | list = body
        if images:
            content = [{"type": "text", "text": body}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in images[:MAX_IMAGES]
            ]
        if msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], str) and isinstance(content, str):
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": "user", "content": content})
        return msgs

    # ------------------------------------------------- measurements from photos
    async def _maybe_confirm_measurement(self, handle: str, images: list[str], user_text: str, msg_id: str) -> bool:
        """If a photo shows a medical reading, read it, show what was read, and ask before it is saved. Returns True if it asked."""
        prompt = [{"role": "system", "content": "Look at the image. If it shows a medical reading or label (a blood pressure monitor, glucose meter, scale, thermometer, "
                   "pulse oximeter, lab report, or medicine label), return JSON only: {\"kind\": one of blood_pressure|glucose|weight|temperature|pulse|oxygen|lab_report|medication_label|other, "
                   "\"fields\": [{\"name\": str, \"value\": str, \"unit\": str, \"confidence\": number 0-1}], \"legible\": bool}. "
                   "Copy digits exactly as shown. If a digit is blurry, give a low confidence for that field. If it is not a medical reading, kind is other."},
                  {"role": "user", "content": [{"type": "text", "text": f"Person's note: {user_text[:200] or '(none)'}"}] +
                   [{"type": "image_url", "image_url": {"url": images[0]}}]}]
        try:
            raw, _ = await self.llm.chat(prompt, self.s.vision_models, max_tokens=400, temperature=0.0, json_mode=True)
            d = guard_json(raw)
        except Exception:
            return False
        kind = d.get("kind")
        fields = [f for f in (d.get("fields") or []) if isinstance(f, dict) and f.get("value")]
        if kind not in MEASURE_KINDS or not fields:
            return False
        unsure = [f for f in fields if float(f.get("confidence") or 0) < 0.85]
        lines = [f"{f.get('name', 'value')}: {f['value']} {f.get('unit', '')}".strip() for f in fields[:5]]
        with self.memory.db.conn() as c:
            cid = c.execute("INSERT INTO pending_confirms(handle,kind,payload,source_message_id,status,created_at) VALUES(?,?,?,?,?,?)",
                            (handle, "image_measure", json.dumps({"kind": kind, "fields": fields[:5]}), msg_id, "open", now_iso())).lastrowid
        warn = ("\n\nI am not sure about: " + ", ".join(str(f.get("name")) for f in unsure) + ". Please check those carefully.") if unsure or not d.get("legible", True) else ""
        question = f"I read your {MEASURE_KINDS[kind]} from the photo:\n\n" + "\n".join(lines) + warn + "\n\nIs that right? I will only save it after you say yes."
        await self.msg.ask(handle, question, ["Yes, that is right", "No, it is wrong"], purpose="confirm", ref=str(cid), key=f"imgconf:{cid}")
        return True

    async def _image_confirm_reply(self, h: str, word: str, msg_id: str) -> bool:
        pc = self._open_confirm(h, "image_measure")
        if not pc:
            return False
        if YES_CONFIRM.match(word) or word.startswith("yes") or word in ("right", "correct", "that is right"):
            await self._confirm_image(h, pc, True, msg_id)
            return True
        if NO_CONFIRM.match(word) or word.startswith("no"):
            self._close_confirm(pc["id"], "cancelled")
            await self._say(h, "OK. I did not save it. Tell me the right numbers and I will.", kind="system")
            return True
        return False

    async def _confirm_image(self, h: str, pc, yes: bool, msg_id: str) -> None:
        payload = json.loads(pc["payload"])
        self._close_confirm(pc["id"], "done")
        fields = payload["fields"]
        value = "; ".join(f"{f.get('name', 'value')} {f['value']} {f.get('unit', '')}".strip() for f in fields)
        key = {"blood_pressure": "reported_bp"}.get(payload["kind"], f"reported_{payload['kind']}")
        self.memory.set_fact(h, key, f"{value} (from a photo you confirmed, {datetime.now().strftime('%b %d')})", [pc["source_message_id"] or msg_id], "photo")
        if self.push_profile:
            self.push_profile(h)
        await self._say(h, "Saved. I noted it as something you reported. I do not interpret it as medical advice.", kind="system")

    # -------------------------------------------------------------- attachments
    async def _prepare_attachments(self, handle: str, pending: list):
        """Route each attachment: images go to the vision model as they are, documents become text.

        Returns (image data URLs, document text blocks, notes, images that still need a stored description).
        """
        images: list[str] = []
        doc_blocks: list[str] = []
        notes: list[str] = []
        to_describe: list[tuple] = []
        user_text = " ".join(r["text"] for r in pending)
        atts = [a for r in pending for a in self.memory.attachments_for(r["id"])]
        if not atts and IMAGE_HINT.search(user_text):
            earlier = self.memory.recent_image_attachment(handle)
            if earlier:
                atts = [earlier]
                notes.append("(The person may be referring to an earlier image, attached again.)")
        for att in atts[:6]:
            att = await self._ensure_downloaded(att)
            name = att["filename"] if att else "file"
            if not att or att["status"] != "ready":
                notes.append(f"[attachment {name} could not be downloaded]")
                continue
            mime = att["mime"] or ""
            path = Path(att["path"])
            if mime.startswith("image/"):
                data_url = await asyncio.to_thread(to_data_url, path, mime)
                images.append(data_url)
                if att["ocr_text"] is None:
                    to_describe.append((att, data_url))
                if self.docs.can_ocr and TEXT_HINT.search(user_text):
                    text, source = await self.docs.read(path, mime)  # second opinion on dense text
                    if text:
                        doc_blocks.append(
                            f'<attachment_text file="{name}" source="{source}">\n{text}\n</attachment_text>')
            elif mime == "application/pdf" or mime.startswith("text/"):
                text = att["ocr_text"]
                source = "cached"
                if text is None:
                    text, source = await self.docs.read(path, mime)
                    if text:
                        self.memory.set_ocr(att["id"], att["message_id"], att["handle"], text)
                if text:
                    doc_blocks.append(f'<attachment_text file="{name}" source="{source}">\n{text}\n</attachment_text>')
                elif self.docs.can_ocr:
                    notes.append(f"[attachment {name} could not be read, even with OCR]")
                else:
                    notes.append(f"[attachment {name} has no readable text layer, and no OCR provider is set up]")
            else:
                notes.append(f"[attachment {name} ({mime or 'unknown type'}) is not something I can read yet]")
        return images, doc_blocks, notes, to_describe

    async def _ensure_downloaded(self, att):
        if att["status"] == "ready" and att["path"] and Path(att["path"]).exists():
            return att
        if not att["url"]:
            return None
        try:
            data = await self.linq.download(att["url"])
        except Exception as exc:
            log.warning("attachment download failed: %s", exc)
            self.memory.mark_attachment(att["id"], status="failed")
            return None
        sha = hashlib.sha256(data).hexdigest()
        folder = self.s.data_dir / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sha[:32]}{ext_for(att['mime'], att['filename'])}"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        self.memory.mark_attachment(att["id"], status="ready", path=str(path), sha256=sha)
        return self.memory.get_attachment(att["id"])

    async def _describe(self, att, data_url: str) -> None:
        """Store a durable text note for an image so it can be searched and recalled later."""
        messages = [
            {"role": "system", "content": DESCRIBE_PROMPT},
            {"role": "user", "content": [{"type": "text", "text": "Write the note for this image."},
                                         {"type": "image_url", "image_url": {"url": data_url}}]},
        ]
        try:
            text, _ = await self.llm.chat(messages, self.s.vision_models, max_tokens=500, temperature=0.0)
        except LLMError as exc:
            log.info("image note skipped: %s", exc)
            return
        self.memory.set_ocr(att["id"], att["message_id"], att["handle"], text)

    # ------------------------------------------------------------ consolidation
    def _kick_consolidation(self, handle: str, to_describe: list | None = None) -> None:
        if to_describe:
            self._to_describe.setdefault(handle, []).extend(to_describe)
        if handle in self._consolidating:
            return
        self._consolidating.add(handle)

        async def run() -> None:
            try:
                await asyncio.sleep(1.0)
                while True:
                    for att, data_url in self._to_describe.pop(handle, []):  # image notes first, so memory sees them
                        await self._describe(att, data_url)
                    for _ in range(3):
                        if not await self.memory.consolidate(handle):
                            break
                    if not self._to_describe.get(handle):  # more images queued while we worked
                        break
            except LLMUnavailable as exc:
                log.info("consolidation postponed: %s", exc)
            except Exception:
                log.exception("consolidation failed")
            finally:
                self._consolidating.discard(handle)

        self._spawn(run())
