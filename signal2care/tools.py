"""Tools the chat model may call. Every number comes from code reading the stored series, never from the model.

Only the person linked to a simulated wearable gets these tools. Everyone else chats without them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import httpx

from . import research, stats
from .alerts import FAMILY, LABEL, UNIT, AlertEngine
from .replay import Replay
from .sim.personas import METRICS

log = logging.getLogger("s2c.tools")

SPECS = [
    {"type": "function", "function": {
        "name": "get_metric_summary",
        "description": "Read the person's (synthetic) wearable data for one metric over the last N simulated hours: latest value, "
                       "window average, range, and how it compares with their own usual level in the same context.",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string", "enum": list(METRICS)},
            "hours": {"type": "integer", "minimum": 1, "maximum": 72, "description": "Window length, default 6"}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "list_alerts",
        "description": "List the most recent wearable alerts sent to this person, with what they said about each and any checks.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 8}}}}},
    {"type": "function", "function": {
        "name": "check_claim",
        "description": "Test, with arithmetic on the person's own history, whether something they mentioned (a workout, a bad night's "
                       "sleep) is consistent with the size of the change in an alert. It never proves a cause.",
        "parameters": {"type": "object", "properties": {
            "factor": {"type": "string", "enum": ["workout", "poor_sleep"]},
            "alert_id": {"type": "integer", "description": "Optional. Defaults to the alert being discussed, else the latest."}},
            "required": ["factor"]}}},
    {"type": "function", "function": {
        "name": "search_literature",
        "description": "Search PubMed for published studies on a general topic (for example 'HRV and alcohol'). Returns titles, "
                       "years, links and short abstract excerpts. Use it for general background only, never to explain this person's data.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]


class ToolBox:
    def __init__(self, replay: Replay, alerts: AlertEngine, http: httpx.AsyncClient):
        self.replay = replay
        self.alerts = alerts
        self.http = http

    def specs_for(self, handle: str) -> list[dict] | None:
        persona = self.alerts.persona_for(handle)
        if not persona or self.replay.state(persona)["cursor"] == 0:
            return None
        return SPECS

    async def run(self, handle: str, name: str, args: dict) -> dict:
        persona = self.alerts.persona_for(handle)
        if not persona:
            return {"error": "no wearable is linked to this person"}
        if name == "get_metric_summary":
            return self._summary(persona, str(args.get("metric", "")), int(args.get("hours") or 6))
        if name == "list_alerts":
            return self._list(persona, int(args.get("limit") or 5))
        if name == "check_claim":
            return self._check(persona, handle, str(args.get("factor", "")), args.get("alert_id"))
        if name == "search_literature":
            try:
                papers = await research.search_pubmed(self.http, str(args.get("query", "")))
            except httpx.HTTPError as exc:
                return {"error": f"PubMed unreachable ({type(exc).__name__})"}
            return {"source": "PubMed", "note": "General literature only. Not about this person.", "papers": papers}
        return {"error": f"unknown tool {name}"}

    # ----------------------------------------------------------------- readers
    def _ticks(self, persona: str) -> list[dict]:
        return self.replay.ticks(persona)[: self.replay.state(persona)["cursor"]]

    def _summary(self, persona: str, metric: str, hours: int) -> dict:
        if metric not in METRICS:
            return {"error": f"unknown metric {metric}", "metrics": list(METRICS)}
        ticks = self._ticks(persona)
        if not ticks:
            return {"error": "no data yet"}
        hours = max(1, min(hours, 72))
        window = [t for t in ticks[-hours * 4:] if t["quality"] == "good"]
        if not window:
            return {"metric": LABEL[metric], "error": "no usable samples in that window (tracker data gap)"}
        vals = [t["v"][metric] for t in window]
        last = window[-1]
        out = {"metric": LABEL[metric], "unit": UNIT[metric], "synthetic_data": True, "simulated_time": last["ts"],
               "state_now": last["state"], "latest": round(last["v"][metric], 2), "window_hours": hours,
               "window_mean": round(sum(vals) / len(vals), 2), "window_min": round(min(vals), 2),
               "window_max": round(max(vals), 2), "samples": len(vals)}
        base = self.replay.monitor(persona).baseline(metric, last["state"], int(last["ts"][11:13]))
        if base:
            z = stats.robust_z(stats.median(vals[-4:]), base["median"], base["scale"])
            out["usual_for_this_context"] = round(base["median"], 2)
            out["difference_pct"] = round(100 * (stats.median(vals[-4:]) - base["median"]) / base["median"], 1) if base["median"] else None
            out["robust_z_last_hour"] = round(z, 2)
            out["baseline_days"] = base["days"]
        else:
            out["usual_for_this_context"] = None
            out["note"] = "Not enough history in this context yet to say what is usual."
        return out

    def _list(self, persona: str, limit: int) -> dict:
        with self.alerts.db.conn() as c:
            rows = c.execute("SELECT a.*, n.summary AS note, n.factors AS factors, n.computed AS computed FROM alerts a "
                             "LEFT JOIN annotations n ON n.alert_id=a.id WHERE a.persona=? AND a.status IN "
                             "('sent','discussed','resolved','dry_run') ORDER BY a.id DESC LIMIT ?",
                             (persona, max(1, min(limit, 8)))).fetchall()
        return {"alerts": [{
            "id": r["id"], "simulated_time": r["ts"], "metric": LABEL.get(r["metric"], r["metric"]), "status": r["status"],
            "message_sent": r["text"], "person_said": r["note"], "factors": json.loads(r["factors"] or "[]"),
            "checks": json.loads(r["computed"] or "[]")} for r in rows]}

    def _check(self, persona: str, handle: str, factor: str, alert_id) -> dict:
        if factor not in ("workout", "poor_sleep"):
            return {"error": "factor must be workout or poor_sleep"}
        with self.alerts.db.conn() as c:
            if alert_id:
                row = c.execute("SELECT * FROM alerts WHERE id=? AND persona=?", (int(alert_id), persona)).fetchone()
            else:
                open_ = self.alerts.open_for(handle)
                row = open_ or c.execute("SELECT * FROM alerts WHERE persona=? AND kind='hourly' ORDER BY id DESC LIMIT 1",
                                         (persona,)).fetchone()
        if not row:
            return {"error": "no alert to check against"}
        ev = json.loads(row["evidence"])
        if ev.get("kind") != "hourly":
            return {"error": "this check only applies to hourly changes, not nightly summaries or data gaps"}
        results = self.alerts.check_factors(persona, ev, [factor])
        return {"alert_id": row["id"], "metric": LABEL.get(ev["metric"], ev["metric"]), "result": results,
                "reminder": "These numbers describe the data. They do not show what caused the change."}
