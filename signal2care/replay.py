"""Replays a synthetic persona's stream tick by tick, the way a live device feed would arrive.

One tick is 15 simulated minutes. `speed` is ticks per real second. Every tick is stored, shown to
the monitor, and handed to the hooks (alerting, model reviews, cloud mirror). The generator is
deterministic, so a restart can rebuild the monitor's state by replaying the past silently.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .db import Database
from .monitor import Monitor
from .registry import PERSONAS
from .sim.generator import TICKS_PER_DAY
from .textutil import now_iso

log = logging.getLogger("s2c.replay")


class Replay:
    def __init__(self, db: Database, hooks):
        self.db = db
        self.hooks = hooks
        self._ticks: dict[str, list[dict]] = {}
        self._monitors: dict[str, Monitor] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._speed: dict[str, float] = {}

    # ------------------------------------------------------------------ access
    def ticks(self, persona: str) -> list[dict]:
        if persona not in self._ticks:
            self._ticks[persona] = PERSONAS.ticks(persona)
        return self._ticks[persona]

    def monitor(self, persona: str) -> Monitor:
        if persona not in self._monitors:
            mon = Monitor(persona)
            for t in self.ticks(persona)[: self._row(persona)["cursor"]]:   # rebuild after a restart
                mon.ingest(t, silent=True)
            self._monitors[persona] = mon
        return self._monitors[persona]

    def _row(self, persona: str) -> dict:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM replay WHERE persona=?", (persona,)).fetchone()
        return dict(r) if r else {"persona": persona, "status": "idle", "cursor": 0, "speed": 2.0}

    def _save(self, persona: str, **kw) -> None:
        row = {**self._row(persona), **kw, "updated_at": now_iso()}
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO replay(persona,status,cursor,speed,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(persona) DO UPDATE SET status=excluded.status, cursor=excluded.cursor, "
                "speed=excluded.speed, updated_at=excluded.updated_at",
                (persona, row["status"], row["cursor"], row["speed"], row["updated_at"]),
            )

    def state(self, persona: str) -> dict:
        row = self._row(persona)
        ticks = self.ticks(persona)
        cur = min(row["cursor"], len(ticks))
        last = ticks[cur - 1] if cur else None
        running = persona in self._tasks and not self._tasks[persona].done()
        status = row["status"] if row["status"] != "running" or running else "paused"
        return {"persona": persona, "status": status, "cursor": cur, "total": len(ticks),
                "ts": last["ts"] if last else None, "day": last["day"] if last else 0,
                "speed": self._speed.get(persona, row["speed"]), "sim_state": last["state"] if last else None}

    # ----------------------------------------------------------------- control
    async def start(self, persona: str, start_day: int | None = None, speed: float | None = None) -> dict:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona}")
        row = self._row(persona)
        running = persona in self._tasks and not self._tasks[persona].done()
        if speed:
            self._speed[persona] = max(0.2, min(60.0, float(speed)))
            self._save(persona, speed=self._speed[persona])
        if running:
            return self.state(persona)
        fresh = start_day is not None or row["status"] == "finished" or row["cursor"] >= len(self.ticks(persona))
        if fresh:
            await self.reset(persona)
            idx = max(0, min(int(start_day or 0), 27)) * TICKS_PER_DAY
            self._backfill(persona, idx)
        self._speed.setdefault(persona, row["speed"])
        self._save(persona, status="running")
        self._tasks[persona] = asyncio.create_task(self._run(persona))
        await self.hooks.on_state(persona)
        return self.state(persona)

    def set_speed(self, persona: str, speed) -> dict:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona}")
        self._speed[persona] = max(0.2, min(60.0, float(speed)))
        self._save(persona, speed=self._speed[persona])
        return self.state(persona)

    async def stop(self, persona: str) -> dict:
        task = self._tasks.pop(persona, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._row(persona)["status"] == "running":
            self._save(persona, status="paused")
        await self.hooks.on_state(persona)
        return self.state(persona)

    async def reset(self, persona: str) -> dict:
        await self.stop(persona)
        self._monitors.pop(persona, None)
        with self.db.conn() as c:
            for table in ("series", "alerts", "annotations", "feed", "replay"):
                c.execute(f"DELETE FROM {table} WHERE persona=?", (persona,))
        await self.hooks.on_reset(persona)
        return self.state(persona)

    # ------------------------------------------------------------------- loops
    def _backfill(self, persona: str, upto: int) -> None:
        """Load history before the start point instantly, so baselines exist before the first live tick."""
        mon = Monitor(persona)
        rows = []
        for t in self.ticks(persona)[:upto]:
            mon.ingest(t, silent=True)
            rows.append((persona, t["i"], t["ts"], t["state"], t["quality"], json.dumps(t["v"])))
        with self.db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO series(persona,i,ts,state,quality,v) VALUES(?,?,?,?,?,?)", rows)
        self._monitors[persona] = mon
        self._save(persona, cursor=upto)

    async def _run(self, persona: str) -> None:
        try:
            ticks = self.ticks(persona)
            mon = self.monitor(persona)
            while True:
                cursor = self._row(persona)["cursor"]
                if cursor >= len(ticks):
                    self._save(persona, status="finished")
                    await self.hooks.on_state(persona)
                    return
                t = ticks[cursor]
                with self.db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO series(persona,i,ts,state,quality,v) VALUES(?,?,?,?,?,?)",
                              (persona, t["i"], t["ts"], t["state"], t["quality"], json.dumps(t["v"])))
                self._save(persona, cursor=cursor + 1)
                cands = mon.ingest(t)
                await self.hooks.on_tick(persona, t, mon, cands)
                await asyncio.sleep(1.0 / self._speed.get(persona, 2.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("replay crashed for %s", persona)
            self._save(persona, status="paused")
