"""One conversation turn: consent, safety, commands, attachments, memory, model, reply."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .config import Settings, mask
from .guard import FALLBACK as GUARD_FALLBACK, REWRITE_NOTE, violations
from .linq import Inbound, LinqClient, LinqError
from .llm import LLMClient, LLMError, LLMUnavailable
from .media import ext_for, to_data_url
from .memory import Memory
from .ocr import DocumentReader
from .prompts import (
    DESCRIBE_PROMPT, DISCLOSURE, FORGET_CONFIRM_TEXT, FORGET_DONE_TEXT, HELP_TEXT, SYSTEM_PROMPT,
    UNAVAILABLE_TEXT, WEARABLE_OFF, WEARABLE_ON, WELCOME,
)
from .safety import classify
from .textutil import clean_reply, fmt_ts, normalize_word, now_iso, parse_ts, split_reply

log = logging.getLogger("s2c.agent")

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "resume"}
HELP_WORDS = {"help", "commands"}
RECALL_CMD = re.compile(
    r"what (do|did) you (remember|know) about me|what do you remember|show (me )?(my )?(memory|notes)", re.I)
FORGET_CMD = re.compile(r"forget (everything|all|it all)")
FORGET_CONFIRM = re.compile(r"confirm forget( everything)?")
# Typed controls for the simulated wearable replay (deterministic, no model involved).
DEMO_CMD = re.compile(r"demo (play|start|resume|pause|stop|status|reset|day \d{1,2}|speed \d{1,2}(\.\d)?|alerts (on|off))")
IMAGE_HINT = re.compile(r"\b(photo|picture|image|screenshot|pic|selfie|receipt|scan|that one)\b", re.I)
# When someone wants the text out of an image, OCR runs as well and the model gets both views.
TEXT_HINT = re.compile(r"\b(read|scan|extract|transcribe|ocr|text|says?|written|handwrit\w*|copy|total|invoice|receipt)\b", re.I)

WINDOW_CHARS = 40_000
ATTACHMENT_HISTORY_CHARS = 24_000
MAX_IMAGES = 4


class Agent:
    def __init__(self, settings: Settings, memory: Memory, llm: LLMClient, linq: LinqClient,
                 docs: DocumentReader):
        self.s = settings
        self.memory = memory
        self.llm = llm
        self.linq = linq
        self.docs = docs
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
        self.demo = None            # async (handle, word) -> reply text, or None

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
        for m in ev.media:
            self.memory.add_attachment(
                att_id=str(m.get("id") or f"{ev.message_id}-{m.get('filename')}"), message_id=ev.message_id,
                handle=h, filename=m.get("filename"), mime=m.get("mime_type"),
                size_bytes=m.get("size_bytes"), url=m.get("url"),
            )

        word = normalize_word(ev.text)
        if self.memory.is_opted_out(h):
            if word in START_WORDS:
                self.memory.set_opt_out(h, False)
                await self._say(h, WELCOME, record=True)
            return
        if word in STOP_WORDS:
            self.memory.set_opt_out(h, True)  # Linq also blocks further sends to this person
            return
        if word in START_WORDS:
            await self._say(h, WELCOME, record=True)
            return
        if word in HELP_WORDS:
            await self._say(h, HELP_TEXT, record=True)
            return

        guidance = classify(ev.text)
        if guidance:
            await self._say(h, guidance, record=True)  # fixed text, the model is bypassed
            return

        if self.memory.pending_forget(h):
            if FORGET_CONFIRM.fullmatch(word):
                self.memory.wipe(h)
                await self._say(h, FORGET_DONE_TEXT, record=True)
                return
            self.memory.set_pending_forget(h, False)  # anything else cancels
        if FORGET_CMD.fullmatch(word):
            self.memory.set_pending_forget(h, True)
            await self._say(h, FORGET_CONFIRM_TEXT, record=True)
            return
        if RECALL_CMD.search(ev.text):
            await self._say(h, self.memory.export_for_user(h), record=True)
            return
        if self.demo and DEMO_CMD.fullmatch(word):
            reply = await self.demo(h, word)
            if reply:
                await self._say(h, reply, record=True)
                return

        if is_new:
            await self._say(h, DISCLOSURE, record=False)
        self.schedule(h)

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
        await self._say(handle, UNAVAILABLE_TEXT, record=False)

    async def sweeper(self) -> None:
        """Retry recent unanswered messages (for example after the model comes back)."""
        while True:
            await asyncio.sleep(45)
            try:
                for handle in self.s.allowed_numbers:
                    if handle in self._active or self.memory.is_opted_out(handle):
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

    # ------------------------------------------------------------------- output
    async def _say(self, handle: str, text: str, *, record: bool) -> str | None:
        """Send one iMessage. Returns the message id when it went out, else None."""
        try:
            resp = await self.linq.send_text(handle, text)
        except LinqError as exc:
            log.error("send failed: %s", exc)
            return None
        except Exception:
            log.exception("send failed")
            return None
        msg = resp.get("message") or {}
        msg_id = str(msg.get("id") or f"out-{time.time_ns()}")
        if record:
            self.memory.record_message(
                msg_id=msg_id, event_id=None, chat_id=str(resp.get("chat_id") or ""), handle=handle,
                direction="out", text=text, created_at=now_iso(),
            )
        return msg_id

    async def send_proactive(self, handle: str, text: str) -> str | None:
        """A message the system starts (an alert). Only to allow-listed people who have texted us and not opted out."""
        if handle not in self.s.allowed_numbers or self.memory.is_opted_out(handle):
            return None
        with self.memory.db.conn() as c:
            known = c.execute("SELECT 1 FROM users WHERE handle=?", (handle,)).fetchone()
        if not known:
            return None          # this person has never written to the line, so there is no consent to message them
        return await self._say(handle, text, record=True)

    # --------------------------------------------------------------------- turn
    async def _answer(self, handle: str, pending: list) -> bool:
        self._spawn(self.linq.typing(pending[-1]["chat_id"]))
        images, doc_blocks, notes, to_describe = await self._prepare_attachments(handle, pending)

        query = " ".join(r["text"] for r in pending) + " " + " ".join(doc_blocks)
        history = self.memory.recent_transcript(handle, WINDOW_CHARS)
        window_min = history[0]["rowid"] if history else pending[0]["rowid"]
        recalled = await self.memory.retrieve(handle, query, window_min_rowid=window_min)

        alert = self.alerts.open_for(handle) if self.alerts else None
        tool_specs = self.tools.specs_for(handle) if self.tools else None
        messages = self._build_messages(handle, history, pending, recalled, images, doc_blocks, notes,
                                        alert=alert, wearable=bool(tool_specs))
        # Router: turns with images go to the vision-capable models, everything else to the text models.
        models = self.s.vision_models if images else self.s.text_models
        text, usage = await self._complete(handle, messages, models, tool_specs)
        log.info("reply generated model=%s prompt_tokens=%s completion_tokens=%s images=%d tools=%s",
                 usage.get("model"), usage.get("prompt_tokens"), usage.get("completion_tokens"), len(images),
                 [t["name"] for t in usage.get("tools", [])] or "-")
        reply = clean_reply(text)
        if not reply:
            return False
        found = violations(reply)
        if found:                                   # firm rules for health: one rewrite, then a fixed answer
            log.warning("reply held by guard: %s", found)
            retry = messages + [{"role": "assistant", "content": reply}, {"role": "user", "content": "[system] " + REWRITE_NOTE}]
            try:
                text2, _ = await self.llm.chat(retry, models, max_tokens=500, temperature=0.3)
                reply = clean_reply(text2)
            except LLMError:
                reply = ""
            if not reply or violations(reply):
                reply = GUARD_FALLBACK

        sent = False
        for i, part in enumerate(split_reply(reply)):
            if i:
                await asyncio.sleep(0.8)
            sent = await self._say(handle, part, record=True) or sent
        if sent:
            self._kick_consolidation(handle, to_describe)
            if self.alerts:
                self.alerts.after_reply(handle)     # link what they said to the chart
        return sent

    async def _complete(self, handle: str, messages: list[dict], models: tuple[str, ...], tool_specs):
        if tool_specs:
            try:
                return await self.llm.chat_with_tools(messages, models, tool_specs, functools.partial(self.tools.run, handle),
                                                      max_tokens=700, temperature=0.5)
            except LLMError as exc:
                log.warning("tool turn failed (%s), answering without tools", exc)
        return await self.llm.chat(messages, models, max_tokens=700, temperature=0.5)

    def _build_messages(self, handle: str, history: list, pending: list, recalled: dict,
                        images: list[str], doc_blocks: list[str], notes: list[str], *,
                        alert: dict | None = None, wearable: bool = False) -> list[dict]:
        now = datetime.now().astimezone().strftime("%A, %B %d, %Y, %I:%M %p %Z")
        persona = self.alerts.persona_for(handle) if self.alerts else None
        sim = (self.replay.state(persona)["ts"] if (wearable and persona and self.replay) else None) or "not started"
        blocks = [SYSTEM_PROMPT, f"Current time: {now}.",
                  WEARABLE_ON.format(sim_time=sim) if wearable else WEARABLE_OFF]
        if alert:
            blocks.append(self.alerts.alert_context(alert))
        blocks.append(self.memory.render(handle, recalled))
        system = "\n\n".join(blocks)
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
