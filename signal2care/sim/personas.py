"""Synthetic personas. Everything here is invented. Times are local clock times of the simulated person."""
from __future__ import annotations

from dataclasses import dataclass

START_DATE = "2026-10-01"   # simulated day 0 (a Thursday)
DAYS = 28
TICK_MINUTES = 15

# Channels. WHOOP's API does not provide the last four; they are extra synthetic channels.
WHOOP_LIKE = ("heart_rate", "hrv_rmssd", "respiratory_rate", "spo2", "skin_temp", "steps")
EXTRA = ("stress", "bp_sys", "bp_dia", "eda")
METRICS = WHOOP_LIKE + EXTRA

UNITS = {"heart_rate": "bpm", "hrv_rmssd": "ms", "respiratory_rate": "br/min", "spo2": "%", "skin_temp": "°C",
         "steps": "steps", "stress": "0-10", "bp_sys": "mmHg", "bp_dia": "mmHg", "eda": "µS"}
LABELS = {"heart_rate": "Heart rate", "hrv_rmssd": "HRV (RMSSD)", "respiratory_rate": "Respiratory rate",
          "spo2": "Blood oxygen", "skin_temp": "Skin temperature", "steps": "Steps", "stress": "Stress (proxy)",
          "bp_sys": "Blood pressure, systolic", "bp_dia": "Blood pressure, diastolic", "eda": "Sweat (EDA)"}


@dataclass(frozen=True)
class Scenario:
    """An injected change. effects maps metric -> ("add", x) or ("mul", x). `truth` is only for evaluation."""
    label: str
    day: int
    hour: float
    hours: float
    effects: dict
    truth: str = ""


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    description: str
    seed: int
    sleep_hr: float
    awake_hr: float
    hrv: float
    resp: float
    spo2: float
    temp: float
    bp_sys: float
    bp_dia: float
    bed: float
    wake: float
    bed_sd: float
    workouts: tuple          # (weekday 0=Mon, start hour, minutes, intensity 0..1)
    scenarios: tuple
    linked: bool = False     # True: alerts for this persona go to the tester's phone


PERSONAS: dict[str, Persona] = {
    "maya": Persona(
        id="maya", name="Arya (runner)", seed=11, linked=True,
        description="Recreational runner, regular schedule. A viral-illness-like pattern starts on day 19, "
                    "and a night of lower blood oxygen on day 24.",
        sleep_hr=52, awake_hr=66, hrv=78, resp=14.2, spo2=97.5, temp=33.4, bp_sys=112, bp_dia=70,
        bed=22.75, wake=6.5, bed_sd=25,
        workouts=((0, 17.5, 50, 0.7), (2, 17.5, 50, 0.7), (4, 17.5, 45, 0.6), (5, 8.0, 75, 0.8)),
        scenarios=(
            Scenario("illness-like pattern", 19, 10, 40,
                     {"heart_rate": ("add", 9), "hrv_rmssd": ("mul", 0.62), "skin_temp": ("add", 0.7),
                      "respiratory_rate": ("add", 1.5), "spo2": ("add", -1.0)}, truth="illness_like"),
            Scenario("low-oxygen night", 24, 23, 6, {"spo2": ("add", -4.5), "respiratory_rate": ("add", 2.0)},
                     truth="low_spo2_night"),
            Scenario("tracker stopped reporting", 14, 13, 4, {"dropout": ("flag", 1)}, truth="sensor_dropout"),
        ),
    ),
    "daniel": Persona(
        id="daniel", name="Daniel (desk job)", seed=23,
        description="Desk worker with a late, irregular schedule. Three short nights from day 9, "
                    "and a stressful afternoon on day 21.",
        sleep_hr=60, awake_hr=76, hrv=48, resp=15.5, spo2=96.5, temp=33.0, bp_sys=126, bp_dia=82,
        bed=24.25, wake=7.25, bed_sd=45,
        workouts=((5, 10.0, 40, 0.3),),
        scenarios=(
            Scenario("short sleep", 9, 0, 72, {"bed_shift": ("add", 3.0)}, truth="short_sleep"),
            Scenario("after short sleep", 10, 7, 72, {"heart_rate": ("add", 4), "hrv_rmssd": ("mul", 0.78),
                                                       "stress": ("add", 1.5)}, truth="short_sleep_effects"),
            Scenario("stressful afternoon", 21, 14, 3, {"bp_sys": ("add", 32), "bp_dia": ("add", 16),
                                                        "heart_rate": ("add", 14), "eda": ("add", 3.0),
                                                        "stress": ("add", 4.0)}, truth="stress_event"),
        ),
    ),
    "priya": Persona(
        id="priya", name="Priya (steady control)", seed=37,
        description="Steady, healthy routine with no injected anomalies. Used to measure false alerts.",
        sleep_hr=56, awake_hr=70, hrv=62, resp=14.8, spo2=97.0, temp=33.2, bp_sys=116, bp_dia=74,
        bed=23.0, wake=6.75, bed_sd=20,
        workouts=((1, 18.0, 40, 0.4), (3, 18.0, 40, 0.4)),
        scenarios=(),
    ),
}
