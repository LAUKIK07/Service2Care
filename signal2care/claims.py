"""Arithmetic checks for what a person says might explain a change. No model computes any of this.

Each check compares the observed change with what the person's own history says is typical in that
situation, and reports the numbers. It never says what caused anything: "consistent with" and
"larger than usual" only describe the data.
"""
from __future__ import annotations

from . import stats

HEART_FAMILY = ("heart_rate", "stress", "eda", "bp_sys", "bp_dia", "respiratory_rate", "hrv_rmssd", "skin_temp")


def _hod(ts: str) -> int:
    return int(ts[11:13])


def _median(xs):
    return stats.median(xs) if xs else None


def workout_check(ticks: list[dict], metric: str, observed_shift: float, window_start_i: int) -> dict:
    """Compare the observed shift with the usual shift in the 3 hours after a workout."""
    if metric not in HEART_FAMILY:
        return {"factor": "workout", "verdict": "not_checkable", "note": "This metric is not meaningfully affected by exercise timing."}
    ends = [t["i"] for a, t in zip(ticks, ticks[1:]) if a["state"] == "active" and t["state"] != "active"]
    ends = [i for i in ends if i < window_start_i - 12]               # workouts before the alert window
    after, other = [], []
    in_after = set()
    for e in ends:
        seg = [t for t in ticks[e + 1:e + 13] if t["state"] == "awake" and t["quality"] == "good"]
        if len(seg) >= 6:
            after.append(stats.median(t["v"][metric] for t in seg))
            in_after.update(t["i"] for t in seg)
    if len(after) < 3:
        return {"factor": "workout", "verdict": "insufficient_history", "n": len(after),
                "note": "Fewer than 3 earlier workouts in the data to compare against."}
    hours = {_hod(ticks[e + 1]["ts"]) for e in ends}
    for t in ticks[:window_start_i]:
        if t["state"] == "awake" and t["quality"] == "good" and t["i"] not in in_after and _hod(t["ts"]) in hours:
            other.append(t["v"][metric])
    if len(other) < 12:
        return {"factor": "workout", "verdict": "insufficient_history", "n": len(after)}
    typical_shift = _median(after) - _median(other)
    spread = max(stats.MAD_SCALE * stats.mad(after), stats.MIN_SCALE.get(metric, 0.1))
    z = (observed_shift - typical_shift) / spread
    near = any(window_start_i - 12 <= e <= window_start_i + 4 for e in [t["i"] for a, t in zip(ticks, ticks[1:]) if a["state"] == "active" and t["state"] != "active"])
    return {"factor": "workout", "metric": metric, "n_workouts": len(after), "typical_shift": round(typical_shift, 2),
            "observed_shift": round(observed_shift, 2), "z_vs_typical": round(z, 2),
            "workout_in_prior_3h": bool(near),
            "verdict": "within_typical_range" if abs(z) < 2 else "larger_than_typical" if z > 0 else "smaller_than_typical"}


def sleep_check(ticks: list[dict], metric: str, observed_shift: float, window_start_i: int) -> dict:
    """Does last night's sleep length predict the daytime level of this metric, and is the shift bigger than predicted?"""
    nights: dict[int, int] = {}
    for t in ticks[:window_start_i]:
        if t["state"] == "sleep":
            nights[(t["i"] + 36) // 96] = nights.get((t["i"] + 36) // 96, 0) + 1
    xs, ys = [], []
    for day, n in sorted(nights.items()):
        seg = [t["v"][metric] for t in ticks[:window_start_i] if t["day"] == day and t["state"] == "awake"
               and t["quality"] == "good" and 9 <= _hod(t["ts"]) < 21]
        if len(seg) >= 20 and n >= 12:
            xs.append(n / 4.0)
            ys.append(stats.median(seg))
    fit = stats.ols(xs, ys)
    if not fit:
        return {"factor": "poor_sleep", "verdict": "insufficient_history", "n": len(xs)}
    recent = [n for d, n in sorted(nights.items())][-1:] or [0]
    last_hours = recent[0] / 4.0
    typical_hours = stats.median(xs)
    predicted_shift = fit["slope"] * (last_hours - typical_hours)
    resid = max(fit["resid_sd"], stats.MIN_SCALE.get(metric, 0.1))
    z = (observed_shift - predicted_shift) / resid
    return {"factor": "poor_sleep", "metric": metric, "n_days": fit["n"], "slope_per_sleep_hour": round(fit["slope"], 3),
            "r": round(fit["r"], 2), "last_night_hours": round(last_hours, 2), "typical_hours": round(typical_hours, 2),
            "predicted_shift": round(predicted_shift, 2), "observed_shift": round(observed_shift, 2),
            "z_vs_predicted": round(z, 2),
            "verdict": ("weak_relationship" if abs(fit["r"]) < 0.3 else
                        "within_predicted_range" if abs(z) < 2 else "larger_than_predicted")}


def pattern_check(monitor_review: dict, direction: dict[str, int]) -> dict:
    """How many of a set of metrics currently deviate in the stated direction (+1 up, -1 down)?"""
    hits, seen = [], 0
    for m, sign in direction.items():
        o = monitor_review.get(m)
        if not o:
            continue
        seen += 1
        if o["z"] * sign >= 2.5:
            hits.append({"metric": m, "z": round(o["z"], 1)})
    return {"factor": "pattern", "metrics_in_direction": len(hits), "metrics_checked": seen, "hits": hits}


FACTOR_CHECKS = {"workout": workout_check, "poor_sleep": sleep_check}
