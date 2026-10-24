"""Real-data replay personas built from the public LifeSnaps dataset (Zenodo 7229547 v4, CC-BY-4.0, Fitbit Sense).

Contract used by signal2care/registry.py:
    available() -> {persona_id: {"name","tag","blurb","source","license","citation","channels":[...]}}
    ticks(persona_id) -> 2688 dicts (28 days x 96 fifteen-minute ticks), same shape as sim.generator.generate()
    episodes() -> labeled real short segments (QoL_Stress, Wearable Stress & Exercise) from data/normalized/

Source files (only these two CSVs of LifeSnaps are read; the 9.7 GB fitbit.bson is never touched):
    data/raw/extracted/lifesnaps/rais_anonymized/csv_rais_anonymized/hourly_fitbit_sema_df_unprocessed.csv
    data/raw/extracted/lifesnaps/rais_anonymized/csv_rais_anonymized/daily_fitbit_sema_df_unprocessed.csv
Everything the code decides is documented in docs/data.md ("Replay personas"). Short version:
  * HOURLY source: the hour's mean bpm is carried across its 4 ticks, "res": "hourly". Steps in an hour are split evenly over
    the 4 ticks (so the hour's total is preserved). Nothing is interpolated: an hour with no value has no key in `v`.
  * Nightly values (RMSSD, breathing rate, SpO2 average) are ONE number per night; they are carried across that night's inferred
    sleep ticks and are absent otherwise. They are not independent hourly measurements (see docs/data.md).
  * Wrist temperature in the hourly file is a deviation from the device baseline, so it is exposed as `skin_temp_delta`
    (not `skin_temp`). The monitor ignores it.
  * The dataset has NO sleep start/end times in these CSVs. `state` is therefore INFERRED, see _infer_sleep() and _state().
  * Real calendar dates are replaced by the demo timeline starting 2026-10-01; `src_ts` keeps the real local-naive hour.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "extracted" / "lifesnaps" / "rais_anonymized" / "csv_rais_anonymized"
HOURLY = RAW_DIR / "hourly_fitbit_sema_df_unprocessed.csv"
DAILY = RAW_DIR / "daily_fitbit_sema_df_unprocessed.csv"
NORM = ROOT / "data" / "normalized"
CACHE = NORM / "realdata"
START = datetime(2026, 10, 1)
DAYS, TICKS_PER_DAY = 28, 96

# Plausibility bounds (same numbers as scripts/data_common.BOUNDS). Values outside are not placed in `v`; counted in the build report.
BOUNDS = {"heart_rate": (30.0, 220.0), "hrv_rmssd": (1.0, 300.0), "respiratory_rate": (4.0, 40.0), "spo2": (70.0, 100.0),
          "skin_temp_delta": (-8.0, 8.0), "steps": (0.0, 20000.0)}

# State rule thresholds (documented in docs/data.md)
ACTIVE_STEPS_PER_HOUR = 1500        # about 25 steps/min sustained over the hour
ACTIVE_BPM = 115.0                  # hourly mean bpm this high counts as active
SLEEP_SEARCH_FROM_H, SLEEP_SEARCH_TO_H = 19, 13   # bed window searched from 19:00 the evening before to 13:00 on the wake date
MIN_HR_COVERAGE = 0.95              # persona candidates need >= 95% of the 672 hours of the window to have a bpm value

SOURCE = "LifeSnaps (Fitbit Sense), Zenodo 7229547 v4"
LICENSE = "CC BY 4.0 (Zenodo record metadata, verified 2026-10-24; see docs/data.md)"
CITATION = ("Yfantidou S, Karagianni C, Efstathiou S, Vakali A, Palotti J, Giakatos DP, Marchioro T, Kazlouski A, Ferrari E, Girdzijauskas S. "
            "LifeSnaps, a 4-month multi-modal dataset capturing unobtrusive snapshots of our lives in the wild. Sci Data 9, 663 (2022). "
            "doi:10.1038/s41597-022-01764-x. Data: doi:10.5281/zenodo.7229547")
IDS = ("ls_a", "ls_b", "ls_c", "ls_d")
LETTERS = {"ls_a": "A", "ls_b": "B", "ls_c": "C", "ls_d": "D"}


def _num(s: str | None) -> float | None:
    if s is None or s.strip() == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if math.isnan(v) else v


def _ok(channel: str, v: float | None) -> bool:
    b = BOUNDS[channel]
    return v is not None and b[0] <= v <= b[1]


# ------------------------------------------------------------------------------------------------ load
def _load():
    """-> hourly[pid][(date, hour)] = row dict, daily[pid][date] = row dict, report of duplicates."""
    hourly: dict[str, dict] = defaultdict(dict)
    daily: dict[str, dict] = defaultdict(dict)
    dup = 0
    with open(HOURLY, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (date.fromisoformat(r["date"]), int(float(r["hour"])))
            if key in hourly[r["id"]]:
                dup += 1                                    # keep the first occurrence; counted
                continue
            hourly[r["id"]][key] = {"bpm": _num(r["bpm"]), "steps": _num(r["steps"]), "temp": _num(r["temperature"]),
                                    "activity": (r["activityType"] or "").strip()}
    with open(DAILY, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            daily[r["id"]][date.fromisoformat(r["date"])] = {
                "rmssd": _num(r["rmssd"]), "resp": _num(r["full_sleep_breathing_rate"]), "spo2": _num(r["spo2"]),
                "sleep_ms": _num(r["sleep_duration"]), "asleep_min": _num(r["minutesAsleep"])}
    return hourly, daily, dup


def _window_stats(h: dict, d: dict, start: date) -> dict:
    n_hr = n_steps = n_temp = 0
    for k in range(DAYS):
        day = start + timedelta(days=k)
        for hr in range(24):
            row = h.get((day, hr))
            if row:
                n_hr += row["bpm"] is not None and _ok("heart_rate", row["bpm"])
                n_steps += row["steps"] is not None
                n_temp += row["temp"] is not None and _ok("skin_temp_delta", row["temp"])
    nights = {m: 0 for m in ("rmssd", "resp", "spo2", "sleep_ms")}
    for k in range(DAYS):
        row = d.get(start + timedelta(days=k))
        if row:
            for m in nights:
                ch = {"rmssd": "hrv_rmssd", "resp": "respiratory_rate", "spo2": "spo2"}.get(m)
                v = row[m]
                nights[m] += v is not None and (ch is None or _ok(ch, v))
    return {"hr_hours": n_hr, "hr_cov": n_hr / (DAYS * 24), "steps_hours": n_steps, "temp_hours": n_temp,
            "nights_rmssd": nights["rmssd"], "nights_resp": nights["resp"], "nights_spo2": nights["spo2"], "nights_sleep_duration": nights["sleep_ms"]}


def _select(hourly, daily) -> list[dict]:
    """Best 28-day window per participant, ranked: HR coverage must be >= 95%, then total nightly-channel nights (rmssd+resp+spo2)."""
    best = []
    for pid, h in hourly.items():
        days = sorted({k[0] for k in h})
        top = None
        for start in days:
            if start + timedelta(days=DAYS - 1) > days[-1]:
                break
            s = _window_stats(h, daily.get(pid, {}), start)
            if s["hr_cov"] < MIN_HR_COVERAGE:
                continue
            score = s["nights_rmssd"] + s["nights_resp"] + s["nights_spo2"] + s["hr_cov"]
            if top is None or score > top[0]:
                top = (score, start, s)
        if top:
            best.append({"pid": pid, "start": top[1].isoformat(), "score": round(top[0], 3), **top[2]})
    best.sort(key=lambda b: (-b["score"], b["pid"]))
    return best


# ------------------------------------------------------------------------------------------------ state
def _infer_sleep(h: dict, d: dict, start: date) -> dict[tuple[date, int], date]:
    """Hour -> wake-date for hours inferred as sleep.

    For each wake date D in the window: length L = round(time in bed from the daily row `sleep_duration`, ms -> h) when the row exists
    and 3 <= L <= 14, else the participant's median L (flag kept). The L-hour block that starts between 19:00 the evening before and
    (13:00 - L) on D with the lowest cost is the night. cost per hour = steps/50 (missing steps = 0 when bpm exists) +
    max(0, bpm - participant 10th percentile bpm)/5; an hour without bpm costs 1.0. Ties: closest to a 23:00 start.
    """
    bpms = sorted(r["bpm"] for r in h.values() if r["bpm"] is not None and _ok("heart_rate", r["bpm"]))
    ref = bpms[len(bpms) // 10] if bpms else 55.0
    lens = [round(r["sleep_ms"] / 3.6e6) for r in d.values() if r["sleep_ms"] and 3 <= round(r["sleep_ms"] / 3.6e6) <= 14]
    med_len = int(statistics.median(lens)) if lens else 8

    def cost(day: date, hr: int) -> float:
        row = h.get((day, hr))
        if not row or row["bpm"] is None:
            return 1.0
        return (row["steps"] or 0.0) / 50.0 + max(0.0, row["bpm"] - ref) / 5.0

    out: dict[tuple[date, int], date] = {}
    meta = {"from_daily_duration": 0, "median_fallback": 0}
    for k in range(-1, DAYS + 1):
        D = start + timedelta(days=k)
        row = d.get(D)
        L = round(row["sleep_ms"] / 3.6e6) if row and row["sleep_ms"] and 3 <= round(row["sleep_ms"] / 3.6e6) <= 14 else None
        meta["from_daily_duration" if L else "median_fallback"] += 1
        L = L or med_len
        best = None
        for s_abs in range(SLEEP_SEARCH_FROM_H - 24, SLEEP_SEARCH_TO_H - L + 1):       # hour offsets relative to D 00:00
            c = sum(cost(D + timedelta(days=(s_abs + j) // 24), (s_abs + j) % 24) for j in range(L))
            key = (round(c, 6), abs((s_abs % 24) - 23))
            if best is None or key < best[0]:
                best = (key, s_abs)
        for j in range(L):
            a = best[1] + j
            out[(D + timedelta(days=a // 24), a % 24)] = D
    out["__meta__"] = meta  # type: ignore[index]
    return out


def _state(row: dict | None, asleep: bool) -> str:
    steps = (row or {}).get("steps") or 0.0
    bpm = (row or {}).get("bpm")
    if steps >= ACTIVE_STEPS_PER_HOUR or (bpm is not None and bpm >= ACTIVE_BPM):
        return "active"
    return "sleep" if asleep else "awake"


# ------------------------------------------------------------------------------------------------ build
def _build(pid: str, start: date, hourly, daily) -> tuple[list[dict], dict]:
    h, d = hourly[pid], daily.get(pid, {})
    sleep = _infer_sleep(h, d, start)
    meta_sleep = sleep.pop("__meta__")  # type: ignore[arg-type]
    ticks: list[dict] = []
    rep = {"implausible_dropped": defaultdict(int), "hours_state": defaultdict(int), "ticks_good": 0, "ticks_gap": 0,
           "channel_ticks": defaultdict(int), "sleep_inference": meta_sleep}
    for i in range(DAYS * TICKS_PER_DAY):
        day_i, tod = divmod(i, TICKS_PER_DAY)
        hour_in_day = tod // 4
        src_day = start + timedelta(days=day_i)
        key = (src_day, hour_in_day)
        row = h.get(key)
        wake = sleep.get(key)
        asleep = wake is not None
        state = _state(row, asleep)
        v: dict[str, float] = {}
        if row:
            if row["bpm"] is not None:
                if _ok("heart_rate", row["bpm"]):
                    v["heart_rate"] = round(row["bpm"], 1)
                else:
                    rep["implausible_dropped"]["heart_rate"] += 1
            if row["steps"] is not None:
                if _ok("steps", row["steps"]):
                    v["steps"] = round(row["steps"] / 4.0, 1)           # hour total split over 4 ticks
                else:
                    rep["implausible_dropped"]["steps"] += 1
            if row["temp"] is not None:
                if _ok("skin_temp_delta", row["temp"]):
                    v["skin_temp_delta"] = round(row["temp"], 2)
                else:
                    rep["implausible_dropped"]["skin_temp_delta"] += 1
        if state == "sleep" and wake is not None:
            nd = d.get(wake)
            if nd:
                for ch, field in (("hrv_rmssd", "rmssd"), ("respiratory_rate", "resp"), ("spo2", "spo2")):
                    val = nd[field]
                    if val is None:
                        continue
                    if _ok(ch, val):
                        v[ch] = round(val, 1)
                    else:
                        rep["implausible_dropped"][ch] += 1
        good = bool(v)
        rep["ticks_good" if good else "ticks_gap"] += 1
        for ch in v:
            rep["channel_ticks"][ch] += 1
        ticks.append({"i": i, "ts": (START + timedelta(minutes=15 * i)).isoformat(timespec="minutes"), "day": day_i, "state": state,
                      "quality": "good" if good else "gap", "v": v,
                      "src_ts": datetime(src_day.year, src_day.month, src_day.day, hour_in_day).isoformat(timespec="minutes"),
                      "res": "hourly"})
        if i % 4 == 0:
            rep["hours_state"][state] += 1
    rep = {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in rep.items()}
    return ticks, rep


def _persona_meta(pid_letter: str, sel: dict, rep: dict) -> dict:
    channels = sorted(rep["channel_ticks"])
    cov = sel
    return {
        "name": f"Real participant {pid_letter} (LifeSnaps)",
        "tag": "real data",
        "blurb": ("Anonymized real participant from the public LifeSnaps study (Fitbit Sense), replayed on a 2026-10-01 timeline. "
                  f"28 consecutive days; heart rate covers {100 * cov['hr_cov']:.1f}% of hours; hourly data; sleep periods are inferred."),
        "source": SOURCE, "license": LICENSE, "citation": CITATION, "channels": channels,
    }


def build(force: bool = False) -> dict:
    """Select personas and write data/normalized/realdata/{selection.json, <id>.json}. Returns the selection dict."""
    sel_path = CACHE / "selection.json"
    if sel_path.exists() and not force:
        return json.loads(sel_path.read_text())
    if not (HOURLY.exists() and DAILY.exists()):
        return {"personas": {}, "error": f"LifeSnaps CSVs not found under {RAW_DIR}"}
    hourly, daily, dup = _load()
    ranking = _select(hourly, daily)
    CACHE.mkdir(parents=True, exist_ok=True)
    personas = {}
    for pid_id, cand in zip(IDS, ranking[: len(IDS)]):
        start = date.fromisoformat(cand["start"])
        ticks, rep = _build(cand["pid"], start, hourly, daily)
        (CACHE / f"{pid_id}.json").write_text(json.dumps(ticks, separators=(",", ":")))
        personas[pid_id] = {"meta": _persona_meta(LETTERS[pid_id], cand, rep), "participant": cand["pid"], "window_start": cand["start"],
                            "coverage": {k: v for k, v in cand.items() if k not in ("pid", "start")}, "report": rep}
    out = {"built_from": {"hourly": HOURLY.name, "daily": DAILY.name, "hourly_duplicate_keys_dropped": dup,
                          "participants_with_a_qualifying_window": len(ranking), "min_hr_coverage": MIN_HR_COVERAGE},
           "ranking_top10": ranking[:10], "personas": personas}
    sel_path.write_text(json.dumps(out, indent=1, default=str))
    return out


# ------------------------------------------------------------------------------------------------ public API
def available() -> dict:
    sel = build()
    return {pid: dict(p["meta"]) for pid, p in sel.get("personas", {}).items()}


def ticks(persona_id: str) -> list[dict]:
    build()
    path = CACHE / f"{persona_id}.json"
    if not path.exists():
        raise KeyError(persona_id)
    return json.loads(path.read_text())


# ------------------------------------------------------------------------------------------------ labeled episodes
_EPISODE_METRICS = {"heart_rate_bpm": "hr", "eda_microsiemens": "eda", "eda_scl_avg": "eda", "skin_temp_celsius": "temp",
                    "acc_magnitude_mean_g": "acc", "acc_magnitude_sd_g": "acc_sd"}


def episodes(force: bool = False) -> list[dict]:
    """One summary per (dataset, participant, session, context) of labeled real recordings.

    Sources: data/normalized/qol_stress (rest vs stress; labels inferred from questionnaire times) and
    data/normalized/wearable_stress_exercise (rest/stress/aerobic/anaerobic from tags + protocol). Only rows with quality == ok
    and a known context contribute. Fields: hr_mean/hr_p90 (bpm), eda_mean, temp_mean (celsius), acc_mean_g (mean of the per-second vector
    magnitude in g), acc_sd_g (mean per-second SD), duration_s (span of heart-rate rows), n_hr.
    """
    cache = CACHE / "episodes.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text())
    acc: dict[tuple, dict] = {}
    for ds in ("qol_stress", "wearable_stress_exercise"):
        path = NORM / ds / "observations.csv.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", newline="") as f:
            for r in csv.DictReader(f):
                m = _EPISODE_METRICS.get(r["metric"])
                if not m or r["quality"] != "ok" or r["context"] in ("unknown", "") or r["value"] == "":
                    continue
                if ds == "qol_stress" and r["session"] != "fitbit-1s" and m == "hr":
                    continue                                      # only the 1 s Fitbit series
                if ds == "qol_stress" and m == "eda" and r["metric"] != "eda_scl_avg":
                    continue
                key = (ds, r["participant"], r["session"] if ds == "wearable_stress_exercise" else "", r["context"])
                e = acc.setdefault(key, defaultdict(list))
                e[m].append(float(r["value"]))
                if m == "hr":
                    e["t"].append(float(r["t_sec"]) if r["t_sec"] else None)
    out = []
    for (ds, pid, session, ctx), e in sorted(acc.items()):
        hr = sorted(e.get("hr", []))
        ts = [t for t in e.get("t", []) if t is not None]
        mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None      # noqa: E731
        out.append({"id": f"{ds}:{pid}:{session or 'all'}:{ctx}", "dataset": ds, "participant": pid, "session": session, "context": ctx,
                    "n_hr": len(hr), "duration_s": round(max(ts) - min(ts) + 1) if ts else None,
                    "hr_mean": mean(hr), "hr_p90": round(hr[int(0.9 * (len(hr) - 1))], 2) if hr else None,
                    "eda_mean": mean(e.get("eda", [])), "temp_mean": mean(e.get("temp", [])),
                    "acc_mean_g": mean(e.get("acc", [])), "acc_sd_g": mean(e.get("acc_sd", []))})
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out
