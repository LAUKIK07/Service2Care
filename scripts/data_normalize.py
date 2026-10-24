"""Normalize the four public datasets into one long format (see scripts/data_common.py for the column list).

    uv run python scripts/data_normalize.py                      # all four
    uv run python scripts/data_normalize.py --dataset lifesnaps  # one of: lifesnaps qol_stress wearable_stress_exercise mmash

Reads data/raw/*.zip (extracts the small datasets and LifeSnaps CSVs into data/raw/extracted/ on first use; the 9.7 GB
LifeSnaps bson is STREAMED from the zip, never extracted). Writes data/normalized/<dataset>/{observations.csv.gz,
participants.csv, quality.json, ...}. Re-running gives identical output (everything is sorted and deterministic).
Rules: never impute; keep missing as missing; flag implausible values (bounds in data_common.BOUNDS), never delete them;
convert units explicitly (each conversion is commented where it happens); keep different HRV statistics separate.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import json
import math
import re
import statistics
import sys
import time
import zipfile
from array import array
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_common import EXTRACTED, OUT, RAW, Sink, judge, open_text, write_json  # noqa: E402

UTC = timezone.utc


def _unzip(zip_name: str, dest: Path, patterns: tuple[str, ...] = ()) -> Path:
    """Extract (once) the members of a zip that match `patterns` (all members when empty) into dest."""
    marker = dest / ".done"
    if not marker.exists():
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(RAW / zip_name) as z:
            for info in z.infolist():
                if info.is_dir() or info.filename.endswith("Wearable_Dataset.ipynb"):
                    continue
                if patterns and not any(re.search(p, info.filename) for p in patterns):
                    continue
                z.extract(info, dest)
        marker.write_text("ok\n")
    return dest


def _find(root: Path, name: str) -> Path:
    hits = sorted(root.rglob(name))
    if not hits:
        raise FileNotFoundError(f"{name} under {root}")
    return hits[0]


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if math.isnan(v) else v


def _tod(s: str) -> int:
    """'HH:MM:SS' or 'H:MM:SS' (possibly with a leading date such as 'Tue Dec 17 13:01:42') -> seconds of day."""
    m = re.search(r"(\d{1,2}):(\d\d):(\d\d)", s)
    if not m:
        raise ValueError(f"no clock time in {s!r}")
    h, mi, se = map(int, m.groups())
    return h * 3600 + mi * 60 + se


def _hms(sec: float) -> str:
    sec = int(round(sec)) % 86400
    return f"T{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _finish(sink: Sink, extra: dict | None = None) -> dict:
    sink.close()
    q = sink.quality(extra)
    q["output_file"] = sink.path.name
    q["output_sha256"] = _sha256(sink.path)
    write_json(sink.dir / "quality.json", q)
    return q


# =====================================================================================================================
# Wearable Device Dataset from Induced Stress and Structured Exercise Sessions (PhysioNet 1.0.1, Empatica E4)
# =====================================================================================================================
# Tag timeline of each protocol, read from the figures in the dataset's own notebook (Wearable_Dataset.ipynb).
# Tag numbers are 1-based; 0 = recording start, N+1 = recording end. Each segment: (start, end, context, name, nominal_s).
# A start/end is (tag, offset_seconds). Sessions whose tag count differs from the protocol are NOT segmented (context unknown).
def _t(i: int, off: float = 0.0):
    return (i, off)


E4_PROTOCOLS: dict[tuple[str, str], dict] = {
    # The notebook says 12 tags; every one of the 18 V1 stress files has 13. Measured interval medians (docs/data.md) match the
    # figure for tags 1-12 and show the 13th as an extra trailing tag, so tags 1-12 keep the figure's meaning and tag 13 is unused.
    ("STRESS", "V1"): {"tags": 13, "segs": [
        (_t(1), _t(2), "rest", "baseline", 180), (_t(3), _t(4), "stress", "stroop", 120), (_t(4), _t(5), "rest", "first-rest", 300),
        (_t(5), _t(6), "stress", "tmct", 180), (_t(6), _t(7), "rest", "second-rest", 300), (_t(7), _t(8), "stress", "real-opinion", 30),
        (_t(9), _t(10), "stress", "opposite-opinion", 30), (_t(11), _t(12), "stress", "subtract", 30)]},
    ("STRESS", "V2"): {"tags": 9, "segs": [
        (_t(0), _t(1), "rest", "baseline", None), (_t(2), _t(3), "stress", "tmct", 180), (_t(3), _t(4), "rest", "first-rest", 600),
        (_t(4), _t(5), "stress", "real-opinion", 30), (_t(6), _t(7), "stress", "opposite-opinion", 30),
        (_t(7), _t(8), "rest", "second-rest", 600), (_t(8), _t(9), "stress", "subtract", 30)]},
    ("AEROBIC", "V1"): {"tags": 12, "segs": [(_t(0), _t(1), "aerobic", "warmup", 180)]
                        + [(_t(i), _t(i + 1), "aerobic", f"block-{i:02d}", 180 if i <= 6 else 120) for i in range(1, 11)]
                        + [(_t(11), _t(12), "aerobic", "cooldown", 240), (_t(12), _t(13), "rest", "rest", 120)]},
    ("AEROBIC", "V2"): {"tags": 8, "segs": [
        (_t(0), _t(1, -135), "rest", "baseline", 270), (_t(1, -135), _t(1), "aerobic", "warmup", 135),
        (_t(1), _t(2), "aerobic", "block-01", 90), (_t(2), _t(3), "aerobic", "block-02", 90), (_t(3), _t(4), "aerobic", "block-03", 90),
        (_t(4), _t(5), "aerobic", "block-04", 675), (_t(5), _t(6), "aerobic", "block-05", 270),
        (_t(6), _t(7), "aerobic", "cooldown", 180), (_t(7), _t(8), "rest", "rest", 120)]},
    ("ANAEROBIC", "V1"): {"tags": 7, "segs": [
        (_t(0), _t(1), "anaerobic", "warmup", 180), (_t(1), _t(2), "anaerobic", "sprint-1", 30), (_t(2), _t(3), "anaerobic", "cooldown-1", 240),
        (_t(3), _t(4), "anaerobic", "sprint-2", 30), (_t(4), _t(5), "anaerobic", "cooldown-2", 240),
        (_t(5), _t(6), "anaerobic", "sprint-3", 30), (_t(6), _t(7), "anaerobic", "cooldown-3", 240), (_t(7), _t(8), "rest", "rest", 120)]},
    ("ANAEROBIC", "V2"): {"tags": 11, "segs": [
        (_t(0), _t(1), "rest", "baseline", 270), (_t(1), _t(2), "anaerobic", "warmup", 270),
        (_t(2), _t(3), "anaerobic", "sprint-1", 45), (_t(3), _t(4), "anaerobic", "cooldown-1", 255),
        (_t(4), _t(5), "anaerobic", "sprint-2", 45), (_t(5), _t(6), "anaerobic", "cooldown-2", 225),
        (_t(6), _t(7), "anaerobic", "sprint-3", 45), (_t(7), _t(8), "anaerobic", "cooldown-3", 225),
        (_t(8), _t(9), "anaerobic", "sprint-4", 45), (_t(9), _t(10), "anaerobic", "cooldown-4", 225), (_t(10), _t(11), "rest", "rest", 120)]},
}
STAGE_CONTEXT = {"STRESS": "stress", "AEROBIC": "aerobic", "ANAEROBIC": "anaerobic"}


def _parse_utc(text: str) -> float:
    """'YYYY-MM-DD HH:MM:SS' with an optional fractional second -> UTC epoch seconds."""
    text = text.strip()
    base, _, frac = text.partition(".")
    t = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
    return t + (float("0." + frac) if frac else 0.0)


def _read_e4(path: Path) -> tuple[datetime, float, list[list[float]]]:
    """Empatica file: line 1 = start (UTC), line 2 = sample rate (Hz) per column, then data rows."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    start = datetime.fromtimestamp(_parse_utc(lines[0].split(",")[0]), UTC)
    rate = float(lines[1].split(",")[0])
    rows = [[float(x) for x in ln.split(",")] for ln in lines[2:] if ln.strip()]
    return start, rate, rows


