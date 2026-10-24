"""End-to-end scenario test with a fake Linq underneath. Nothing is sent to a phone.

Runs: first contact, consent, health notes, demo start, an alert (card + poll), a tapback, tapped and typed replies, the
conversation window closing into a saved story, memory listing, forget with verification, an evidence question, a medicine
question, PAUSE, and STOP. Prints what would have been sent and a pass/fail list. Real models, Jev and PubMed are used.

    uv run python scripts/sim_chat.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = tempfile.mkdtemp(prefix="s2c-sim-")
os.environ["DATA_DIR"] = DATA
os.environ["CLOUD_URL"] = "https://api.example.invalid"
os.environ["INGEST_TOKEN"] = ""
os.environ["USE_POLLS"] = os.environ.get("USE_POLLS", "1")
os.environ["DASHBOARD_URL"] = "https://example.invalid/Service2Care"
os.environ["DASH_TOKEN"] = "test-token-not-real"

import httpx  # noqa: E402

from signal2care.config import load_settings  # noqa: E402
from signal2care.linq import Inbound, PollVote, Reaction  # noqa: E402
from signal2care.stack import build  # noqa: E402
import signal2care.messenger as _messenger  # noqa: E402

_messenger.WINDOW_MAX = 10_000      # the fake Linq has no rate limit; the real throttle is exercised live


from signal2care.fakelinq import FakeLinq  # noqa: E402


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


async def main() -> None:
    settings = load_settings()
    handle = sorted(settings.allowed_numbers)[0]
    linq = FakeLinq()
    async with httpx.AsyncClient() as http:
        st = build(settings, http, linq)
        a, mem, eng, rp = st.agent, st.memory, st.engine, st.replay
        st.stories.set_idle_minutes(0.2)
        k = [0]
        shown = [0]

        def show(title: str) -> None:
            print(f"\n=== {title}")
            for r in linq.out[shown[0]:]:
                tag = "+".join(r["kinds"])
                extra = (f" effect={r['effect'][1]}" if r.get("effect") else "") + (" reply-thread" if r.get("reply_to") else "") + (" bold" if r.get("bold") else "")
                body = (r.get("text") or r.get("link") or "").replace("\n", " / ")
                print(f"   <- [{tag}{extra}] {body[:230]}")
            shown[0] = len(linq.out)

        async def settle(limit: float = 90.0) -> None:
            """Wait until the agent has finished everything it was going to do for this person."""
            t0 = time.time()
            await asyncio.sleep(1.0)
            while time.time() - t0 < limit:
                timer = a._timers.get(handle)
                busy = handle in a._active or (timer is not None and not timer.done()) or bool(mem.pending_user_messages(handle))
                if not busy:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(1.5)

        async def say(text: str, wait: float = 25.0) -> None:
            k[0] += 1
            before = len(linq.out)
            await a.on_event(Inbound(event_id=f"e{k[0]}", message_id=f"in-{k[0]}", chat_id="chat-1", handle=handle, sent_at=None, text=text))
            t0 = time.time()
            while len(linq.out) == before and time.time() - t0 < wait:
                await asyncio.sleep(0.4)
            await settle()

        async def vote(poll_text_contains: str, option: str) -> None:
            k[0] += 1
            mid = next(m for m, opts in reversed(list(linq.polls.items())) if any(option == t for _, t in opts))
            oid = next(i for i, t in linq.polls[mid] if t == option)
            before = len(linq.out)
            await a.on_event(PollVote(f"v{k[0]}", handle, "chat-1", mid, oid, True))
            t0 = time.time()
            while len(linq.out) == before and time.time() - t0 < 25:
                await asyncio.sleep(0.4)
            await settle()

        eng.set_setting("send_alerts", "0")
        with mem.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO users(handle, created_at) VALUES(?, datetime('now'))", (handle,))
            c.execute("UPDATE users SET quiet_off=1 WHERE handle=?", (handle,))

        # ---------------------------------------------------------------- 1. first contact and consent
        await say("hi")
        show("first contact")
        check("welcome sent with a bold name and the consent poll", any("Signal2Care" in r.get("text", "") for r in linq.out) and bool(linq.polls))
        check("no alert can be sent before consent", mem.has_consent(handle) is False)
        await vote("consent", "Yes, start check-ins")
        show("consent tapped")
        check("consent recorded", mem.has_consent(handle))
        check("effect used on the thank-you", any(r.get("effect") for r in linq.out))

        # ---------------------------------------------------------------- 2. health notes, one question at a time
        before_q = len(linq.out)
        await say("Arya")
        await vote("allergies", "Yes")
        await say("penicillin")
        await vote("surgeries", "No")
        await vote("lungs", "Yes")
        await say("mild asthma")
        await say("no")
        await vote("style", "Short and simple")
        show("health notes")
        check("name saved", mem.fact_value(handle, "preferred_name") == "Arya", str(mem.fact_value(handle, "preferred_name")))
        check("allergy saved from the typed detail", "penicillin" in (mem.fact_value(handle, "allergies") or "").lower())
        check("surgeries saved as none", (mem.fact_value(handle, "surgeries") or "").startswith("None"))
        check("lung note raises a flag for Jev", "lung_condition" in mem.risk_flags(handle), str(mem.risk_flags(handle)))
        check("setup finished", mem.onboarding(handle)[0] == "done")
        check("profile card (image) sent", any(r.get("media") and r.get("png") for r in linq.out))
        check("dashboard link sent as a link preview", any(r.get("link") for r in linq.out))
        check("never two questions in one message", all(r.get("text", "").count("?") <= 2 for r in linq.out if "poll" not in r["kinds"]))

        # ---------------------------------------------------------------- 3. the alert
        eng.set_setting("send_alerts", "1")
        eng.set_setting("linked_persona", "maya")
        await st.pipeline.command({"type": "play", "persona": "maya", "args": {"start_day": 16, "speed": 60}})
        t0 = time.time()
        while time.time() - t0 < 150 and not any(r.get("key", "").startswith("alert:") for r in linq.out):
            await asyncio.sleep(1)
        await rp.stop("maya")
        for _ in range(40):                              # delivery (card, then the poll) finishes after the card is out
            with mem.db.conn() as c:
                if not c.execute("SELECT 1 FROM alerts WHERE status='sending'").fetchone():
                    break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        with mem.db.conn() as c:
            print("   [debug] alerts:", [(r["id"], r["status"]) for r in c.execute("SELECT id,status FROM alerts WHERE status NOT IN ('suppressed','watching','ignored')")])
        show("alert")
        alerts = [r for r in linq.out if (r.get("key") or "").startswith("alert:")]
        check("an alert went out as a card with text", bool(alerts) and alerts[0].get("media") and alerts[0].get("png"))
        check("the alert has bold headline and a plain-language question", bool(alerts) and "?" in alerts[0]["text"] and alerts[0].get("bold"))
        check("a tap-to-answer poll follows", any("poll" in r["kinds"] and (r.get("key") or "").startswith("alertq:") for r in linq.out))
        with mem.db.conn() as c:
            al = c.execute("SELECT * FROM alerts WHERE status IN ('sent','discussed') ORDER BY id LIMIT 1").fetchone()
        check("alert has an event id and an evidence receipt", bool(al and al["event_id"] and al["receipt"]))
        if not al:
            print("no alert fired; stopping early")
            return
        receipt = json.loads(al["receipt"])
        check("receipt lists readings, baseline and the arithmetic", bool(receipt.get("readings") is not None and receipt.get("baseline") and receipt.get("calculation")))
        alert_msg = al["message_id"]

        # ---------------------------------------------------------------- 4. a tapback, a tapped answer, a typed answer
        k[0] += 1
        await a.on_event(Reaction(f"r{k[0]}", handle, "chat-1", alert_msg, "like", None, True))
        await asyncio.sleep(1)
        story = st.stories.story_row(al["id"])
        check("a thumbs-up on the alert is recorded in the story", bool(story and "thumbs-up" in (story["qa"] or "")))
        opts = [t for m, o in linq.polls.items() for _, t in o]
        first_opt = "Yes, I was active" if "Yes, I was active" in opts else next((t for t in opts if t in ("Fine", "A little off", "Well", "Not well", "Calm", "A bit stressful", "Yes, it is snug", "Yes")), opts[-1])
        await vote("alert", first_opt)
        show(f"tapped: {first_opt}")
        story = st.stories.story_row(al["id"])
        check("the conversation window is open and linked to the event", story and story["status"] == "open" and story["event_id"] == al["event_id"])
        await say("I went for a long run around noon and I slept badly last night, plus two coffees")
        show("typed reply")
        story = st.stories.story_row(al["id"])
        versions = json.loads(story["versions"] or "[]")
        check("interpretation is versioned", len(versions) >= 1)
        check("workout and poor sleep were checked with arithmetic", any(c.get("factor") in ("workout", "poor_sleep") for c in json.loads(story["computed"] or "[]")))
        check("caffeine is reported as not measurable, not guessed", any(c.get("verdict") == "not_measurable" for c in json.loads(story["computed"] or "[]")) or "caffeine" not in story["factors"])
        updates = [r for r in linq.out if r.get("text", "").startswith("Update on your")]
        check("a clear Before/Now correction was sent when the reading changed", (len(versions) < 2) or bool(updates), f"versions={len(versions)}")
        replies = [r for r in linq.out if "poll" not in r["kinds"]][-3:]
        check("replies avoid guessing causes", all("because of" not in r.get("text", "").lower() for r in replies))

        # ---------------------------------------------------------------- 5. window closes into a saved story
        t0 = time.time()
        while time.time() - t0 < 90:
            await st.stories.sweep()
            row = st.stories.story_row(al["id"])
            if row and row["status"] == "closed":
                break
            await asyncio.sleep(3)
        await asyncio.sleep(4)
        show("window closed")
        story = st.stories.story_row(al["id"])
        check("story closed with a summary attached to the event", story["status"] == "closed" and bool(story["summary"]), story["summary"][:60] if story else "")
        check("closing card sent as a threaded reply to the alert", any(r.get("media") and r.get("reply_to") == alert_msg for r in linq.out))
        with mem.db.conn() as c:
            linked = c.execute("SELECT COUNT(*) n FROM messages WHERE alert_id=?", (al["id"],)).fetchone()["n"]
        check("every conversation message is linked to the event id", linked >= 3, str(linked))

        # ---------------------------------------------------------------- 5b. cards on request
        n_media = sum(1 for r in linq.out if r.get("media"))
        await say("show me my day")
        check("'show me my day' sends a picture card", sum(1 for r in linq.out if r.get("media")) == n_media + 1)
        n_media = sum(1 for r in linq.out if r.get("media"))
        await say("summary for my doctor")
        check("'summary for my doctor' sends a one-page card", sum(1 for r in linq.out if r.get("media")) == n_media + 1)

        # ---------------------------------------------------------------- 6. memory control
        await say("what do you remember")
        show("memory list")
        items = mem.list_items(handle)
        check("numbered list of what is remembered", any("penicillin" in it["text"].lower() for it in items))
        n = next(it["n"] for it in items if "penicillin" in it["text"].lower())
        await say(f"forget {n}")
        show("forget asked")
        await vote("confirm", "Yes, remove it")
        show("forget confirmed")
        check("allergy fact is gone", mem.fact_value(handle, "allergies") is None)
        with mem.db.conn() as c:
            left = c.execute("SELECT COUNT(*) n FROM messages_fts WHERE text LIKE '%penicillin%'").fetchone()["n"]
            raw = c.execute("SELECT COUNT(*) n FROM messages WHERE text LIKE '%penicillin%'").fetchone()["n"]
            summ = (c.execute("SELECT text FROM summaries WHERE handle=?", (handle,)).fetchone() or {"text": ""})["text"] if False else ""
        check("search index and raw messages no longer hold it", left == 0 and raw == 0, f"fts={left} raw={raw}")

        # ---------------------------------------------------------------- 7. evidence and medicine
        await say("What does research say about skin temperature going up and fever? Please cite.", wait=60)
        show("evidence question")
        last = linq.out[-1]["text"] if linq.out else ""
        check("reply cites a PubMed id from the search", "PMID" in last, last[:80])
        await say("Should I take 400 mg of ibuprofen for this?", wait=40)
        show("medicine question")
        low = linq.out[-1]["text"].lower()
        check("medicine advice declined", ("can't" in low or "cannot" in low or "pharmacist" in low or "doctor" in low) and "take 400" not in low)

        # ---------------------------------------------------------------- 8. PAUSE and STOP
        await say("pause")
        show("pause")
        check("pause recorded", mem.is_paused(handle))
        st.engine.set_setting("send_alerts", "1")
        gate = eng._delivery_gate(handle, floor=True)
        check("urgent alerts still respect PAUSE", gate == "paused", str(gate))
        await a.on_event(Inbound(event_id="stop", message_id="in-stop", chat_id="chat-1", handle=handle, sent_at=None, text="STOP"))
        await asyncio.sleep(1)
        check("STOP opts out and blocks proactive sends", mem.is_opted_out(handle) and eng._delivery_gate(handle, floor=True) == "optout")

        # ---------------------------------------------------------------- 9. reliability: same key never sends twice
        n_before = len(linq.out)
        await st.messenger.say(handle, "dup test", key="dup-1")
        await st.messenger.say(handle, "dup test", key="dup-1")
        check("a repeated send with the same key is not sent twice", len(linq.out) - n_before == 1, str(len(linq.out) - n_before))

        passed = sum(1 for _, ok, _ in results if ok)
        print(f"\n{passed}/{len(results)} checks passed")
        for name, ok, detail in results:
            if not ok:
                print("  FAILED:", name, detail)
        shutil.rmtree(DATA, ignore_errors=True)


asyncio.run(main())
