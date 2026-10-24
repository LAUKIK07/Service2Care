"""Welcome, consent and a short health-notes interview, one question at a time.

State lives on the user row (onboarding: new -> consent -> profile -> done, ob_step = step*10 + sub). Nothing is ever
sent proactively until the person has agreed. Answers are stored as facts with the id of the message they came from,
so they can be shown, corrected or forgotten later. Anything can be skipped.
"""
from __future__ import annotations

import logging
import re

from . import cards, voice
from .memory import Memory
from .messenger import Messenger
from .textutil import normalize_word

log = logging.getLogger("s2c.onboarding")
QUESTION_START = re.compile(r"^(what|why|how|when|where|who|can|could|should|is|are|do|does|will|would)\b", re.I)


def _is_question(text: str) -> bool:
    return "?" in text and bool(QUESTION_START.match(text.strip()))


def _clean_name(text: str) -> str:
    t = re.sub(r"^(please )?(call me|my name is|i am|i'm|it's|its|this is)\s+", "", text.strip(), flags=re.I)
    return re.sub(r"[^\w\s'\-]", "", t).strip()[:40]


class Onboarding:
    def __init__(self, memory: Memory, messenger: Messenger):
        self.m = memory
        self.msg = messenger
        self.link = None            # () -> str | None   (the signed-in dashboard link)
        self.on_profile = None      # (handle) -> None   (push the profile to the dashboard)
        self.start_demo = None      # async (handle) -> None
        self.name_for_card = None

    # ----------------------------------------------------------------- begin
    async def begin(self, handle: str) -> None:
        state, _ = self.m.onboarding(handle)
        if state not in ("new",):
            return
        self.m.set_onboarding(handle, "consent", 0)
        await self.msg.say(handle, voice.WELCOME, kind="onboarding", bold=["Signal2Care"])
        await self.msg.ask(handle, voice.CONSENT_ASK, [voice.CONSENT_YES, voice.CONSENT_NO], purpose="consent")

    async def nudge(self, handle: str) -> None:
        """After answering an unrelated question in the middle of setup, ask the open question again, once."""
        state, step = self.m.onboarding(handle)
        if state == "consent":
            await self.msg.say(handle, voice.CONSENT_HINT, kind="onboarding")
        elif state == "profile":
            await self._ask_current(handle, again=True)

    # ------------------------------------------------------------------ input
    async def on_text(self, handle: str, text: str, msg_id: str) -> bool:
        """Returns True when the message was an answer to the setup and has been handled."""
        state, _ = self.m.onboarding(handle)
        w = normalize_word(text)
        if state == "consent":
            if w in ("start", "yes", "yes start checkins", "ok", "okay", "sure", "yes please", "turn on"):
                await self._consent(handle, True)
                return True
            if w in ("no", "not now", "later", "nope", "no thanks"):
                await self._consent(handle, False)
                return True
            return False
        if state == "profile":
            if _is_question(text):
                return False
            await self._answer(handle, text, msg_id)
            return True
        return False

    async def on_poll(self, handle: str, purpose: str, ref: str | None, option: str, msg_id: str) -> bool:
        if purpose == "consent":
            await self._consent(handle, option == voice.CONSENT_YES)
            return True
        if purpose == "profile":
            await self._answer(handle, option, msg_id, from_poll=True)
            return True
        if purpose == "start_demo":
            if option.lower().startswith("yes") and self.start_demo:
                await self.start_demo(handle)
            else:
                await self.msg.say(handle, "OK. Say \"demo play\" whenever you want to start.", kind="onboarding")
            return True
        return False

    # ----------------------------------------------------------------- steps
    async def _consent(self, handle: str, yes: bool) -> None:
        if not yes:
            self.m.set_consent(handle, False)
            self.m.set_onboarding(handle, "done", 0)
            await self.msg.say(handle, voice.CONSENT_DECLINED, kind="onboarding")
            return
        self.m.set_consent(handle, True)
        self.m.set_onboarding(handle, "profile", 0)
        await self.msg.say(handle, "Thank you. Check-ins are on. " + voice.PROFILE_INTRO, kind="onboarding", effect=("screen", "sparkles"))
        await self._ask_current(handle)

    async def _ask_current(self, handle: str, again: bool = False) -> None:
        _, raw = self.m.onboarding(handle)
        idx, sub = divmod(raw, 10)
        if idx >= len(voice.PROFILE_STEPS):
            await self._finish(handle)
            return
        step = voice.PROFILE_STEPS[idx]
        prefix = "Back to your setup. " if again else ""
        key = f"ob:{handle}:{idx}:{sub}:{'r' if again else 'a'}"
        if sub == 1:
            await self.msg.say(handle, prefix + step["detail"], kind="onboarding", key=key)
        elif step["kind"] == "yesno":
            await self.msg.ask(handle, prefix + step["ask"], ["Yes", "No", "Skip"], purpose="profile", ref=step["key"], key=key)
        elif step["kind"] == "choice":
            await self.msg.ask(handle, prefix + step["ask"], step["options"], purpose="profile", ref=step["key"], key=key)
        else:
            await self.msg.say(handle, prefix + step["ask"], kind="onboarding", key=key)

    async def _answer(self, handle: str, text: str, msg_id: str, from_poll: bool = False) -> None:
        _, raw = self.m.onboarding(handle)
        idx, sub = divmod(raw, 10)
        if idx >= len(voice.PROFILE_STEPS):
            await self._finish(handle)
            return
        step = voice.PROFILE_STEPS[idx]
        w = normalize_word(text)
        skipped = w in voice.SKIP_WORDS
        value: str | None = None
        go_next = True
        if skipped:
            pass
        elif step["kind"] == "yesno" and sub == 0:
            if w in voice.NONE_WORDS:
                value = "None reported"
            elif w in voice.YES_WORDS:
                self.m.set_onboarding(handle, "profile", idx * 10 + 1)          # now ask for the detail
                go_next = False
            else:
                value = text.strip()[:300]                                       # they typed the detail straight away
        elif step["key"] == "preferred_name":
            value = _clean_name(text) or None
        elif step["key"] == "other_health_notes":
            value = "None reported" if w in voice.NONE_WORDS else text.strip()[:300]
        else:
            value = text.strip()[:300]
        if value:
            self.m.set_fact(handle, step["key"], value, [msg_id], "onboarding")
            self._push_profile(handle)
        if not go_next:
            await self._ask_current(handle)
            return
        self.m.set_onboarding(handle, "profile", (idx + 1) * 10)
        await self._ask_current(handle)

    def _push_profile(self, handle: str) -> None:
        if self.on_profile:
            try:
                self.on_profile(handle)
            except Exception:
                log.exception("profile push failed")

    async def _finish(self, handle: str) -> None:
        self.m.set_onboarding(handle, "done", 0)
        items = [(i["label"], i["value"]) for i in self.m.profile_items(handle) if i["key"] != "message_style"]
        name = self.m.fact_value(handle, "preferred_name")
        if items:
            png = cards.profile_card(name=name or "", items=items, note="Say \"correct that\" or \"forget that\" any time.")
            await self.msg.card(handle, png, voice.PROFILE_DONE, kind="onboarding", key=f"ob:{handle}:card", filename="your-notes.png")
        done = "You are all set. I will text you only when something looks different from your usual. Say PAUSE any time to stop check-ins for a while."
        await self.msg.say(handle, done, kind="onboarding", effect=("screen", "confetti"), key=f"ob:{handle}:done")
        link = self.link() if self.link else None
        if link:
            await self.msg.say(handle, "Here is your private dashboard. It shows your numbers, my check-ins, and what you tell me. "
                                       "Keep this link to yourself.", kind="onboarding", key=f"ob:{handle}:linktext")
            await self.msg.link(handle, link, key=f"ob:{handle}:link")
        await self.msg.ask(handle, "Want to see it work? I can start the demo data now.", ["Yes, start", "Not yet"], purpose="start_demo",
                           key=f"ob:{handle}:startdemo")
