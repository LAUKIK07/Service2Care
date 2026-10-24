"""Always-on monitor for one persona's stream.

For every completed hour it compares each metric with the person's own history in the same context
(sleep, or awake in the same part of the day). The spread used for the comparison includes the
day-to-day variation, not just hour-to-hour noise, so a naturally "high day" does not look like an
event. It tracks how long a deviation persists, and when a deviation is both large and sustained it
emits a Candidate with all the numbers. After each sleep it does the same for nightly summaries, and
it reports when the device stops sending usable data. Detection only; whether to message anyone is
decided elsewhere.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

from . import stats
from .sim.personas import METRICS

DETECT = tuple(m for m in METRICS if m != "steps")
HIST_HOURS = 14 * 24
MIN_HIST = 24            # hourly values needed in a context
MIN_DAYS = 5             # distinct days needed in a context (warm-up)
Z_KEEP = 2.5             # deviation level that keeps a streak alive
# (min |z|, min consecutive hours): any rule that matches fires
ONSET_RULES = ((4.5, 3), (5.0, 2), (7.0, 1))   # measured on 60 clean runs: 0.17 false candidates per 28 days
ONGOING_EVERY_H = 6      # re-emit a continuing deviation this often (simulated hours)
DAILY_MIN_HIST = 10
DAILY_Z = 3.5
DAILY = ("sleep_hours", "rhr_sleep", "hrv_sleep")
GAP_HOURS = 2


def bucket(hour: int) -> str:
    return "night" if hour < 6 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"


@dataclass
class Candidate:
    persona: str
    kind: str                 # "hourly", "daily" or "gap"
    metric: str
    ts: str                   # simulated time of detection
    state: str
    value: float
    baseline: float
    scale: float
    z: float
    pct: float
    persistence_h: int
    n_hist: int
    quality: str
    started: str
    ongoing: bool = False
    co: list = field(default_factory=list)   # other metrics deviating at the same time

    def to_dict(self) -> dict:
        return asdict(self)


class Monitor:
    def __init__(self, persona: str):
        self.persona = persona
        self.hist: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=HIST_HOURS))   # (day, value)
        self.streak: dict[str, dict] = {}
        self.hour: list[dict] = []
        self.sleep: list[dict] = []
        self.daily_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=21))
        self.gap_hours = 0
        self.last_night: dict | None = None
        self.last_review: dict[str, dict] = {}            # metric -> latest hourly observation, for the dashboard

    # ------------------------------------------------------------------ public
    def ingest(self, tick: dict, *, silent: bool = False) -> list[Candidate]:
        out: list[Candidate] = []
        self.hour.append(tick)
        self._track_sleep(tick, out, silent)
        if tick["ts"][14:16] == "45":                    # the 15-minute tick that closes the hour
            out += self._close_hour(silent)
            self.hour = []
        return [] if silent else out

    def baseline(self, metric: str, state: str, hour: int) -> dict | None:
        return self._baseline((metric, state, "sleep" if state == "sleep" else bucket(hour)))

    # -------------------------------------------------------------- baselines
    def _baseline(self, key: tuple) -> dict | None:
        rows = list(self.hist.get(key, []))
        days = defaultdict(list)
        for d, v in rows:
            days[d].append(v)
        if len(rows) < MIN_HIST or len(days) < MIN_DAYS:
            return None
        values = [v for _, v in rows]
        med = stats.median(values)
        within = stats.MAD_SCALE * stats.mad(values, med)
        daily = [stats.median(v) for v in days.values()]
        between = stats.MAD_SCALE * stats.mad(daily)
        scale = max(within, between, stats.MIN_SCALE.get(key[0], 0.1))
        return {"median": med, "scale": scale, "n": len(rows), "days": len(days)}

    # ----------------------------------------------------------------- hourly
    def _close_hour(self, silent: bool) -> list[Candidate]:
        ticks = [t for t in self.hour if t["quality"] == "good"]
        end_ts = self.hour[-1]["ts"]
        if len(ticks) < 3:
            return self._gap(end_ts, silent)
        self.gap_hours = 0
        states = [t["state"] for t in ticks]
        state = max(set(states), key=states.count)
        if states.count(state) / len(ticks) < 0.6:
            return []                                      # mixed hour: not comparable
        hod = int(self.hour[0]["ts"][11:13])
        day = self.hour[0]["day"]
        quality = f"{len(ticks)}/{len(self.hour)} good samples"
        observed: dict[str, dict] = {}
        for m in DETECT:
            seen = [t["v"][m] for t in ticks if t["v"].get(m) is not None]
            if len(seen) < 3:
                continue                                    # this device does not report the channel, or not enough of it this hour
            value = stats.median(seen)
            key = (m, state, "sleep" if state == "sleep" else bucket(hod))
            base = self._baseline(key) if state != "active" else None
            if base and not (state == "sleep" and m in ("stress", "eda")):
                z = stats.robust_z(value, base["median"], base["scale"])
                observed[m] = {"value": value, "median": base["median"], "scale": base["scale"], "z": z, "n": base["n"]}
            self.hist[key].append((day, value))
        self.last_review = {m: {**o, "ts": end_ts, "state": state} for m, o in observed.items()}
        if state == "active":
            self.streak.clear()
            return []
        self._advance_streaks(observed, end_ts)
        return [] if silent else self._candidates(observed, state, end_ts, quality)

    def _gap(self, ts: str, silent: bool) -> list[Candidate]:
        self.gap_hours += 1
        if self.gap_hours == GAP_HOURS and not silent:
            return [Candidate(persona=self.persona, kind="gap", metric="data_quality", ts=ts, state="unknown",
                              value=0.0, baseline=1.0, scale=1.0, z=-9.0, pct=-100.0, persistence_h=self.gap_hours,
                              n_hist=0, quality="no usable samples", started=ts)]
        return []

    def _advance_streaks(self, observed: dict, ts: str) -> None:
        for m in DETECT:
            o = observed.get(m)
            s = self.streak.get(m)
            if o and abs(o["z"]) >= Z_KEEP:
                sign = 1 if o["z"] > 0 else -1
                if s and s["sign"] == sign:
                    s["n"] += 1
                else:
                    self.streak[m] = {"n": 1, "sign": sign, "started": ts, "last_emit": None}
            else:
                self.streak.pop(m, None)

    def _candidates(self, observed: dict, state: str, ts: str, quality: str) -> list[Candidate]:
        firing = []
        for m, o in observed.items():
            s = self.streak.get(m)
            if not s or not any(abs(o["z"]) >= z and s["n"] >= n for z, n in ONSET_RULES):
                continue
            first = s["last_emit"] is None
            if not first and s["n"] - s["last_emit"] < ONGOING_EVERY_H:
                continue
            firing.append((m, o, s, first))
        if not firing:
            return []
        firing.sort(key=lambda f: abs(f[1]["z"]), reverse=True)
        m, o, s, first = firing[0]
        for _, _, st, _ in firing:
            st["last_emit"] = st["n"]
        co = [{"metric": mm, "z": round(oo["z"], 1), "value": round(oo["value"], 2)} for mm, oo, _, _ in firing[1:4]]
        return [Candidate(
            persona=self.persona, kind="hourly", metric=m, ts=ts, state=state, value=round(o["value"], 2),
            baseline=round(o["median"], 2), scale=round(o["scale"], 3), z=round(o["z"], 2),
            pct=round(100 * (o["value"] - o["median"]) / o["median"], 1) if o["median"] else 0.0,
            persistence_h=s["n"], n_hist=o["n"], quality=quality, started=s["started"], ongoing=not first, co=co,
        )]

    # ------------------------------------------------------------------ nights
    def _track_sleep(self, tick: dict, out: list, silent: bool) -> None:
        if tick["state"] == "sleep":
            self.sleep.append(tick)
            return
        if not self.sleep:
            return
        if tick["state"] == "awake":
            self.sleep.append(tick)                         # a brief awakening inside the night
            tail = 0
            for t in reversed(self.sleep):
                if t["state"] == "sleep":
                    break
                tail += 1
            if tail < 3:
                return
        night = [t for t in self.sleep if t["state"] == "sleep"]
        span = len(self.sleep)
        self.sleep = []
        if len(night) < 12:
            return                                          # a nap or a fragment
        out += self._close_night(night, span, tick["ts"], silent)

    def _close_night(self, night: list[dict], span: int, ts: str, silent: bool) -> list[Candidate]:
        good = [t for t in night if t["quality"] == "good"] or night
        hr = sorted(t["v"]["heart_rate"] for t in good if t["v"].get("heart_rate") is not None)
        hrv = [t["v"]["hrv_rmssd"] for t in good if t["v"].get("hrv_rmssd") is not None]
        values = {"sleep_hours": len(night) / 4.0}
        if len(hr) >= 6:
            values["rhr_sleep"] = hr[max(0, int(len(hr) * 0.1))]
        if len(hrv) >= 6:
            values["hrv_sleep"] = stats.median(hrv)
        found: list[Candidate] = []
        for m in DAILY:
            if m not in values:
                continue
            past = list(self.daily_hist[m])
            if len(past) >= DAILY_MIN_HIST:
                med, scale = stats.robust_baseline(past, m)
                z = stats.robust_z(values[m], med, scale)
                if abs(z) >= DAILY_Z and not silent:
                    found.append(Candidate(
                        persona=self.persona, kind="daily", metric=m, ts=ts, state="sleep",
                        value=round(values[m], 2), baseline=round(med, 2), scale=round(scale, 3), z=round(z, 2),
                        pct=round(100 * (values[m] - med) / med, 1) if med else 0.0, persistence_h=1,
                        n_hist=len(past), quality=f"{len(night)} sleep samples", started=ts))
            self.daily_hist[m].append(values[m])
        self.last_night = {**{k: round(v, 2) for k, v in values.items()}, "span_h": span / 4.0, "ended": ts}
        if len(found) > 1:
            found.sort(key=lambda c: abs(c.z), reverse=True)
            top = found[0]
            top.co = [{"metric": c.metric, "z": c.z, "value": c.value} for c in found[1:]]
            return [top]
        return found