def _dup_half(vals: list) -> bool:
    """True when the second half of the series is an exact copy of the first half (documented defect of STRESS/S02)."""
    n = len(vals)
    return n >= 40 and n % 2 == 0 and vals[: n // 2] == vals[n // 2:]


def norm_wearable() -> dict:
    root = _unzip("wearable_stress_exercise.zip", EXTRACTED / "wearable_stress_exercise")
    base = _find(root, "Wearable_Dataset").resolve()
    top = base.parent
    sink = Sink("wearable_stress_exercise")
    # subject table (demographics as published; unknown ages are '-')
    subj = {}
    with open(top / "subject-info.csv", newline="", encoding="latin-1") as f:
        for r in csv.reader(f):
            if r and re.fullmatch(r"[Sf]\d\d", r[0].strip()):
                subj[r[0].strip()] = r
    with open(sink.dir / "participants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "sex", "age_years", "protocol_version", "did_stress", "did_aerobic", "did_anaerobic"])
        for pid, r in sorted(subj.items()):
            w.writerow([pid, r[1], r[2] if r[2] != "-" else "", r[6], r[7], r[8], r[9]])
    sessions_found: dict[str, set] = defaultdict(set)
    timing: dict[str, list] = defaultdict(list)
    seg_stats = {"sessions": 0, "segmented": 0, "tag_count_mismatch": []}
    dup_half_sessions = []
    for activity in ("STRESS", "AEROBIC", "ANAEROBIC"):
        for sdir in sorted((base / activity).iterdir()):
            if not sdir.is_dir():
                continue
            name = sdir.name
            pid, _, part = name.partition("_")
            version = "V1" if pid.startswith("S") else "V2"
            session = activity + (f"-{part}" if part else "")
            sessions_found[activity].add(pid)
            seg_stats["sessions"] += 1
            # ---- tags -> segments
            tags = []
            for ln in (sdir / "tags.csv").read_text().splitlines():
                if ln.strip():
                    tags.append(datetime.strptime(ln.strip().split(",")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())
            proto = E4_PROTOCOLS[(activity, version)]
            starts, ends = [], []
            hr_start, _, hr_rows = _read_e4(sdir / "HR.csv")
            rec_start = min(_read_e4(sdir / "EDA.csv")[0].timestamp(), hr_start.timestamp())
            rec_end = hr_start.timestamp() + len(hr_rows)
            segs: list[tuple] = []
            have_split = len(tags) == proto["tags"] and not part       # a session split in two files cannot be mapped by tag number
            if have_split:
                seg_stats["segmented"] += 1

                def at(pt):
                    i, off = pt
                    if i == 0:
                        return rec_start + off
                    if i == len(tags) + 1:
                        return rec_end + off
                    return tags[i - 1] + off
                for a, b, ctx, nm, nominal in proto["segs"]:
                    t0, t1 = at(a), at(b)
                    if t1 > t0:
                        segs.append((t0, t1, ctx, nm))
                        if nominal:
                            timing[f"{activity}-{version}/{nm}"].append((t1 - t0) / nominal)
                segs.sort()
                starts = [s[0] for s in segs]
            else:
                seg_stats["tag_count_mismatch"].append(f"{name}:{activity}:{len(tags)}tags(expected {proto['tags']}{', split file' if part else ''})")

            def label(ts: float):
                if not segs:
                    return "unknown", "", "tags-unmapped"
                k = bisect.bisect_right(starts, ts) - 1
                if k >= 0 and segs[k][0] <= ts < segs[k][1]:
                    return segs[k][2], segs[k][3], "tags+protocol"
                return "unknown", "", "tags+protocol"

            def emit(metric, unit, series_start, rate, values, *, flag_from=None, extra_period=None):
                """series values at series_start + i/rate. flag_from: index from which values are source-documented duplicates."""
                for i, v in enumerate(values):
                    ts = series_start + i / rate
                    ctx, seg, src = label(ts)
                    q = "source-flagged-invalid" if (flag_from is not None and i >= flag_from) else None
                    sink.add(pid, metric, unit, v, datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds"), "utc-deidentified",
                             epoch=ts, session=session, t_sec=ts - rec_start, quality=q, context=ctx, segment=seg, label_source=src,
                             period_s=1 / rate)

            # ---- signals
            sink.source_rows["HR.csv"] += len(hr_rows)
            hr = [r[0] for r in hr_rows]
            emit("heart_rate_bpm", "bpm", hr_start.timestamp(), 1.0, hr, flag_from=len(hr) // 2 if _dup_half(hr) else None)
            for code, metric, unit in (("EDA", "eda_microsiemens", "microsiemens"), ("TEMP", "skin_temp_celsius", "celsius")):
                st, rate, rows = _read_e4(sdir / f"{code}.csv")
                sink.source_rows[f"{code}.csv"] += len(rows)
                vals = [r[0] for r in rows]
                dh = _dup_half(vals)
                if dh:
                    dup_half_sessions.append(f"{name}:{activity}:{code}")
                # f07 STRESS: the dock covered PPG and temperature sensors (data_constraints.txt) -> skin temperature invalid
                invalid_all = code == "TEMP" and pid == "f07" and activity == "STRESS"
                if invalid_all:
                    emit(metric, unit, st.timestamp(), rate, vals, flag_from=0)
                else:
                    emit(metric, unit, st.timestamp(), rate, vals, flag_from=len(vals) // 2 if dh else None)
            # accelerometer: 32 Hz, three columns, unit 1/64 g -> g (divide by 64). Reduced to one row per second:
            # mean and standard deviation of the vector magnitude over the second (orientation independent).
            st, rate, rows = _read_e4(sdir / "ACC.csv")
            sink.source_rows["ACC.csv"] += len(rows)
            accdh = _dup_half([tuple(r) for r in rows])
            if accdh:
                dup_half_sessions.append(f"{name}:{activity}:ACC")
            step = int(rate)
            for k in range(0, len(rows) - step + 1, step):
                mags = [math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2) / 64.0 for r in rows[k:k + step]]
                mean = sum(mags) / step
                sd = math.sqrt(sum((m - mean) ** 2 for m in mags) / step)
                ts = st.timestamp() + k / rate
                ctx, seg, src = label(ts)
                q = "source-flagged-invalid" if accdh and k >= (len(rows) // 2) else None
                iso = datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")
                for metric, v in (("acc_magnitude_mean_g", mean), ("acc_magnitude_sd_g", sd)):
                    sink.add(pid, metric, "g", v, iso, "utc-deidentified", epoch=ts, session=session, t_sec=ts - rec_start, quality=q,
                             context=ctx, segment=seg, label_source=src, n_obs=step, period_s=1.0)
            # inter-beat intervals (derived by the E4 from BVP): column 0 = offset from start (s), column 1 = duration (s)
            ibi_lines = (sdir / "IBI.csv").read_text().splitlines()
            if not ibi_lines:                                    # documented: STRESS-independent, ANAEROBIC/S01 IBI file is empty
                sink.counts["ibi_files_empty"] += 1
                ibi_lines = [""]
            ist_ts = _parse_utc(ibi_lines[0].split(",")[0]) if ibi_lines[0].strip() else 0.0
            ibi, bad_rows = [], 0
            for ln in ibi_lines[1:]:
                parts_ = ln.split(",")
                try:
                    ibi.append((float(parts_[0]), float(parts_[1])))
                except (ValueError, IndexError):
                    bad_rows += 1                                # e.g. a second header line "1684532969.0, IBI" inside STRESS/S02
            if bad_rows:
                sink.counts["ibi_non_numeric_rows_skipped"] += bad_rows
                sink.notes["ibi_non_numeric_rows"].append(f"{name}:{activity}:{bad_rows}")
            # an IBI file whose start time is far from the HR file of the same session cannot be aligned -> flag every row
            ibi_misaligned = bool(ibi) and abs(ist_ts - hr_start.timestamp()) > 3600
            if ibi_misaligned:
                sink.notes["ibi_start_misaligned"].append(f"{name}:{activity}: IBI start {ibi_lines[0].split(',')[0]} vs HR start {hr_start:%Y-%m-%d %H:%M:%S}")
            sink.source_rows["IBI.csv"] += len(ibi)
            for off, dur in ibi:
                ts = (hr_start.timestamp() if ibi_misaligned else ist_ts) + off
                ctx, seg, src = label(ts)
                sink.add(pid, "ibi_seconds", "s", dur, datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds"),
                         "utc-deidentified", epoch=ts, session=session, t_sec=ts - rec_start, context=ctx, segment=seg, label_source=src,
                         quality="source-flagged-invalid" if ibi_misaligned else None)
            nb = sum(1 for _ in open(sdir / "BVP.csv", "rb")) - 2
            sink.source_rows["BVP.csv"] += nb
    sink.skipped["BVP.csv"] = "64 Hz raw PPG waveform, unitless; HR and IBI (already derived by the device) are kept instead"
    sink.skipped["ACC.csv (raw 32 Hz)"] = "reduced to 1 Hz mean/sd of vector magnitude in g (derived by us, documented)"
    # self-reported stress level per stage (0-10), as published
    for fname, ver in (("Stress_Level_v1.csv", "V1"), ("Stress_Level_v2.csv", "V2")):
        with open(top / fname, newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f)
            header = next(r)[1:]
            for row in r:
                for stage, v in zip(header, row[1:]):
                    ctx = "rest" if "Rest" in stage or stage == "Baseline" else "stress"
                    sink.add(row[0], "self_reported_stress_0_10", "points", _num(v), "", "relative-seconds", epoch=0, session="STRESS",
                             context=ctx, segment=stage.lower().replace(" ", "-"), label_source="dataset", check_order=False)
    timing_summary = {k: {"n": len(v), "median_observed_over_nominal": round(statistics.median(v), 3),
                          "p10": round(sorted(v)[len(v) // 10], 3), "p90": round(sorted(v)[(len(v) * 9) // 10], 3)} for k, v in sorted(timing.items())}
    q = _finish(sink, {
        "participants_by_activity": {k: len(v) for k, v in sessions_found.items()},
        "session_folders": seg_stats["sessions"], "sessions_segmented_by_tags": seg_stats["segmented"],
        "tag_count_mismatch": seg_stats["tag_count_mismatch"], "duplicated_second_half_series": dup_half_sessions,
        "segment_duration_vs_nominal": timing_summary})
    return q


# =====================================================================================================================
# QoL_Stress (PhysioNet 1.0.0): Fitbit Charge 6 HR, ECG (Fitbit app), EDA, questionnaires; relax vs induced stress
# =====================================================================================================================
def norm_qol() -> dict:
    root = _unzip("qol_stress.zip", EXTRACTED / "qol_stress")
    data = _find(root, "participants.csv").parent
    sink = Sink("qol_stress")
    csv.field_size_limit(10**9)

    def rows(name):
        with open_text(data / name) as f:
            return list(csv.DictReader(f))

    pid = lambda u: u[:10]            # noqa: E731  (first 10 hex chars of the published hash; checked unique below)
    parts = rows("participants.csv")
    assert len({pid(r["username"]) for r in parts}) == len(parts), "shortened ids collide"
    with open(sink.dir / "participants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "sex", "age_years"])
        for r in parts:
            w.writerow([pid(r["username"]), r["sex"], r["age"]])
    # questionnaire completion times give the phase windows of the Fitbit HR files, which carry no phase label
    stai = {(r["username"], r["phase"]): r for r in rows("stai.csv")}
    windows = {}
    hr1 = defaultdict(list)
    for r in rows("fitbit_heart_rate_per_second.csv"):
        sink.source_rows["fitbit_heart_rate_per_second.csv"] += 1
        hr1[r["username"]].append((_tod(r["time"]), _num(r["value"])))
    for u, series in hr1.items():
        t_relax, t_stress = _tod(stai[(u, "relax")]["time"]), _tod(stai[(u, "stress")]["time"])
        windows[u] = (min(t for t, _ in series), t_relax, t_stress)

    def phase_of(u, t):
        a, b, c = windows[u]
        if a <= t <= b:
            return "rest", "relax-window"
        if b < t <= c:
            return "stress", "stress-window"
        return "unknown", ""

    for u, series in sorted(hr1.items()):
        seen = set()
        for t, v in series:
            if t in seen:
                sink.dup("heart_rate_bpm")
                continue
            seen.add(t)
            ctx, seg = phase_of(u, t)
            sink.add(pid(u), "heart_rate_bpm", "bpm", v, _hms(t), "time-of-day-only", epoch=t, session="fitbit-1s", context=ctx, segment=seg,
                     label_source="inferred-questionnaire-time" if ctx != "unknown" else "", period_s=None)
    for r in sorted(rows("fitbit_heart_rate_per_minute.csv"), key=lambda r: (r["username"], _tod(r["time"]))):
        sink.source_rows["fitbit_heart_rate_per_minute.csv"] += 1
        t = _tod(r["time"])
        ctx, seg = phase_of(r["username"], t)
        sink.add(pid(r["username"]), "heart_rate_minute_bpm", "bpm", _num(r["value"]), _hms(t), "time-of-day-only", epoch=t, session="fitbit-1min",
                 context=ctx, segment=seg, label_source="inferred-questionnaire-time" if ctx != "unknown" else "")
    # EDA: per phase, time_sec is seconds from the start of that phase's recording (5 s resolution). scl_avg has no stated unit.
    for r in rows("eda.csv"):
        sink.source_rows["eda.csv"] += 1
        phase = r["phase"]
        ctx = {"relax": "rest", "stress": "stress"}[phase]
        valid = r["valid_data"].strip().lower() == "true"
        ts = float(r["time_sec"])
        sink.add(pid(r["username"]), "eda_scl_avg", "unspecified", _num(r["scl_avg"]), "", "relative-seconds", epoch=ts, session=phase,
                 t_sec=ts, quality=None if valid else "source-flagged-invalid", context=ctx, segment=phase, label_source="dataset", check_order=False)
        if ts < 0:
            sink.counts["eda_negative_time_sec"] += 1
    sink.skipped["eda.csv activation column"] = "binary activation flag not used"
    # ECG reading: device-reported heart rate and rhythm classification (waveform not normalized)
    for r in rows("ecg.csv"):
        sink.source_rows["ecg.csv"] += 1
        phase = r["phase"]
        try:
            t = _tod(r["reading_time"])
        except ValueError:
            t = 0
        if not re.fullmatch(r"\d\d:\d\d:\d\d", r["reading_time"].strip()):
            sink.counts["ecg_reading_time_nonstandard_format"] += 1
        sink.add(pid(r["username"]), "ecg_heart_rate_bpm", "bpm", _num(r["heart_rate"]), _hms(t), "time-of-day-only", epoch=t, session=phase,
                 context={"relax": "rest", "stress": "stress"}[phase], segment=r["result_classification"], label_source="dataset", check_order=False)
    sink.skipped["ecg.csv waveform_samples"] = "7500-sample ECG waveform per reading (30 s) not needed"
    for name, metric in (("stai.csv", "stai_state_score"), ("pss_14.csv", "pss14_score")):
        for r in rows(name):
            sink.source_rows[name] += 1
            phase = r.get("phase", "")
            sink.add(pid(r["username"]), metric, "points", _num(r["score"]), _hms(_tod(r["time"])), "time-of-day-only", epoch=_tod(r["time"]),
                     session=phase, context={"relax": "rest", "stress": "stress"}.get(phase, "unknown"), segment=phase,
                     label_source="dataset" if phase else "", check_order=False)
    sink.skipped["fitbit_heart_rate_summary.csv"] = "per-participant HR-zone table, not a time series"
    return _finish(sink, {"time_note": "Fitbit/ECG/questionnaire times are clock times with no date and no time zone; one ECG time has a weekday/date prefix without a year.",
                          "phase_window_rule": "rest = from first HR sample to the relax STAI time; stress = after that up to the stress STAI time (inferred)"})


# =====================================================================================================================
# MMASH (PhysioNet 1.0.0): 22 men, ~24 h each. Beat-to-beat RR, ActiGraph, sleep log, questionnaires
# =====================================================================================================================
def _clean_rr(rr: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Keep beats with 0.3 <= RR <= 2.0 s and within 20% of the previous kept beat (simple ectopic filter)."""
    out = []
    prev = None
    for t, d in rr:
        if not (0.3 <= d <= 2.0):
            continue
        if prev is not None and abs(d - prev) / prev > 0.20:
            prev = d
            continue
        out.append((t, d))
        prev = d
    return out


def norm_mmash() -> dict:
    root = _unzip("mmash.zip", EXTRACTED / "mmash")
    inner = _find(root, "MMASH.zip")
    dest = inner.parent / "inner"
    if not (dest / ".done").exists():
        with zipfile.ZipFile(inner) as z:
            z.extractall(dest)
        (dest / ".done").write_text("ok\n")
    users = sorted((p for p in dest.rglob("user_*") if p.is_dir()), key=lambda p: int(p.name.split("_")[1]))
    sink = Sink("mmash")
    sleep_rows, info_rows = [], []

    def wrap(tods: list[int]) -> list[int]:
        """time of day -> seconds since day-1 midnight, adding 24 h at each backward jump of more than 12 h."""
        out, off, prev = [], 0, None
        for t in tods:
            if prev is not None and t - prev < -43200:
                off += 86400
            out.append(t + off)
            prev = t
        return out

    for u in users:
        pid = u.name
        with open_text(u / "user_info.csv") as f:
            ui = next(csv.DictReader(f))
        info_rows.append([pid, ui["Gender"], ui["Age"], ui["Height"], ui["Weight"]])
        if _num(ui["Age"]) == 0:
            sink.notes["age_zero"].append(pid)
        # sleep intervals (day index 1 or 2 + clock time) -> seconds since day-1 midnight
        spans = []
        with open_text(u / "sleep.csv") as f:
            for r in csv.DictReader(f):
                a = (int(r["In Bed Date"]) - 1) * 86400 + _tod(r["In Bed Time"] + ":00")
                b = (int(r["Out Bed Date"]) - 1) * 86400 + _tod(r["Out Bed Time"] + ":00")
                if b < a:
                    b += 86400
                spans.append((a, b))
                sleep_rows.append([pid, a, b, r["Total Sleep Time (TST)"], r["Efficiency"], r["Number of Awakenings"]])
        starts = sorted(spans)

        def ctx_at(t):
            for a, b in starts:
                if a <= t < b:
                    return "sleep", "sleep-log"
            return ("unknown", "") if not starts else ("unknown", "")

        # ActiGraph, 1 Hz: HR (bpm), Steps (per second), Vector Magnitude (ActiGraph counts, device-specific unit)
        with open_text(u / "Actigraph.csv") as f:
            act = list(csv.DictReader(f))
        sink.source_rows["Actigraph.csv"] += len(act)
        t_s = wrap([_tod(r["time"]) for r in act])
        days = Counter(r["day"] for r in act)
        for d in days:
            if d not in ("1", "2"):
                sink.counts["actigraph_day_label_not_1_or_2"] += days[d]
                sink.notes["odd_day_labels"].append(f"{pid}:{d}:{days[d]}")
        seen = set()
        for r, t in zip(act, t_s):
            if t in seen:
                sink.dup("heart_rate_bpm")
                continue
            seen.add(t)
            ctx, src = ctx_at(t)
            iso = _hms(t)
            sink.add(pid, "heart_rate_bpm", "bpm", _num(r["HR"]), iso, "time-of-day-only", epoch=t, t_sec=t, context=ctx, label_source=src, period_s=1.0)
            sink.add(pid, "steps_count", "count", _num(r["Steps"]), iso, "time-of-day-only", epoch=t, t_sec=t, context=ctx, label_source=src, period_s=1.0)
            sink.add(pid, "actigraph_vector_magnitude_counts", "counts", _num(r["Vector Magnitude"]), iso, "time-of-day-only", epoch=t, t_sec=t,
                     context=ctx, label_source=src, period_s=1.0)
        # RR intervals: ibi_s at the second they were logged (several beats can share a second)
        with open_text(u / "RR.csv") as f:
            rr = list(csv.DictReader(f))
        sink.source_rows["RR.csv"] += len(rr)
        tr = wrap([_tod(r["time"]) for r in rr])
        beats = []
        for r, t in zip(rr, tr):
            d = _num(r["ibi_s"])
            ctx, src = ctx_at(t)
            sink.add(pid, "ibi_seconds", "s", d, _hms(t), "time-of-day-only", epoch=t, t_sec=t, context=ctx, label_source=src, check_order=False)
            if d is not None:
                beats.append((t, d))
        # DERIVED here (not in the source): RMSSD per 5-minute window from successive differences of cleaned RR.
        # 'beat' time within a second is not given, so beats keep file order; at least 100 clean differences are required.
        clean = _clean_rr(beats)
        by_win = defaultdict(list)
        for t, d in clean:
            by_win[int(t // 300)].append(d)
        for w in sorted(by_win):
            ds = by_win[w]
            diffs = [(b - a) for a, b in zip(ds, ds[1:])]
            if len(diffs) < 100:
                continue
            rmssd_ms = 1000.0 * math.sqrt(sum(x * x for x in diffs) / len(diffs))      # seconds -> milliseconds
            t = w * 300
            ctx, src = ctx_at(t + 150)
            sink.add(pid, "hrv_rmssd_ms", "ms", rmssd_ms, _hms(t), "time-of-day-only", epoch=t, t_sec=t, context=ctx, label_source=src,
                     n_obs=len(diffs), period_s=300.0)
        sink.counts["rr_rows_removed_by_ectopic_filter_for_rmssd"] += len(beats) - len(clean)
        sink.skipped["Activity.csv"] = "activity-code log; code meanings are not in the files (see Rossi et al. 2020)"
        sink.skipped["questionnaire.csv, saliva.csv"] = "not time series; not needed"
    with open(sink.dir / "participants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "sex", "age_years", "height_cm", "weight_kg"])
        w.writerows(info_rows)
    with open(sink.dir / "sleep_logs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "in_bed_t_sec", "out_bed_t_sec", "total_sleep_time_min", "efficiency_percent", "awakenings"])
        w.writerows(sleep_rows)
    return _finish(sink, {"time_note": "Clock times only; t_sec counts from day-1 00:00 local using the file's own day wrap",
                          "hrv_rmssd_ms": "derived by data_normalize from RR (see docs/data.md)"})


# =====================================================================================================================
# LifeSnaps (Zenodo 7229547 v4): Fitbit Sense, 71 participants. CSVs lack sleep windows/intraday HRV/SpO2, so the Fitbit bson is
# streamed from the zip (never extracted).
# =====================================================================================================================
_DCACHE: dict[str, int] = {}
_US_RE = re.compile(r"^(\d\d)/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)$")


def ls_epoch(s: str) -> int:
    """Local wall-clock string -> seconds since 1970-01-01 00:00 treated as UTC (ordering key only; NOT a real UTC time)."""
    if s[2] == "/":
        m = _US_RE.match(s)
        mo, d, y, h, mi, se = (int(x) for x in m.groups())          # MM/DD/YY (surveyed: no DD/MM values occur in the dump)
        return (date(2000 + y, mo, d).toordinal() - 719163) * 86400 + h * 3600 + mi * 60 + se
    k = s[:10]
    day = _DCACHE.get(k)
    if day is None:
        day = _DCACHE[k] = date(int(k[:4]), int(k[5:7]), int(k[8:10])).toordinal() - 719163
    t = day * 86400
    if len(s) >= 16 and s[10] in "T ":
        t += int(s[11:13]) * 3600 + int(s[14:16]) * 60
        if len(s) >= 19 and s[16] == ":":
            t += int(s[17:19])
    return t


_ISOCACHE: dict[int, str] = {}


def ls_iso(epoch: int) -> str:
    day, sod = divmod(epoch, 86400)
    d = _ISOCACHE.get(day)
    if d is None:
        d = _ISOCACHE[day] = date.fromordinal(day + 719163).isoformat()
    return f"{d}T{sod // 3600:02d}:{sod % 3600 // 60:02d}:{sod % 60:02d}"


class _Series:
    """Compact per-(participant, type) store: epoch seconds (uint32) and a float value, plus an optional small int."""
    __slots__ = ("t", "v", "x")

    def __init__(self):
        self.t, self.v, self.x = array("I"), array("f"), array("b")


def norm_lifesnaps() -> dict:
    from data_bson import decode, doc_type, fast_heart_rate, iter_docs, pid_hex     # noqa: F401  (doc_type used inside iter_docs)
    t0 = time.time()
    root = _unzip("lifesnaps_v4.zip", EXTRACTED / "lifesnaps", ("csv_rais_anonymized/", "scored_surveys/"))
    sink = Sink("lifesnaps")
    series: dict[tuple[str, str], _Series] = defaultdict(_Series)
    nightly: list[tuple] = []          # (pid, kind, epoch, value, extra)
    sleeps: list[dict] = []
    exercises: list[dict] = []
    profiles: dict[str, dict] = {}
    type_counts: Counter = Counter()
    SKIP = {"calories": "per-minute energy estimate, not a monitored channel (9.7M rows)",
            "distance": "per-minute distance, not a monitored channel (3.0M rows)",
            "altitude": "floors/altitude, not a monitored channel",
            "estimated_oxygen_variation": "unitless infrared/red signal ratio (not SpO2 %); 2.0M rows during sleep",
            "Device Temperature": "device (not skin) temperature, sensor_type UNKNOWN",
            "Afib ECG Readings": "ECG waveform readings (73), not needed",
            "Heart Rate Variability Histogram": "histogram buckets, not needed",
            "badge": "badges", "water_logs": "self-logged water", "journal_entries": "self-logged journal",
            "mindfulness_sessions": "session summary, not needed", "mindfulness_goals": "goals",
            "demographic_vo2_max": "Fitbit VO2max estimate", "time_in_heart_rate_zones": "daily zone minutes",
            "lightly_active_minutes": "daily minutes", "moderately_active_minutes": "daily minutes",
            "very_active_minutes": "daily minutes", "sedentary_minutes": "daily minutes"}
    print("  streaming fitbit.bson from the zip (about 1.5-3 minutes)...", flush=True)
    for typ, raw in iter_docs():
        type_counts[typ] += 1
        if typ == "heart_rate":
            pid = pid_hex(raw)
            dt, bpm, conf = fast_heart_rate(raw)
            s = series[(pid, "hr")]
            s.t.append(ls_epoch(dt))
            s.v.append(bpm)
            s.x.append(conf)
            continue
        if typ in SKIP:
            continue
        d = decode(raw)
        pid, data = d["id"], d["data"]
        if typ == "steps":
            s = series[(pid, "steps")]
            s.t.append(ls_epoch(data["dateTime"]))
            s.v.append(float(data["value"]))
        elif typ == "Wrist Temperature":
            s = series[(pid, "wrist")]
            s.t.append(ls_epoch(data["recorded_time"]))
            s.v.append(float(data["temperature"]) if data.get("temperature") is not None else float("nan"))
        elif typ == "Heart Rate Variability Details":
            s = series[(pid, "hrv")]
            s.t.append(ls_epoch(data["timestamp"]))
            s.v.append(float(data["rmssd"]) if data.get("rmssd") is not None else float("nan"))
            s.x.append(int(round(100 * float(data.get("coverage") or 0))))
        elif typ == "Respiratory Rate Summary":
            nightly.append((pid, "resp", ls_epoch(data["timestamp"]), data.get("full_sleep_breathing_rate"), data.get("full_sleep_standard_deviation")))
        elif typ == "Daily SpO2":
            nightly.append((pid, "spo2", ls_epoch(data["timestamp"][:19]), data.get("average_value"), (data.get("lower_bound"), data.get("upper_bound"))))
        elif typ == "Computed Temperature":
            nightly.append((pid, "skin", ls_epoch(data["sleep_end"]), data.get("nightly_temperature"), (data.get("sleep_start"), data.get("sleep_end"), data.get("type"))))
        elif typ == "resting_heart_rate":
            v = data["value"]
            nightly.append((pid, "rhr", ls_epoch(data["dateTime"]), v.get("value") if isinstance(v, dict) else None, None))
        elif typ == "Stress Score":
            nightly.append((pid, "stress", ls_epoch(data["DATE"]), data.get("STRESS_SCORE"), data.get("STATUS")))
        elif typ == "Daily Heart Rate Variability Summary":
            nightly.append((pid, "dailyhrv", ls_epoch(data["timestamp"]), data.get("rmssd"), data.get("nremhr")))
        elif typ == "mindfulness_eda_data_sessions":
            nightly.append((pid, "eda", ls_epoch(data["timestamp"][:19]), data.get("scl_avg"), data.get("valid_data")))
        elif typ == "sleep":
            sleeps.append({"pid": pid, **data})
        elif typ == "exercise":
            exercises.append({"pid": pid, **data})
        elif typ == "Profile":
            profiles[pid] = data
        else:
            sink.notes["unhandled_types"].append(typ)
    print(f"  bson pass done in {time.time() - t0:.0f}s; {sum(type_counts.values()):,} documents", flush=True)
    for typ, n in type_counts.items():
        sink.source_rows[f"fitbit.bson:{typ}"] = n
    for typ, why in SKIP.items():
        if typ in type_counts:
            sink.skipped[f"fitbit.bson:{typ}"] = why
    pids = sorted({k[0] for k in series} | {n[0] for n in nightly} | set(profiles))

    # ------------------------------------------------------------- heart rate, 5-minute means of the raw readings
    # raw readings are ~every 5 s but irregular; confidence 0 readings are excluded from the mean and counted.
    # One row per bin: bin start time, mean bpm, n_obs = number of raw readings. Exact duplicate seconds are dropped and counted.
    BIN = 300
    for pid in pids:
        s = series.get((pid, "hr"))
        if not s:
            continue
        n = len(s.t)
        sink.counts["hr_readings_in_file_order_backward_steps"] += sum(1 for i in range(1, n) if s.t[i] < s.t[i - 1])
        order = sorted(range(n), key=s.t.__getitem__)
        last_t, bins = None, defaultdict(lambda: [0, 0.0])
        for i in order:
            t = s.t[i]
            if t == last_t:
                sink.dup("heart_rate_bpm")
                continue
            last_t = t
            sink.source_rows["hr_readings_unique"] += 1
            if s.x[i] == 0:
                sink.counts["hr_readings_confidence0_excluded"] += 1
                continue
            b = bins[t // BIN]
            b[0] += 1
            b[1] += s.v[i]
        for k in sorted(bins):
            cnt, tot = bins[k]
            sink.add(pid, "heart_rate_bpm", "bpm", tot / cnt, ls_iso(k * BIN), "local-naive", epoch=k * BIN, n_obs=cnt, period_s=BIN, check_order=False)
        series.pop((pid, "hr"))

    def dump_raw(kind: str, metric: str, unit: str, period: float | None, with_x=None):
        for pid in pids:
            s = series.get((pid, kind))
            if not s:
                continue
            n = len(s.t)
            sink.counts[f"{kind}_in_file_order_backward_steps"] += sum(1 for i in range(1, n) if s.t[i] < s.t[i - 1])
            last = None
            for i in sorted(range(n), key=s.t.__getitem__):
                t = s.t[i]
                if t == last:
                    sink.dup(metric)
                    continue
                last = t
                v = s.v[i]
                sink.add(pid, metric, unit, None if math.isnan(v) else v, ls_iso(t), "local-naive", epoch=t, period_s=period, check_order=False)
                if with_x:
                    sink.add(pid, with_x[0], with_x[1], s.x[i] / 100.0, ls_iso(t), "local-naive", epoch=t, period_s=period, check_order=False)

    dump_raw("steps", "steps_count", "count", 60.0)                      # per-minute counts as published; absent minutes stay absent
    dump_raw("wrist", "skin_temp_delta_celsius", "celsius", 360.0)       # °C relative to the device baseline (NOT absolute)
    dump_raw("hrv", "hrv_rmssd_ms", "ms", 300.0, with_x=("hrv_coverage_fraction", "ratio"))   # 5-minute RMSSD; coverage 0..1

    # ------------------------------------------------------------- nightly / daily summaries (timestamps as given)
    for pid, kind, ep, val, extra in sorted(nightly, key=lambda x: (x[0], x[1], x[2])):
        iso = ls_iso(ep)
        if kind == "resp":
            sink.add(pid, "respiratory_rate_brpm", "brpm", val, iso, "local-naive", epoch=ep, session="nightly-summary", context="sleep", label_source="dataset", check_order=False)
        elif kind == "spo2":
            sink.add(pid, "spo2_percent", "percent", val, iso, "local-naive", epoch=ep, session="nightly-summary", context="sleep", label_source="dataset", check_order=False)
            sink.add(pid, "spo2_lower_bound_percent", "percent", extra[0], iso, "local-naive", epoch=ep, session="nightly-summary", context="sleep", label_source="dataset", check_order=False)
            sink.add(pid, "spo2_upper_bound_percent", "percent", extra[1], iso, "local-naive", epoch=ep, session="nightly-summary", context="sleep", label_source="dataset", check_order=False)
        elif kind == "skin":
            # nightly mean skin temperature in degrees C as reported by the device (absolute), time = sleep end; sleep start kept in segment
            sink.add(pid, "skin_temp_celsius", "celsius", val, iso, "local-naive", epoch=ep, session="nightly-summary", context="sleep",
                     segment=f"sleep_start={extra[0]}", label_source="dataset", check_order=False)
        elif kind == "rhr":
            sink.add(pid, "rhr_daily_bpm", "bpm", val, iso, "local-naive", epoch=ep, session="daily-summary", check_order=False)
        elif kind == "stress":
            sink.add(pid, "fitbit_stress_score_0_100", "points", val, iso, "local-naive", epoch=ep, session="daily-summary", segment=str(extra), check_order=False)
        elif kind == "dailyhrv":
            sink.add(pid, "hrv_rmssd_daily_ms", "ms", val, iso, "local-naive", epoch=ep, session="daily-summary", context="sleep", label_source="dataset", check_order=False)
        elif kind == "eda":
            sink.add(pid, "eda_scl_avg", "unspecified", val, iso, "local-naive", epoch=ep, session="mindfulness", check_order=False,
                     quality=None if extra else "source-flagged-invalid")
    # ------------------------------------------------------------- sleep logs, stages, exercise, participants
    sleeps = sorted(sleeps, key=lambda d: (d["pid"], d["startTime"]))
    seen_logs = set()
    with gzip.open(sink.dir / "sleep_stages.csv.gz", "wt", newline="") as fs, open(sink.dir / "sleep_logs.csv", "w", newline="") as fl:
        ws, wl = csv.writer(fs), csv.writer(fl)
        ws.writerow(["participant", "log_id", "start_local", "seconds", "level"])
        wl.writerow(["participant", "log_id", "date_of_sleep", "start_local", "end_local", "main_sleep", "type", "minutes_asleep", "minutes_awake",
                     "time_in_bed_min", "efficiency_percent"])
        for d in sleeps:
            if d["logId"] in seen_logs:
                sink.dup("sleep_logs")
                continue
            seen_logs.add(d["logId"])
            wl.writerow([d["pid"], d["logId"], d["dateOfSleep"], d["startTime"][:19], d["endTime"][:19], d.get("mainSleep"), d.get("type"),
                         d.get("minutesAsleep"), d.get("minutesAwake"), d.get("timeInBed"), d.get("efficiency")])
            for seg in (d.get("levels") or {}).get("data", []):
                ws.writerow([d["pid"], d["logId"], seg["dateTime"][:19], seg["seconds"], seg["level"]])
            # short wake periods inside classic logs are in levels.shortData for 'stages' logs; kept out (documented)
    with open(sink.dir / "exercise_logs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "start_local", "duration_ms", "activity", "avg_heart_rate_bpm", "steps", "calories"])
        for d in sorted(exercises, key=lambda d: (d["pid"], str(d.get("startTime", "")))):
            w.writerow([d["pid"], str(d.get("startTime", ""))[:19], d.get("duration"), d.get("activityName"), d.get("averageHeartRate"), d.get("steps"), d.get("calories")])
    with open(sink.dir / "participants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["participant", "gender", "age_band", "bmi_band_or_value"])
        for pid in sorted(profiles):
            p = profiles[pid]
            w.writerow([pid, p.get("gender"), p.get("age"), p.get("bmi")])
    sink.counts["sleep_logs_unique"] = len(seen_logs)
    sink.counts["exercise_logs"] = len(exercises)
    sink.notes["time"].append("All Fitbit timestamps are local wall-clock time of each participant (Zenodo description: 'all times are stored in local time'); "
                              "time zones are not given and daylight-saving changes are not marked.")
    return _finish(sink, {"documents_by_type": dict(type_counts.most_common()), "bson_seconds": round(time.time() - t0)})


RUNNERS = {"qol_stress": norm_qol, "wearable_stress_exercise": norm_wearable, "mmash": norm_mmash, "lifesnaps": norm_lifesnaps}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="all", choices=["all", *RUNNERS])
    a = ap.parse_args()
    for name in (RUNNERS if a.dataset == "all" else [a.dataset]):
        t = time.time()
        print(f"[{name}] normalizing...", flush=True)
        q = RUNNERS[name]()
        print(f"[{name}] {q['rows_out']:,} rows, {q['participants']} participants, {time.time() - t:.0f}s -> data/normalized/{name}/", flush=True)


if __name__ == "__main__":
    main()
