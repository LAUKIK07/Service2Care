// Constants and small helpers shared by the dashboard.

export const START = Date.UTC(2026, 9, 1, 0, 0); // simulated day 0 (naive local clock, so everything is read as UTC)
export const TICK_MIN = 15;
export const TICKS_PER_DAY = 96;

export const ICONS = {
  heart: "M12 20.5s-7.5-4.7-9.4-9.4A5.3 5.3 0 0 1 12 6.4a5.3 5.3 0 0 1 9.4 4.7c-1.9 4.7-9.4 9.4-9.4 9.4z",
  wave: "M3 12h3.2l2.3-6 4 12 3-9 1.7 3H21",
  wind: "M3 8h10a2.8 2.8 0 1 0-2.8-2.8M3 12h14.5a2.8 2.8 0 1 1-2.8 2.8M3 16h6.5a2.4 2.4 0 1 1-2.4 2.4",
  drop: "M12 3.2s6 6 6 10.4a6 6 0 0 1-12 0C6 9.2 12 3.2 12 3.2z",
  thermo: "M14 14.6V5.2a2 2 0 0 0-4 0v9.4a4 4 0 1 0 4 0z",
  gauge: "M4.2 17a8 8 0 1 1 15.6 0M12 16.5l3.6-5",
  bolt: "M13 3 5 14h6l-1 7 8-11h-6z",
  steps: "M13.2 4.6a1.6 1.6 0 1 1-3.2 0 1.6 1.6 0 0 1 3.2 0zM9.2 21.5l1.9-6.6-2.1-2.2 1.1-5 3 2.8h3M11.3 14.9l3 2.2v4.4",
  moon: "M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.8 6.8 0 0 0 10 10z",
  play: "M8 5.5v13l11-6.5z",
  pause: "M8 5.5v13M16 5.5v13",
  reset: "M4 12a8 8 0 1 0 2.6-5.9M4 4.5v4.6h4.6",
  chat: "M4 5.5h16v10.5H9.5L5 20v-4H4z",
  close: "M6 6l12 12M18 6 6 18",
  check: "M5 12.5l4.2 4.2L19 7",
  info: "M12 11v5.5M12 7.6v.1M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18z",
  diamond: "M12 3l8 9-8 9-8-9z",
  chip: "M8 8h8v8H8zM4 10h2M4 14h2M18 10h2M18 14h2M10 4v2M14 4v2M10 18v2M14 18v2",
  shield: "M12 3l7.5 3v5.5c0 4.6-3.1 8.2-7.5 9.5-4.4-1.3-7.5-4.9-7.5-9.5V6z",
  pulse: "M3 12h4l2.5-6 4 12 2.5-6H21",
};

