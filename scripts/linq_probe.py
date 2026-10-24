"""Live check of every Linq feature the product uses, against the real API. Sends a handful of clearly labelled messages
to the first number in ALLOWED_NUMBERS. Run it on purpose, not in tests.

    uv run python scripts/linq_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from signal2care import cards  # noqa: E402
from signal2care.config import load_settings  # noqa: E402
from signal2care.linq import LinqClient, LinqError, text_part, utf16_range  # noqa: E402


async def step(name, coro):
    t0 = time.time()
    try:
        out = await coro
        print(f"  OK    {name}  ({time.time() - t0:.1f}s)")
        return out
    except LinqError as exc:
        print(f"  FAIL  {name}: {exc}")
    except Exception as exc:
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    return None


async def main() -> None:
    s = load_settings()
    handle = sorted(s.allowed_numbers)[0]
    run = int(time.time())
    async with httpx.AsyncClient() as http:
        linq = LinqClient(s.linq_api_key, http)
        print("Linq probe (6 visible messages to your phone)")
        print("  iMessage available:", await linq.imessage_available(handle))
        text = "Signal2Care test 1 of 6: this word is bold, and this one is not."
        rng = utf16_range(text, "bold")
        first = await step("1 text with bold styling", linq.send(handle, [text_part(text, bold=[rng])], idempotency_key=f"probe-{run}-1"))
        chat_id = (first or {}).get("chat_id")
        mid = ((first or {}).get("message") or {}).get("id")
        print("     chat:", bool(chat_id), "message id:", bool(mid))
        if not chat_id:
            return
        png = cards.metric_card(label="Test card", value="42", unit="units", status_text="Image upload works", usual="Usual 40",
                                values=[40 + (i % 7) * 0.3 for i in range(96)], band=[(39, 41)] * 96, mark=(60, 95), when="Probe",
                                footer="Signal2Care test 2 of 6. Demo data.")
        await step("2 image card (upload + send)", linq.send_image(handle, png, caption="Test 2 of 6: a picture card with a caption.",
                                                                  chat_id=chat_id, idempotency_key=f"probe-{run}-2"))
        await asyncio.sleep(1)
        poll = await step("3 poll", linq.poll(chat_id, ["Option A", "Option B", "Option C"], idempotency_key=f"probe-{run}-3"))
        if poll:
            opts = (poll.get("poll") or {}).get("options") or []
            print("     poll message id:", bool(poll.get("message_id")), "| options with ids:", len(opts), "| keys:", sorted(poll.keys())[:6])
        if mid:
            await step("4 tapback (heart) on message 1", linq.react(mid, "love"))
            await asyncio.sleep(1)
            await step("5 threaded reply to message 1", linq.send(handle, [{"type": "text", "value": "Test 5 of 6: this is a reply attached to test 1."}],
                                                                 chat_id=chat_id, reply_to=mid, idempotency_key=f"probe-{run}-5"))
        await asyncio.sleep(1)
        await step("6 rich link preview", linq.send_link(handle, "https://laukik07.github.io/Service2Care/", chat_id=chat_id, idempotency_key=f"probe-{run}-6"))
        await step("   mark chat read", linq.mark_read(chat_id))
        await step("   typing on", linq.typing(chat_id, True))
        await step("   typing off", linq.typing(chat_id, False))
        r = await step("   same idempotency key again (must not send twice)", linq.send(handle, [text_part(text, bold=[rng])], chat_id=chat_id, idempotency_key=f"probe-{run}-1"))
        print("     same message id returned:", ((r or {}).get("message") or {}).get("id") == mid)


asyncio.run(main())
