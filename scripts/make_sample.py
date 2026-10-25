"""Record a full simulated run of Arya into dashboard/sample.json, the preview shown to visitors without a live link.

The monitor, Jev and the language model are the real ones. No iMessage is sent. Two replies are scripted
(a tracker on a charger, and a run plus a bad night) so the preview can show how replies are attached to the chart.
Run:  uv run python scripts/make_sample.py   (needs DEEPINFRA_API_KEY and OPENROUTER_API_KEY in .env)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="s2c-sample-")

import httpx  # noqa: E402

from signal2care.alerts import AlertEngine  # noqa: E402
from signal2care.cloud import CloudSync  # noqa: E402
from signal2care.config import load_settings  # noqa: E402
from signal2care.db import Database  # noqa: E402
from signal2care.jev import JevClient  # noqa: E402
from signal2care.llm import LLMClient  # noqa: E402
from signal2care.memory import Memory  # noqa: E402
from signal2care.pipeline import Pipeline  # noqa: E402
from signal2care.replay import Replay  # noqa: E402
from signal2care.textutil import now_iso  # noqa: E402

PERSONA = "maya"
START_DAY = 13
REPLIES = {
    "data_quality": "Oh, it was on the charger, I just forgot to put it back on. All good now.",
    "skin_temp": "Yeah I did a long run around noon and I slept badly last night.",
}


class Recorder(CloudSync):
    def __init__(self, http):
        super().__init__("", "", http)
        self.items: list[tuple[str, dict]] = []

    def push(self, kind: str, obj: dict) -> None:
        self.items.append((kind, obj))


async def main() -> None:
    s = load_settings()
    if not (s.llm_api_key and s.openrouter_api_key):
        sys.exit("DEEPINFRA_API_KEY and OPENROUTER_API_KEY are required")
    handle = sorted(s.allowed_numbers)[0]
    async with httpx.AsyncClient() as http:
        db = Database(s.data_dir / "s2c.sqlite3")
        llm = LLMClient(s.llm_base_url, s.llm_api_key, http, embed_model=s.embed_model)
        jev = JevClient(s.openrouter_api_key, http, s.jev_model)
        mem = Memory(db, llm, s)
        mem.ensure_user(handle)
        rec = Recorder(http)
        eng = AlertEngine(s, db, llm, jev, rec)
        sent = 0

        async def say(h: str, text: str) -> str:
            nonlocal sent
            sent += 1
            mid = f"sample-out-{sent}"
            mem.record_message(msg_id=mid, event_id=None, chat_id="c", handle=h, direction="out", text=text, created_at=now_iso())
            return mid

        eng.say = say
        eng.set_setting("send_alerts", "1")
        pipe = Pipeline(db, eng, rec)
        replay = Replay(db, pipe)
        pipe.replay = replay
        eng.replay = replay
        await pipe.command({"type": "play", "persona": PERSONA, "args": {"start_day": START_DAY, "speed": 60}})
        answered: set[int] = set()
        t0 = time.time()
        while time.time() - t0 < 600:
            await asyncio.sleep(0.5)
            with db.conn() as c:
                rows = c.execute("SELECT id, metric FROM alerts WHERE status='sent' ORDER BY id").fetchall()
            for r in rows:
                if r["id"] in answered or r["metric"] not in REPLIES:
                    continue
                answered.add(r["id"])
                await replay.stop(PERSONA)
                mem.record_message(msg_id=f"sample-in-{r['id']}", event_id=f"e{r['id']}", chat_id="c", handle=handle, direction="in",
                                   text=REPLIES[r["metric"]], created_at=now_iso())
                eng.after_reply(handle)
                for _ in range(60):
                    await asyncio.sleep(1)
                    with db.conn() as c:
                        if c.execute("SELECT 1 FROM annotations WHERE alert_id=?", (r["id"],)).fetchone():
                            break
                await pipe.command({"type": "play", "persona": PERSONA})
            if replay.state(PERSONA)["status"] == "finished":
                break
        await asyncio.sleep(10)                      # let the last model calls land
        await replay.stop(PERSONA)

        # Rebuild what the dashboard service would hold from the same pushes it would have received.
        series, feed, alerts, notes, thread = {}, {}, {}, {}, {}
        state = monitor = None
        for kind, d in rec.items:
            if kind == "reset":
                series, feed, alerts, notes, thread = {}, {}, {}, {}, {}
            elif kind == "series":
                series[d["i"]] = d
            elif kind == "feed":
                feed[d["id"]] = d
            elif kind == "alert":
                alerts[d["id"]] = dict(d)
            elif kind == "alert_status" and d["id"] in alerts:
                alerts[d["id"]]["status"] = d["status"]
                if d["status"] == "resolved":
                    alerts[d["id"]]["closed_at"] = now_iso()
            elif kind == "annotation":
                notes[d["alert_id"]] = d
            elif kind == "thread":
                thread[(d["alert_id"], d["mid"])] = d
            elif kind == "state":
                state = d
            elif kind == "monitor":
                monitor = d
        out = {
            "persona": PERSONA,
            "note": "Recorded with the real monitor, Jev and language model. No texts were sent, and the two replies shown were scripted.",
            "states": {PERSONA: state},
            "series": [{"i": i, "ts": t["ts"], "state": t["state"], "quality": t["quality"], "v": t["v"]} for i, t in sorted(series.items())],
            "feed": [d for _, d in sorted(feed.items())][-220:],
            "alerts": [{**a, "evidence": json.loads(a["evidence"]), "decision": json.loads(a["decision"])} for _, a in sorted(alerts.items())],
            "annotations": [{**n, "factors": json.loads(n["factors"]), "computed": json.loads(n["computed"])} for _, n in sorted(notes.items())],
            "thread": sorted(thread.values(), key=lambda t: (t["created_at"], t["mid"])),
            "monitor": monitor,
            "rev": 1,
            "health": {"text_model": s.text_models[0], "jev": True, "jev_model": s.jev_model, "ocr": "ocrspace"},
        }
        target = ROOT / "dashboard" / "sample.json"
        target.write_text(json.dumps(out, separators=(",", ":")))
        print(f"wrote {target} ({target.stat().st_size // 1024} KB): {len(out['series'])} ticks, "
              f"{len(out['alerts'])} alert records, {len(out['annotations'])} annotations, {len(out['thread'])} thread messages")


asyncio.run(main())
