"""Shared pieces for the normalizers: the output schema, physiological bounds, and the quality accumulator.

Long format, one record per observation (CSV, gzip). Columns:

    source        dataset id: lifesnaps | qol_stress | wearable_stress_exercise | mmash
    participant   the dataset's own anonymized id (QoL_Stress hashes are shortened to their first 10 hex characters)
    session       sub-recording inside a participant (e.g. STRESS, AEROBIC, relax, day1); may be empty
    observed_at   ISO 8601. With an explicit "+00:00" when the source is UTC; WITHOUT an offset when the source gives
                  local wall-clock time ("local-naive"); "T12:11:50" when the source only has a time of day; empty when
                  only seconds-from-start exist
    time_basis    utc | utc-deidentified | local-naive | time-of-day-only | relative-seconds
    t_sec         seconds from the start of the session when that is meaningful (else empty)
    metric        canonical name with unit suffix, e.g. heart_rate_bpm
    value         number (empty when quality = missing)
    unit          bpm, ms, microsiemens, celsius, g, s, count, percent, brpm, ratio, unspecified ...
    quality       ok | missing | implausible | interpolated-by-source | source-flagged-invalid
    context       rest | stress | aerobic | anaerobic | sleep | unknown
    segment       finer label from the dataset (e.g. stroop, sprint-1, relax); may be empty
    label_source  where the context came from: dataset | tags+protocol | inferred-questionnaire-time | sleep-log | ""
    n_obs         number of source samples behind the value (1 for raw rows, >1 for documented aggregates)

Nothing is imputed. A row is written with quality=missing only when the source has a row whose value is blank/NaN.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
EXTRACTED = RAW / "extracted"
OUT = ROOT / "data" / "normalized"

FIELDS = ["source", "participant", "session", "observed_at", "time_basis", "t_sec", "metric", "value", "unit", "quality",
          "context", "segment", "label_source", "n_obs"]

# Physiological plausibility bounds (inclusive). They only set the `implausible` flag, nothing is deleted.
# They are my choices, wide on purpose; see docs/data.md "Bounds". unit is the canonical unit of the metric.
BOUNDS = {
    "heart_rate_bpm": (30.0, 220.0),
    "hrv_rmssd_ms": (1.0, 300.0),
    "respiratory_rate_brpm": (4.0, 40.0),
    "spo2_percent": (70.0, 100.0),
    "spo2_lower_bound_percent": (50.0, 100.0),
    "spo2_upper_bound_percent": (50.0, 100.0),
    "skin_temp_celsius": (25.0, 42.0),           # absolute wrist skin temperature
    "skin_temp_delta_celsius": (-8.0, 8.0),      # relative to the device's own baseline
    "steps_count": (0.0, 300.0),                 # per minute
    "eda_microsiemens": (0.0, 100.0),            # Empatica E4 range
    "acc_magnitude_mean_g": (0.0, 4.0),
    "acc_magnitude_sd_g": (0.0, 4.0),
    "ibi_seconds": (0.25, 2.0),                  # 30 to 240 beats/min
    "rhr_daily_bpm": (30.0, 120.0),
    "sleep_efficiency_percent": (0.0, 100.0),
}


def judge(metric: str, value: float | None) -> str:
    """quality flag for a numeric value: missing | implausible | ok."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "missing"
    b = BOUNDS.get(metric)
    if b and not (b[0] <= value <= b[1]):
        return "implausible"
    return "ok"


def fmt(v: float | None) -> str:
    if v is None:
        return ""
    if isinstance(v, int):
        return str(v)
    return f"{v:.6g}" if abs(v) < 1e6 else f"{v:.1f}"


