"""Robust statistics used by the monitor. Plain code: no model computes any of these numbers."""
from __future__ import annotations

import math
import statistics
from typing import Iterable

MAD_SCALE = 1.4826  # makes MAD comparable to a standard deviation for roughly normal data

# Smallest scale (in the metric's own unit) treated as meaningful, so a very quiet baseline cannot
# turn a trivial wobble into a huge z-score.
MIN_SCALE = {"heart_rate": 1.5, "hrv_rmssd": 3.0, "respiratory_rate": 0.4, "spo2": 0.5, "skin_temp": 0.12,
             "stress": 0.6, "bp_sys": 3.5, "bp_dia": 2.5, "eda": 0.4, "steps": 20.0,
             "sleep_hours": 0.8, "rhr_sleep": 2.5, "hrv_sleep": 7.0, "sleep_efficiency": 0.03}


def median(xs: Iterable[float]) -> float:
    return statistics.median(list(xs))


def mad(xs: list[float], med: float | None = None) -> float:
    med = median(xs) if med is None else med
    return statistics.median(abs(x - med) for x in xs)


def robust_baseline(xs: list[float], metric: str) -> tuple[float, float]:
    """(median, scale) where scale is the MAD-based spread with a floor for the metric."""
    m = median(xs)
    return m, max(MAD_SCALE * mad(xs, m), MIN_SCALE.get(metric, 0.1))


def robust_z(x: float, med: float, scale: float) -> float:
    return (x - med) / scale


def percentile_rank(x: float, xs: list[float]) -> float:
    """Share of historical values at or below x, in percent."""
    if not xs:
        return float("nan")
    return 100.0 * sum(1 for v in xs if v <= x) / len(xs)


def ols(xs: list[float], ys: list[float]) -> dict | None:
    """Simple least-squares fit y = a + b x. Returns slope, intercept, r, n, residual spread, or None."""
    n = len(xs)
    if n < 6:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    r = sxy / math.sqrt(sxx * syy)
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    spread = math.sqrt(sum(e * e for e in resid) / max(1, n - 2))
    return {"slope": b, "intercept": a, "r": r, "n": n, "resid_sd": spread}
