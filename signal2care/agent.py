"""One conversation turn: consent, safety, commands, media, memory, model, reply."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .config import Settings, mask
from .linq import Inbound, LinqClient, LinqError
from .media import ext_for, to_data_url
from .memory import Memory
from .mistral import LLMUnavailable, MistralClient, MistralError
from .prompts import (
    DISCLOSURE, FORGET_CONFIRM_TEXT, FORGET_DONE_TEXT, HELP_TEXT, SYSTEM_PROMPT, UNAVAILABLE_TEXT, WELCOME,
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
IMAGE_HINT = re.compile(r"\b(photo|picture|image|screenshot|pic|selfie|receipt|scan|that one)\b", re.I)

WINDOW_CHARS = 40_000
MAX_IMAGES = 4


class Agent:
    def __init__(self, settings: Settings, memory: Memory, mistral: MistralClient, linq: LinqClient):
        self.s = settings
        self.memory = memory
        self.mistral = mistral
        self.linq = linq
        self._timers: dict[str, asyncio.Task] = {}
        self._active: set[str] = set()
        self._consolidating: set[str] = set()
        self._notice_at: dict[str, float] = {}
        self._sweep_tries: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()

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
    async def _say(self, handle: str, text: str, *, record: bool) -> bool:
        try:
            resp = await self.linq.send_text(handle, text)
        except LinqError as exc:
            log.error("send failed: %s", exc)
            return False
        except Exception:
            log.exception("send failed")
            return False
        if record:
            msg = resp.get("message") or {}
            self.memory.record_message(
                msg_id=str(msg.get("id") or f"out-{time.time_ns()}"), event_id=None,
                chat_id=str(resp.get("chat_id") or ""), handle=handle, direction="out", text=text,
                created_at=now_iso(),
            )
        return True

    # --------------------------------------------------------------------- turn
    async def _answer(self, handle: str, pending: list) -> bool:
        self._spawn(self.linq.typing(pending[-1]["chat_id"]))
        images, ocr_blocks, notes = await self._prepare_attachments(handle, pending)

        query = " ".join(r["text"] for r in pending) + " " + " ".join(ocr_blocks)
        history = self.memory.recent_transcript(handle, WINDOW_CHARS)
        window_min = history[0]["rowid"] if history else pending[0]["rowid"]
        recalled = await self.memory.retrieve(handle, query, window_min_rowid=window_min)

        messages = self._build_messages(handle, history, pending, recalled, images, ocr_blocks, notes)
        text, usage = await self.mistral.chat(messages, self.s.chat_models, max_tokens=700, temperature=0.5)
        log.info("reply generated model=%s prompt_tokens=%s completion_tokens=%s",
                 usage.get("model"), usage.get("prompt_tokens"), usage.get("completion_tokens"))
        reply = clean_reply(text)
        if not reply:
            return False

        sent = False
        for i, part in enumerate(split_reply(reply)):
            if i:
                await asyncio.sleep(0.8)
            sent = await self._say(handle, part, record=True) or sent
        if sent:
            self._kick_consolidation(handle)
        return sent

    def _build_messages(self, handle: str, history: list, pending: list, recalled: dict,
                        images: list[str], ocr_blocks: list[str], notes: list[str]) -> list[dict]:
        now = datetime.now().astimezone().strftime("%A, %B %d, %Y, %I:%M %p %Z")
        system = f"{SYSTEM_PROMPT}\n\nCurrent time: {now}.\n\n{self.memory.render(handle, recalled)}"
        msgs: list[dict] = [{"role": "system", "content": system}]
        pending_ids = {r["rowid"] for r in pending}
        for r in history:
            if r["rowid"] in pending_ids:
                continue
            role = "user" if r["direction"] == "in" else "assistant"
            if role == "assistant" and len(msgs) == 1:
                continue  # the first turn after the system prompt must be the user's
            content = f"[{fmt_ts(r['created_at'])}] {r['text']}" if role == "user" else r["text"]
            if msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content
            else:
                msgs.append({"role": role, "content": content})

        body = "\n".join(f"[{fmt_ts(r['created_at'])}] {r['text']}" for r in pending)
        if ocr_blocks:
            body += "\n\n" + "\n\n".join(ocr_blocks)
        if notes:
            body += "\n\n" + "\n".join(notes)
        content: str | list = body
        if images:
            content = [{"type": "text", "text": body}] + [
                {"type": "image_url", "image_url": u} for u in images[:MAX_IMAGES]
            ]
        if msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], str) and isinstance(content, str):
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": "user", "content": content})
        return msgs

    # -------------------------------------------------------------------- media
    async def _prepare_attachments(self, handle: str, pending: list) -> tuple[list[str], list[str], list[str]]:
        images: list[str] = []
        ocr_blocks: list[str] = []
        notes: list[str] = []
        atts = [a for r in pending for a in self.memory.attachments_for(r["id"])]
        if not atts and IMAGE_HINT.search(" ".join(r["text"] for r in pending)):
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
                ocr = att["ocr_text"]
                if ocr is None:
                    ocr = await self._ocr(att, data_url, "image_url")
            elif mime == "application/pdf":
                data_url = "data:application/pdf;base64," + base64.b64encode(path.read_bytes()).decode()
                ocr = att["ocr_text"]
                if ocr is None:
                    ocr = await self._ocr(att, data_url, "document_url")
                if not ocr:
                    notes.append(f"[attachment {name} is a PDF I could not read]")
            else:
                notes.append(f"[attachment {name} ({mime or 'unknown type'}) is not something I can read yet]")
                continue
            if ocr:
                ocr_blocks.append(f'<image_text file="{name}">\n{ocr[:6000]}\n</image_text>')
        return images, ocr_blocks, notes

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

    async def _ocr(self, att, data_url: str, kind: str) -> str:
        try:
            text = await self.mistral.ocr(data_url, kind)
        except MistralError as exc:
            log.info("ocr skipped: %s", exc)
            return ""
        self.memory.set_ocr(att["id"], att["message_id"], att["handle"], text)
        return text

    # ------------------------------------------------------------ consolidation
    def _kick_consolidation(self, handle: str) -> None:
        if handle in self._consolidating:
            return
        self._consolidating.add(handle)

        async def run() -> None:
            try:
                await asyncio.sleep(1.5)
                for _ in range(3):
                    if not await self.memory.consolidate(handle):
                        break
            except LLMUnavailable as exc:
                log.info("consolidation postponed: %s", exc)
            except Exception:
                log.exception("consolidation failed")
            finally:
                self._consolidating.discard(handle)

        self._spawn(run())
