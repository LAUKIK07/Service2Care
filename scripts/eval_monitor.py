"""Measures the monitor on the simulator: false candidates on clean runs, and detection of the planned events.

    uv run python scripts/eval_monitor.py            # 20 clean seeds per baseline + 10 seeds per planned event
    uv run python scripts/eval_monitor.py --quick    # 6 and 4 seeds

Reference results (2026-10-24, rules (4.5 z, 3 h) (5.0, 2) (7.0, 1)): 0.17 false candidates per clean 28-day run over 60 runs;
illness-like 10/10 (median 3.2 h), low-oxygen night 10/10 (1.8 h), tracker dropout 10/10 (1.8 h), stress afternoon 10/10 (1.8 h),
short sleep 6/10.  Any change to the simulator or the monitor should be compared against these.
"""
from __future__ import annotations

import argparse
import dataclasses
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal2care.monitor import Monitor  # noqa: E402
from signal2care.sim.generator import generate  # noqa: E402
from signal2care.sim.personas import PERSONAS, START_DATE  # noqa: E402

S0 = datetime.fromisoformat(START_DATE)
EVENTS = [
    ("maya", "illness-like", 19, 10, 26, {"heart_rate", "hrv_rmssd", "skin_temp", "respiratory_rate", "spo2"}),
    ("maya", "low-oxygen night", 24, 23, 7, {"spo2", "respiratory_rate"}),
    ("maya", "tracker dropout", 14, 13, 6, {"data_quality"}),
    ("daniel", "stress afternoon", 21, 14, 6, {"bp_sys", "bp_dia", "eda", "stress", "heart_rate"}),
    ("daniel", "short sleep", 9, 0, 100, {"sleep_hours", "rhr_sleep", "hrv_sleep"}),
]


def run(ticks, persona_id):
    mon = Monitor(persona_id)
    out = []
    for t in ticks:
        out += mon.ingest(t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    n_clean, n_event = (6, 4) if ap.parse_args().quick else (20, 10)
    total, runs, by = 0, 0, Counter()
    for base in ("priya", "maya", "daniel"):
        for s in range(n_clean):
            p = dataclasses.replace(PERSONAS[base], seed=1000 + s, scenarios=())
            c = run(generate(p), base)
            total, runs = total + len(c), runs + 1
            by.update((x.kind, x.metric) for x in c)
    print(f"clean runs: {total} false candidates over {runs} runs ({total / runs:.2f} per 28-day run)  most common: {dict(by.most_common(4))}")
    for pid, name, day, hour, window, metrics in EVENTS:
        lat = []
        for s in range(n_event):
            p = dataclasses.replace(PERSONAS[pid], seed=2000 + s)
            t0 = S0 + timedelta(days=day, hours=hour)
            hits = [datetime.fromisoformat(c.ts) for c in run(generate(p), pid)
                    if c.metric in metrics and t0 <= datetime.fromisoformat(c.ts) <= t0 + timedelta(hours=window)]
            lat.append((min(hits) - t0).total_seconds() / 3600 if hits else None)
        got = [x for x in lat if x is not None]
        print(f"{pid:7}{name:18} detected {len(got)}/{len(lat)}" + (f", median latency {statistics.median(got):.1f} h" if got else ""))


if __name__ == "__main__":
    main()