class Sink:
    """Writes observations and accumulates every statistic reported in quality.json."""

    def __init__(self, dataset: str, folder: Path | None = None):
        self.dataset = dataset
        self.dir = (folder or OUT) / dataset
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "observations.csv.gz"
        self._tmp = self.dir / "observations.csv.gz.tmp"
        self._f = gzip.open(self._tmp, "wt", newline="", compresslevel=3)
        self._w = csv.writer(self._f)
        self._w.writerow(FIELDS)
        self.rows = 0
        self.m: dict[str, dict] = defaultdict(lambda: {
            "rows": 0, "ok": 0, "missing": 0, "implausible": 0, "interpolated-by-source": 0, "source-flagged-invalid": 0,
            "duplicates_dropped": 0, "out_of_order": 0, "vmin": None, "vmax": None, "unit": "", "nominal_period_s": None})
        self.part: dict[tuple, list] = {}        # (metric, participant, session) -> [n, first_epoch, last_epoch, days:set]
        self.last_epoch: dict[tuple, float] = {}
        self.participants: set[str] = set()
        self.notes: dict[str, list] = defaultdict(list)
        self.source_rows: dict[str, int] = defaultdict(int)     # rows read per source file/type
        self.skipped: dict[str, str] = {}                       # source part -> reason it is not normalized
        self.counts: dict[str, int] = defaultdict(int)          # free-form counters (timestamp issues etc.)

    # ------------------------------------------------------------------ write
    def add(self, participant: str, metric: str, unit: str, value, observed_at: str, time_basis: str, *, epoch: float,
            session: str = "", t_sec: float | None = None, quality: str | None = None, context: str = "unknown",
            segment: str = "", label_source: str = "", n_obs: int = 1, period_s: float | None = None,
            check_order: bool = True) -> None:
        """Write one row. `epoch` is any monotone number for ordering/coverage (UTC seconds, time-of-day seconds, ...)."""
        q = quality or judge(metric, value)
        s = self.m[metric]
        s["rows"] += 1
        s[q] += 1
        s["unit"] = unit
        if period_s:
            s["nominal_period_s"] = period_s
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            s["vmin"] = value if s["vmin"] is None else min(s["vmin"], value)
            s["vmax"] = value if s["vmax"] is None else max(s["vmax"], value)
        key = (metric, participant, session)
        if check_order:
            last = self.last_epoch.get(key)
            if last is not None and epoch < last:
                s["out_of_order"] += 1                  # went backwards in file order (exact repeats are counted via dup())
            self.last_epoch[key] = epoch if last is None else max(epoch, last)
        p = self.part.get(key)
        if p is None:
            self.part[key] = [1, epoch, epoch, {int(epoch // 86400)}]
        else:
            p[0] += 1
            p[1] = min(p[1], epoch)
            p[2] = max(p[2], epoch)
            p[3].add(int(epoch // 86400))
        self.participants.add(participant)
        self._w.writerow([self.dataset, participant, session, observed_at, time_basis, fmt(t_sec) if t_sec is not None else "",
                          metric, fmt(value), unit, q, context, segment, label_source, n_obs])
        self.rows += 1

    def dup(self, metric: str, n: int = 1) -> None:
        self.m[metric]["duplicates_dropped"] += n

    # ----------------------------------------------------------------- finish
    def close(self) -> None:
        self._f.close()
        self._tmp.replace(self.path)

    def quality(self, extra: dict | None = None) -> dict:
        per_metric = {}
        for metric, s in sorted(self.m.items()):
            parts = [(k, v) for k, v in self.part.items() if k[0] == metric]
            n_part = len({k[1] for k, _ in parts})
            period = s["nominal_period_s"]
            expected = sum(((v[2] - v[1]) / period + 1) for _, v in parts) if period else None
            observed_for_cov = sum(v[0] for _, v in parts)
            days_present = sum(len(v[3]) for _, v in parts)
            days_span = sum(int(v[2] // 86400) - int(v[1] // 86400) + 1 for _, v in parts)
            rows = s["rows"] or 1
            per_metric[metric] = {
                "unit": s["unit"], "rows": s["rows"], "participants": n_part,
                "pct_ok": round(100 * s["ok"] / rows, 3), "pct_missing": round(100 * s["missing"] / rows, 3),
                "pct_implausible": round(100 * s["implausible"] / rows, 3),
                "n_ok": s["ok"], "n_missing": s["missing"], "n_implausible": s["implausible"],
                "n_interpolated_by_source": s["interpolated-by-source"], "n_source_flagged_invalid": s["source-flagged-invalid"],
                "duplicates_dropped": s["duplicates_dropped"], "out_of_order_rows": s["out_of_order"],
                "value_min": s["vmin"], "value_max": s["vmax"], "nominal_period_s": period,
                "coverage_pct_of_span": round(min(100.0, 100 * observed_for_cov / expected), 2) if expected else None,
                "days_with_data_pct_of_span": round(100 * days_present / days_span, 2) if days_span else None,
            }
        out = {"dataset": self.dataset, "rows_out": self.rows, "participants": len(self.participants),
               "source_rows_read": dict(self.source_rows), "not_normalized": self.skipped,
               "counters": dict(self.counts), "notes": {k: v[:20] for k, v in self.notes.items()},
               "metrics": per_metric}
        if extra:
            out.update(extra)
        return out


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=False, default=str) + "\n")


def open_text(path: Path):
    """Open a text file that may carry a UTF-8 BOM (some PhysioNet CSVs do)."""
    return open(path, newline="", encoding="utf-8-sig")
