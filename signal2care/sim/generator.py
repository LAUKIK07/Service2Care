"""Deterministic generator: one tick every 15 minutes for 28 days, all channels, with a sleep/awake/active state.

Realism comes from structure, not from a language model: circadian rhythm, sleep stages by state,
exercise bouts, day-to-day random effects, autocorrelated noise, couplings (HR up means HRV down).
Same persona and seed always give the same data.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from .personas import DAYS, METRICS, START_DATE, TICK_MINUTES, Persona

TICKS_PER_DAY = 24 * 60 // TICK_MINUTES
_START = datetime.fromisoformat(START_DATE)


def _ramp(t: float, start: float, end: float, edge: float = 2.0) -> float:
    """1 inside the window, easing in and out over `edge` hours, 0 outside."""
    if t < start or t >= end:
        return 0.0
    return max(0.0, min(1.0, (t - start) / edge, (end - t) / edge))


def _sleep_windows(p: Persona, rng: random.Random) -> list[tuple[float, float]]:
    """Absolute-hour sleep windows, one per night (night d starts on day d and ends on day d+1)."""
    windows = []
    for d in range(-1, DAYS + 1):
        bed = p.bed + rng.gauss(0, p.bed_sd / 60)
        wake = p.wake + rng.gauss(0, 0.2)
        for s in p.scenarios:
            if "bed_shift" in s.effects and s.day * 24 + s.hour <= d * 24 + 12 < s.day * 24 + s.hour + s.hours:
                bed += s.effects["bed_shift"][1]
        start = d * 24 + bed
        end = (d + 1) * 24 + wake
        if end - start > 3:
            windows.append((start, end))
    return windows


def _workout_windows(p: Persona) -> list[tuple[float, float, float]]:
    out = []
    for d in range(DAYS):
        weekday = (_START + timedelta(days=d)).weekday()
        for wd, hour, minutes, intensity in p.workouts:
            if wd == weekday:
                out.append((d * 24 + hour, d * 24 + hour + minutes / 60, intensity))
    return out


def generate(p: Persona) -> list[dict]:
    rng = random.Random(p.seed)
    sleep = _sleep_windows(p, rng)
    work = _workout_windows(p)
    day_fx = {d: {"hr": rng.gauss(0, 1.4), "hrv": rng.gauss(0, 0.07), "temp": rng.gauss(0, 0.12),
                  "bp": rng.gauss(0, 3.0)} for d in range(-1, DAYS + 1)}
    ar = {k: 0.0 for k in ("hr", "hrv", "resp", "spo2", "temp", "bp", "eda")}
    ticks: list[dict] = []
    stress_prev = 3.0
    for i in range(DAYS * TICKS_PER_DAY):
        t = i * TICK_MINUTES / 60.0                      # absolute hours since day 0, 00:00
        day, hod = divmod(t, 24)
        day = int(day)
        fx = day_fx[day]
        state, intensity = "awake", 0.0
        if any(a <= t < b for a, b in sleep):
            state = "sleep"
            if rng.random() < 0.03:                      # brief awakening
                state = "awake"
        else:
            for a, b, inten in work:
                if a <= t < b:
                    state, intensity = "active", inten
                    break
        circ = math.sin((hod - 9) / 24 * 2 * math.pi)    # peaks mid-afternoon
        for m, phi, sigma in (("hr", .8, 1.6), ("hrv", .8, .06), ("resp", .6, .35), ("spo2", .5, .35),
                              ("temp", .9, .06), ("bp", .7, 2.0), ("eda", .7, .25)):
            ar[m] = phi * ar[m] + rng.gauss(0, sigma)
        if state == "sleep":
            hr = p.sleep_hr + 2.0 * (circ + 1) / 2 + fx["hr"] + ar["hr"]
            hrv = p.hrv * (1 + fx["hrv"]) * math.exp(ar["hrv"])
            resp = p.resp + ar["resp"]
            spo2 = p.spo2 - 0.6 + ar["spo2"] * 0.7
            temp = p.temp + 0.35 + fx["temp"] + ar["temp"]
            sys_, dia = p.bp_sys - 12 + fx["bp"] + ar["bp"], p.bp_dia - 9 + fx["bp"] * 0.6 + ar["bp"] * 0.6
            eda, steps, stress = 0.8 + ar["eda"] * 0.3, 0.0, 1.0
        else:
            act = 0.0
            if state == "active":
                within = 1.0
                act = intensity * within
            hr = (p.awake_hr + 4.0 * circ + fx["hr"] + ar["hr"] * 1.4 + 55 * act)
            hrv = p.hrv * (0.55 if act == 0 else 0.3) * (1 + fx["hrv"]) * math.exp(ar["hrv"])
            resp = p.resp + 1.8 + 9 * act + ar["resp"]
            spo2 = p.spo2 + ar["spo2"] * 0.6 - 0.3 * act
            temp = p.temp - 0.15 * circ + fx["temp"] + ar["temp"] - 0.2 * act
            sys_ = p.bp_sys + 5 * circ + fx["bp"] + ar["bp"] + 38 * act
            dia = p.bp_dia + 3 * circ + fx["bp"] * 0.6 + ar["bp"] * 0.6 + 8 * act
            eda = 2.0 + 0.9 * circ + ar["eda"] + 7 * act
            steps = max(0.0, rng.gauss(55, 35)) if act == 0 else max(0.0, rng.gauss(1100 * act, 150))
            if 0 <= hod < 6:
                steps *= 0.15
            hr_dev = hr - (p.awake_hr + 4.0 * circ)
            stress = max(0.0, min(10.0, 3.2 + 0.12 * hr_dev - 0.5 * math.log(max(hrv, 1) / (p.hrv * 0.55))))
            stress = 0.6 * stress_prev + 0.4 * stress
            stress_prev = stress
        v = {"heart_rate": hr, "hrv_rmssd": hrv, "respiratory_rate": resp, "spo2": spo2, "skin_temp": temp,
             "steps": steps, "stress": stress, "bp_sys": sys_, "bp_dia": dia, "eda": eda}
        quality = "good"
        for s in p.scenarios:
            w = _ramp(t, s.day * 24 + s.hour, s.day * 24 + s.hour + s.hours, edge=min(2.0, s.hours / 4))
            if w <= 0:
                continue
            if "dropout" in s.effects:
                quality = "dropout"
                v["heart_rate"], v["spo2"] = rng.uniform(25, 40), rng.uniform(70, 80)
                continue
            for metric, (op, x) in s.effects.items():
                if metric not in v:
                    continue
                if op == "add":
                    v[metric] += x * w * (0.6 if state == "sleep" and metric in ("bp_sys", "bp_dia") else 1.0)
                else:
                    v[metric] *= 1 + (x - 1) * w
        v["spo2"] = min(100.0, max(80.0, v["spo2"]))
        v["stress"] = min(10.0, max(0.0, v["stress"]))
        ticks.append({
            "i": i,
            "ts": (_START + timedelta(minutes=i * TICK_MINUTES)).isoformat(timespec="minutes"),
            "day": day,
            "state": state,
            "quality": quality,
            "v": {k: round(x, 1 if k not in ("skin_temp", "eda", "stress") else 2) for k, x in v.items()},
        })
    return ticks