export function icon(name, size = 20, extra = "") {
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}><path d="${ICONS[name]}"/></svg>`;
}

// keys: tick fields drawn. extra: not a field of WHOOP's API, so it is labelled as an added synthetic signal.
export const METRICS = {
  heart_rate: { label: "Heart rate", unit: "bpm", dec: 0, color: "#FF385C", group: "heart", icon: "heart", keys: ["heart_rate"], minScale: 1.5, hint: "Beats per minute" },
  hrv_rmssd: { label: "HRV", long: "Heart rate variability (RMSSD)", unit: "ms", dec: 0, color: "#7C5CFA", group: "heart", icon: "wave", keys: ["hrv_rmssd"], minScale: 3, hint: "Beat-to-beat variation" },
  respiratory_rate: { label: "Respiratory rate", unit: "br/min", dec: 1, color: "#0EA5E9", group: "breathing", icon: "wind", keys: ["respiratory_rate"], minScale: 0.4, hint: "Breaths per minute" },
  spo2: { label: "Blood oxygen", unit: "%", dec: 1, color: "#0891B2", group: "breathing", icon: "drop", keys: ["spo2"], minScale: 0.5, hint: "SpO\u2082" },
  skin_temp: { label: "Skin temperature", unit: "\u00b0C", dec: 1, color: "#F59E0B", group: "temperature", icon: "thermo", keys: ["skin_temp"], minScale: 0.12, hint: "At the wrist" },
  bp: { label: "Blood pressure", unit: "mmHg", dec: 0, color: "#E4572E", group: "cardio", icon: "gauge", keys: ["bp_sys", "bp_dia"], minScale: 3.5, extra: true, hint: "Systolic / diastolic" },
  stress: { label: "Stress", unit: "/10", dec: 1, color: "#C026D3", group: "stress", icon: "bolt", keys: ["stress"], minScale: 0.6, extra: true, hint: "Proxy score" },
  eda: { label: "Sweat response", unit: "\u00b5S", dec: 1, color: "#14B8A6", group: "stress", icon: "drop", keys: ["eda"], minScale: 0.4, extra: true, hint: "Electrodermal activity" },
  steps: { label: "Steps", unit: "today", dec: 0, color: "#22A06B", group: "activity", icon: "steps", keys: ["steps"], minScale: 20, hint: "Counted since midnight" },
  sleep: { label: "Sleep", unit: "h", dec: 1, color: "#6366F1", group: "activity", icon: "moon", keys: [], minScale: 0.8, hint: "Last completed night" },
};

export const GROUPS = [
  ["all", "All signals"],
  ["heart", "Heart"],
  ["breathing", "Breathing"],
  ["temperature", "Temperature"],
  ["cardio", "Blood pressure"],
  ["stress", "Stress"],
  ["activity", "Activity & sleep"],
];

// Alert metrics use the brain's names; map them to the card that shows them.
export const CARD_OF = {
  heart_rate: "heart_rate", hrv_rmssd: "hrv_rmssd", respiratory_rate: "respiratory_rate", spo2: "spo2", skin_temp: "skin_temp",
  bp_sys: "bp", bp_dia: "bp", stress: "stress", eda: "eda", steps: "steps",
  sleep_hours: "sleep", rhr_sleep: "heart_rate", hrv_sleep: "hrv_rmssd", data_quality: null,
};

export const ALERT_LABELS = {
  heart_rate: "Heart rate", hrv_rmssd: "HRV", respiratory_rate: "Respiratory rate", spo2: "Blood oxygen",
  skin_temp: "Skin temperature", bp_sys: "Blood pressure (systolic)", bp_dia: "Blood pressure (diastolic)",
  stress: "Stress", eda: "Sweat response", steps: "Steps", sleep_hours: "Sleep duration",
  rhr_sleep: "Resting heart rate in sleep", hrv_sleep: "Overnight HRV", data_quality: "Tracker data",
};

export const PERSONAS = {
  maya: { name: "Maya", tag: "Runner", blurb: "Recreational runner with a regular schedule. Her story: an illness-like pattern, a low-oxygen night and a tracker dropout.", hue: "#FF385C" },
  daniel: { name: "Daniel", tag: "Desk job", blurb: "Late, irregular schedule. His story: three short nights, then a stressful afternoon.", hue: "#7C5CFA" },
  priya: { name: "Priya", tag: "Control", blurb: "A steady routine with nothing injected. She shows how quiet the system stays when nothing is wrong.", hue: "#0EA5E9" },
};

// ----------------------------------------------------------------- time
export function parseTs(ts) {
  // "2026-10-22T14:45" -> minutes since START, read as a naive clock
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(ts || "");
  if (!m) return null;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
}
export const tsToIndex = (ts) => {
  const t = parseTs(ts);
  return t == null ? null : Math.round((t - START) / (TICK_MIN * 60000));
};
const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function when(ts, withDay = true) {
  const t = parseTs(ts);
  if (t == null) return "";
  const d = new Date(t);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return withDay ? `${DAY[d.getUTCDay()]} ${MON[d.getUTCMonth()]} ${d.getUTCDate()} \u00b7 ${hh}:${mm}` : `${hh}:${mm}`;
}
export function dayLabel(i) {
  const d = new Date(START + i * TICK_MIN * 60000);
  return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`;
}
export function clock(i) {
  const d = new Date(START + i * TICK_MIN * 60000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

// -------------------------------------------------------------- numbers
export const fmt = (x, dec = 0) => (x == null || Number.isNaN(x) ? "\u2013" : Number(x).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec }));
export const sign = (x, dec = 1) => (x == null ? "\u2013" : (x > 0 ? "+" : x < 0 ? "\u2212" : "") + fmt(Math.abs(x), dec));
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function median(xs) {
  if (!xs.length) return null;
  const a = [...xs].sort((p, q) => p - q);
  const h = a.length >> 1;
  return a.length % 2 ? a[h] : (a[h - 1] + a[h]) / 2;
}
export function robustSpread(xs, med, floor) {
  const dev = xs.map((x) => Math.abs(x - med));
  return Math.max(1.4826 * median(dev), floor);
}
