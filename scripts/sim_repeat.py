"""Starting the demo must text you every time. Runs 'demo restart' three times against a fake Linq and checks that each run
sends a 'Demo started' message and a new alert card (a repeat of the same event must not be swallowed by duplicate protection).

    uv run python scripts/sim_repeat.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DATA = tempfile.mkdtemp(prefix="s2c-repeat-")
os.environ.update(DATA_DIR=DATA, CLOUD_URL="", INGEST_TOKEN="", USE_POLLS="0")

import httpx  # noqa: E402

from signal2care.config import load_settings  # noqa: E402
from signal2care.fakelinq import FakeLinq  # noqa: E402
from signal2care.linq import Inbound  # noqa: E402
from signal2care.stack import build  # noqa: E402


async def main() -> None:
    settings = load_settings()
    handle = sorted(settings.allowed_numbers)[0]
    linq = FakeLinq()
    async with httpx.AsyncClient() as http:
        st = build(settings, http, linq)
        with st.memory.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO users(handle, created_at) VALUES(?, datetime('now'))", (handle,))
        ok = True
        for run in (1, 2, 3):
            n_before = len(linq.out)
            alerts_before = sum(1 for r in linq.out if r.get("media") and (r.get("key") or "").startswith("alert:"))
            await st.agent.on_event(Inbound(event_id=f"e{run}", message_id=f"in-{run}", chat_id="chat-1", handle=handle, sent_at=None,
                                            text="demo play" if run == 1 else "demo restart"))
            t0 = time.time()
            while time.time() - t0 < 120 and sum(1 for r in linq.out if r.get("media") and (r.get("key") or "").startswith("alert:")) == alerts_before:
                await asyncio.sleep(1)
            took = time.time() - t0
            new = linq.out[n_before:]
            started = any("Demo started" in r.get("text", "") for r in new)
            alert = sum(1 for r in new if r.get("media") and (r.get("key") or "").startswith("alert:"))
            print(f"run {run}: 'Demo started' sent={started}, alert cards sent={alert}, first alert after {took:.0f}s")
            ok &= started and alert >= 1
            print("   first lines:", [r.get("text", "").split("\n")[0][:60] for r in new if r.get("text")][:4])
        await st.pipeline.command({"type": "stop", "persona": "maya"})
        print("PASS: every start sent a message and an alert" if ok else "FAIL: a start did not send")
    shutil.rmtree(DATA, ignore_errors=True)


asyncio.run(main())
