import { ALERT_LABELS, CARD_OF, GROUPS, METRICS, PERSONAS, TICKS_PER_DAY, clock, dayLabel, esc, fmt, icon, median, robustSpread, sign, tsToIndex, when } from "./format.js";
import { bars, detailChart, sparkline } from "./charts.js";
import { AuthError, call, forgetConn, loadConn } from "./api.js";

const $ = (sel, root = document) => root.querySelector(sel);
const TOTAL = 28 * TICKS_PER_DAY;
const SPEEDS = [[1, "Slow", "one simulated day in about 96 seconds"], [4, "Normal", "about 24 seconds per day"], [12, "Fast", "about 8 seconds per day"], [40, "Turbo", "about 2.4 seconds per day"]];

const S = {
  conn: null, preview: false, persona: "maya",
  ticks: [], lastI: -1, feed: [], lastFeed: 0,
  alerts: [], notes: [], thread: [], monitor: null, rev: -1,
  states: {}, brain: { online: false }, sendAlerts: false,
  group: "all", openMetric: null, range: "3d", selectedAlert: null,
  startDay: 16, hideRoutine: true, seenCmd: new Map(), loadedAt: Date.now(), error: null, timer: null, polling: false,
  bandCache: new Map(), lastCounts: {},
};

// ------------------------------------------------------------------ data
function resetLocal() {
  Object.assign(S, { ticks: [], lastI: -1, feed: [], lastFeed: 0, alerts: [], notes: [], thread: [], monitor: null, rev: -1, bandCache: new Map() });
}

function apply(d) {
  // The run was reset or restarted: the service now holds fewer readings than we do. Start over from zero.
  const epochChanged = d.epoch !== undefined && S.epoch != null && d.epoch !== S.epoch;
  if (d.epoch !== undefined) S.epoch = d.epoch;
  if (epochChanged || (d.max_i !== undefined && S.lastI >= 0 && (d.max_i === null || d.max_i < S.lastI))) {
    resetLocal();
    S.states = d.states || S.states;
    S.refetch = true;
    return;
  }
  const st = d.states?.[S.persona];
  for (const r of d.series || []) { S.ticks[r.i] = r; if (r.i > S.lastI) S.lastI = r.i; }
  for (const f of d.feed || []) { if (f.id > S.lastFeed) { S.feed.push(f); S.lastFeed = f.id; } }
  if (S.feed.length > 400) S.feed = S.feed.slice(-300);
  if (d.alerts) { S.alerts = d.alerts; S.notes = d.annotations || []; S.thread = d.thread || []; S.monitor = d.monitor; S.rev = d.rev; }
  S.states = d.states || S.states;
  S.brain = d.brain || S.brain;
  S.sendAlerts = !!d.send_alerts;
  announceCommands(d.commands || []);
}

function announceCommands(cmds) {
  for (const c of cmds) {
    if (c.created_at < S.loadedAt - 2000) continue;
    const prev = S.seenCmd.get(c.id);
    if (prev === c.status) continue;
    S.seenCmd.set(c.id, c.status);
    if (c.status === "done") toast(commandText(c), "ok");
    else if (c.status === "failed") toast(`That didn't work: ${c.result || "unknown error"}`, "bad");
  }
}
const commandText = (c) => ({ play: "Replay running", stop: "Replay paused", reset: "Run cleared", speed: "Speed changed", send_alerts: c.result || "Alert setting saved" }[c.type] || "Done");

async function poll() {
  if (S.polling || S.preview || !S.conn) return;
  S.polling = true;
  try {
    const q = new URLSearchParams({ persona: S.persona, after_i: S.lastI, after_feed: S.lastFeed, rev: S.rev });
    const d = await call(S.conn, `/api/live?${q}`);
    S.error = null;
    const before = S.alerts.length;
    apply(d);
    render();
    if (S.alerts.length > before && before >= 0) pulse("alerts");
  } catch (e) {
    S.error = e instanceof AuthError ? "auth" : "network";
    renderBanner();
  } finally {
    S.polling = false;
    if (S.refetch) { S.refetch = false; schedule(50); } else schedule();
  }
}

function schedule(ms) {
  clearTimeout(S.timer);
  if (S.preview || !S.conn) return;
  const running = S.states[S.persona]?.status === "running";
  S.timer = setTimeout(poll, ms ?? (document.hidden ? 15000 : running ? 1500 : 5000));
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(50); });

// --------------------------------------------------------------- derived
const lastTick = () => (S.lastI >= 0 ? S.ticks[S.lastI] : null);
const stateOf = () => S.states[S.persona] || null;

function valueAt(i, key) {
  const t = S.ticks[i];
  return t && t.quality === "good" && t.v[key] != null ? t.v[key] : null;
}

function bandFor(key) {
  const day = Math.floor(S.lastI / TICKS_PER_DAY);
  const ck = `${key}:${day}`;
  if (S.bandCache.has(ck)) return S.bandCache.get(ck);
  const minScale = Object.values(METRICS).find((m) => m.keys.includes(key))?.minScale ?? 0.1;
  const byHour = Array.from({ length: 24 }, () => []);
  const stop = Math.min(S.lastI, day * TICKS_PER_DAY - 1);        // earlier days only
  for (let i = 0; i <= stop; i++) { const v = valueAt(i, key); if (v != null) byHour[Math.floor((i % TICKS_PER_DAY) / 4)].push(v); }
  const out = byHour.map((xs) => {
    if (xs.length < 8) return null;
    const med = median(xs);
    const sp = robustSpread(xs, med, minScale);
    return { lo: med - 2.5 * sp, hi: med + 2.5 * sp, med };
  });
  S.bandCache.set(ck, out);
  return out;
}
const bandAt = (key, i) => bandFor(key)[Math.floor((((i % TICKS_PER_DAY) + TICKS_PER_DAY) % TICKS_PER_DAY) / 4)];

function nights() {
  const out = [];
  let run = 0, end = -1;
  for (let i = 0; i <= S.lastI; i++) {
    const t = S.ticks[i];
    if (t && t.state === "sleep") { run++; end = i; }
    else if (run) { if (run >= 12) out.push({ end, hours: run / 4 }); run = 0; }
  }
  return out;                       // completed nights only
}

function stepsToday() {
  const day = Math.floor(S.lastI / TICKS_PER_DAY);
  let sum = 0;
  for (let i = day * TICKS_PER_DAY; i <= S.lastI; i++) sum += S.ticks[i]?.v.steps || 0;
  return sum;
}

const alertCard = (a) => CARD_OF[a.metric];
const OPEN = new Set(["sent", "discussed", "dry_run"]);
const TEXTED = new Set(["sent", "discussed", "resolved", "dry_run", "send_failed"]);

function cardStatus(cardKey) {
  const open = S.alerts.some((a) => alertCard(a) === cardKey && OPEN.has(a.status) && !a.closed_at && S.lastI - (tsToIndex(a.ts) ?? -1e9) < 48);
  if (open) return { cls: "alert", text: "Alert" };
  const keys = METRICS[cardKey].keys;
  const rev = S.monitor?.review;
  if (!rev || !keys.length) return { cls: "learn", text: cardKey === "sleep" ? "Nightly" : "Learning" };
  const z = Math.max(...keys.map((k) => Math.abs(rev[k]?.z ?? 0)));
  if (!keys.some((k) => rev[k])) return { cls: "learn", text: "Learning" };
  if (z >= 4.5) return { cls: "unusual", text: "Unusual" };
  if (z >= 2.5) return { cls: "watch", text: "Watching" };
  return { cls: "ok", text: "Usual" };
}

// ---------------------------------------------------------------- render
function render() {
  renderBanner();
  renderTop();
  renderHero();
  renderPipeline();
  renderChips();
  renderCards();
  renderAlerts();
  renderSide();
  renderFoot();
  if (S.openMetric) renderDrawer(true);
}

function renderBanner() {
  const el = $("#banner");
  let html = "";
  if (S.preview) html = `<div class="banner info">${icon("info", 18)}<div><b>You're viewing a recorded demo run.</b> To control a live run, text <code>demo link</code> to the Signal2Care number and open the link it sends back.</div></div>`;
  else if (!S.conn) html = "";
  else if (S.error === "auth") html = `<div class="banner bad">${icon("shield", 18)}<div><b>This link is no longer valid.</b> Text <code>demo link</code> to the Signal2Care number to get a fresh one.</div></div>`;
  else if (S.error === "network") html = `<div class="banner warn">${icon("info", 18)}<div><b>Can't reach the dashboard service.</b> Retrying automatically.</div></div>`;
  else if (!S.brain.online) html = `<div class="banner warn">${icon("chip", 18)}<div><b>The monitor isn't running.</b> Data below is the last saved state. Start it on your Mac with <code>scripts/serve.sh</code>.</div></div>`;
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

function renderTop() {
  const tabs = Object.entries(PERSONAS).map(([id, p]) => `<button role="tab" aria-selected="${id === S.persona}" data-persona="${id}" class="tab"><span class="av" style="--c:${p.hue}">${p.name[0]}</span><span class="tn">${p.name}<small>${p.tag}</small></span></button>`).join("");
  const live = S.preview ? `<span class="pill muted"><i></i>Recorded</span>` : S.brain.online ? `<span class="pill live"><i></i>Monitor online</span>` : `<span class="pill muted"><i></i>Monitor offline</span>`;
  const html = `<div class="brand"><span class="logo">${icon("pulse", 20)}</span><span class="bn">Signal2Care</span><span class="tag">demo \u00b7 synthetic data</span></div>
    <nav class="tabs" role="tablist" aria-label="Choose a person">${tabs}</nav><div class="top-r">${live}</div>`;
  const el = $("#top-inner");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

function renderHero() {
  const p = PERSONAS[S.persona];
  const st = stateOf();
  const t = lastTick();
  const status = st?.status || "idle";
  const running = status === "running";
  const day = Math.max(0, Math.floor(S.lastI / TICKS_PER_DAY));
  const cursor = Math.max(st?.cursor ?? 0, S.lastI + 1);
  const simState = t?.state;
  const stateChip = simState ? `<span class="state ${simState}">${{ sleep: "Sleeping", awake: "Awake", active: "Exercising" }[simState] || simState}</span>` : "";
  const speed = st?.speed ?? 4;
  const nearest = SPEEDS.reduce((a, b) => (Math.abs(b[0] - speed) < Math.abs(a[0] - speed) ? b : a));
  const dots = S.alerts.filter((a) => TEXTED.has(a.status)).map((a) => {
    const i = tsToIndex(a.ts);
    return i == null ? "" : `<button class="tdot ${a.status === "dry_run" ? "draft" : ""}" style="left:${(i / TOTAL) * 100}%" data-alert="${a.id}" aria-label="${esc((ALERT_LABELS[a.metric] || a.metric) + " alert, " + when(a.ts))}" title="${esc((ALERT_LABELS[a.metric] || a.metric) + " \u00b7 " + when(a.ts))}"></button>`;
  }).join("");
  const dayTicks = Array.from({ length: 27 }, (_, k) => `<i style="left:${((k + 1) / 28) * 100}%"></i>`).join("");
  const offline = !S.preview && !S.brain.online;
  const startOpts = Array.from({ length: 28 }, (_, d) => `<option value="${d}" ${d === S.startDay ? "selected" : ""}>Day ${d + 1} \u00b7 ${dayLabel(d * TICKS_PER_DAY)}</option>`).join("");
  const html = `<div class="hero-main">
      <div class="who"><span class="av lg" style="--c:${p.hue}">${p.name[0]}</span>
        <div><h1>${p.name}<small>${p.tag}</small></h1><p>${esc(p.blurb)}</p></div></div>
      <div class="clockbox" aria-live="off">
        <div class="clock">${t ? clock(S.lastI) : "--:--"}</div>
        <div class="date">${t ? `${when(t.ts).split(" \u00b7 ")[0]} \u00b7 day ${day + 1} of 28` : "Not started"}</div>${stateChip}
      </div></div>
    <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="${TOTAL}" aria-valuenow="${cursor}" aria-label="Replay progress"><div class="fill" style="width:${(cursor / TOTAL) * 100}%"></div>${dayTicks}${dots}</div>
    <div class="track-l"><span>${dayLabel(0)}</span><span>${running ? "Replaying" : status === "finished" ? "Finished" : cursor ? "Paused" : "Ready"}</span><span>${dayLabel(TOTAL - 1)}</span></div>
    <div class="controls">
      <button class="btn primary" id="play" ${offline ? "disabled" : ""}>${icon(running ? "pause" : "play", 18)}<span>${running ? "Pause" : cursor && status !== "finished" ? "Resume" : "Start replay"}</span></button>
      <button class="btn" id="reset" ${offline || !cursor ? "disabled" : ""}>${icon("reset", 18)}<span>Reset</span></button>
      <label class="field"><span>Start at</span><select id="startday" ${offline || running ? "disabled" : ""}>${startOpts}</select></label>
      <div class="seg" role="group" aria-label="Replay speed">${SPEEDS.map(([v, n, tip]) => `<button data-speed="${v}" aria-pressed="${nearest[0] === v}" title="${tip}" ${offline ? "disabled" : ""}>${n}</button>`).join("")}</div>
      <label class="switch" title="Off: alerts are drafted and shown here but nothing is texted"><input type="checkbox" id="sendalerts" ${S.sendAlerts ? "checked" : ""} ${offline ? "disabled" : ""}><span class="knob"></span><span class="sl">${icon("chat", 16)} Text me alerts</span></label>
    </div>
    <p class="hint">${S.sendAlerts ? "Detected changes are texted from the Signal2Care number. Reply there and your words land on the chart." : "Alert texts are off. Detected changes are drafted here and nothing is sent."}${p.id !== "maya" ? " Only Maya is linked to a phone; other people's alerts stay on this page." : ""}</p>`;
  const el = $("#hero");
  if (el.dataset.k !== html) {
    el.dataset.k = html;
    el.innerHTML = html;
  }
}

function renderPipeline() {
  const t = lastTick();
  const rev = S.monitor?.review || {};
  const top = Object.entries(rev).sort((a, b) => Math.abs(b[1].z) - Math.abs(a[1].z))[0];
  const jevItems = S.feed.filter((f) => f.actor === "jev");
  const jevLast = jevItems[jevItems.length - 1];
  const llmItems = S.feed.filter((f) => f.actor === "llm");
  const sent = S.alerts.filter((a) => a.status === "sent" || a.status === "discussed" || a.status === "resolved").length;
  const drafted = S.alerts.filter((a) => a.status === "dry_run").length;
  const held = S.alerts.filter((a) => ["suppressed", "watching", "ignored", "data_issue"].includes(a.status)).length;
  const counts = { stream: S.lastI, monitor: S.monitor?.ts, jev: jevItems.length, llm: llmItems.length, out: sent + drafted };
  const flash = (k) => (S.lastCounts[k] !== undefined && S.lastCounts[k] !== counts[k] ? "flash" : "");
  const topName = top ? `${METRICS[CARD_OF[top[0]]]?.label || ALERT_LABELS[top[0]] || top[0]}` : null;
  const nodes = [
    ["stream", "pulse", "Wearable stream", t ? `${fmt(S.lastI + 1)} readings in \u00b7 15 min each` : "Waiting for data", t ? `${t.quality === "good" ? "Signal good" : "Sensor gap"} at ${clock(S.lastI)}` : ""],
    ["monitor", "chip", "Monitor", top ? `${Object.keys(rev).length} signals checked this hour` : "Learning normal ranges", top ? `Largest move: ${esc(topName)} ${sign(top[1].z)}\u03c3` : "Needs about 5 days of history"],
    ["jev", "diamond", "Jev", jevItems.length ? `${jevItems.length} decisions` : "No decisions yet", jevLast ? esc(jevLast.text.replace(/^Reviewed /, "")) : "Judges whether a change is worth a text"],
    ["llm", "chat", "Language model", llmItems.length ? `${llmItems.length} notes` : "Standing by", `Writes the text and links replies \u00b7 ${held} held back`],
    ["out", "chat", "iMessage", `${sent} sent \u00b7 ${drafted} drafted`, S.sendAlerts ? "Texting is on" : "Texting is off (drafts only)"],
  ];
  const html = nodes.map(([k, ic, name, a, b], n) => `${n ? `<div class="link ${stateOf()?.status === "running" ? "run" : ""}" aria-hidden="true"><i></i></div>` : ""}<div class="node ${flash(k)}"><span class="nic">${icon(ic, 20)}</span><h3>${name}</h3><p class="l1">${a}</p><p class="l2">${b}</p></div>`).join("");
  S.lastCounts = counts;
  const el = $("#pipe");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

function renderChips() {
  const html = GROUPS.map(([id, name]) => `<button class="chip" data-group="${id}" aria-pressed="${S.group === id}">${name}</button>`).join("");
  const el = $("#chips");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

function cardData(key) {
  const m = METRICS[key];
  const rev = S.monitor?.review || {};
  const out = { value: null, deltaText: "", deltaCls: "", spark: "", sub: m.hint };
  const from = Math.max(0, S.lastI - 191);
  const mk = m.keys[0];
  if (key === "sleep") {
    const n = nights();
    const last = n[n.length - 1];
    out.value = last ? fmt(last.hours, 1) : "\u2013";
    const recent = n.slice(-7);
    out.spark = recent.length > 1 ? bars({ values: recent.map((x) => x.hours), color: m.color }) : "";
    if (recent.length > 1) { const avg = recent.reduce((s, x) => s + x.hours, 0) / recent.length; out.deltaText = `${fmt(avg, 1)} h average`; out.deltaCls = "flat"; }
    out.sub = last ? `Night ending ${dayLabel(last.end)}` : "No completed night yet";
    return out;
  }
  if (key === "steps") {
    out.value = S.lastI >= 0 ? fmt(stepsToday()) : "\u2013";
    const hours = [];
    for (let h = 23; h >= 0; h--) { let sum = 0, any = false; for (let k = 0; k < 4; k++) { const t = S.ticks[S.lastI - h * 4 - k]; if (t) { sum += t.v.steps || 0; any = true; } } hours.push(any ? sum : null); }
    out.spark = bars({ values: hours, color: m.color });
    out.sub = "Hourly steps, last 24 h";
    return out;
  }
  const vals = [];
  const band = [];
  const marks = [];
  for (let i = from; i <= S.lastI; i++) {
    vals.push(valueAt(i, mk));
    const b = bandAt(mk, i);
    band.push(b ? { lo: b.lo, hi: b.hi } : null);
  }
  S.alerts.filter((a) => alertCard(a) === key && TEXTED.has(a.status)).forEach((a) => { const i = tsToIndex(a.ts); if (i != null && i >= from && i <= S.lastI) marks.push(i - from); });
  out.spark = sparkline({ values: vals, band: band.some(Boolean) ? band : null, color: m.color, marks, id: key });
  let latest = null;
  for (let i = S.lastI; i >= Math.max(0, S.lastI - 8); i--) { const v = valueAt(i, mk); if (v != null) { latest = i; break; } }
  if (latest != null) {
    if (key === "bp") out.value = `${fmt(valueAt(latest, "bp_sys"))}/${fmt(valueAt(latest, "bp_dia"))}`;
    else out.value = fmt(valueAt(latest, mk), m.dec);
  } else out.value = S.lastI >= 0 ? "No signal" : "\u2013";
  const o = rev[mk];
  if (o && o.median) {
    const diff = o.value - o.median;
    const absolute = mk === "skin_temp" || mk === "spo2";
    const pct = (diff / o.median) * 100;
    const small = absolute ? Math.abs(diff) < 0.05 : Math.abs(pct) < 0.5;
    out.deltaText = small ? "At your usual" : absolute ? `${sign(diff, 1)} ${mk === "spo2" ? "pts" : "\u00b0C"} vs usual` : `${sign(pct, 1)}% vs usual`;
    out.deltaCls = small ? "flat" : diff > 0 ? "up" : "down";
    out.z = o.z;
  }
  return out;
}

function renderCards() {
  const list = Object.entries(METRICS).filter(([, m]) => S.group === "all" || m.group === S.group);
  const html = list.map(([key, m]) => {
    const d = cardData(key);
    const st = cardStatus(key);
    const unit = key === "steps" ? "steps today" : m.unit;
    return `<button class="mcard" data-m="${key}" aria-label="${esc(m.label)} details" style="--c:${m.color}">
      <span class="mc-top"><span class="mc-ic">${icon(m.icon, 18)}</span><span class="mc-title">${m.label}${m.extra ? `<em class="xtag" title="Not a field of the WHOOP API. Added as a synthetic channel for this demo.">extra signal</em>` : ""}</span><span class="badge ${st.cls}">${st.text}</span></span>
      <span class="mc-val"><b>${d.value}</b><i>${d.value === "No signal" || d.value === "\u2013" ? "" : unit}</i></span>
      <span class="mc-sub">${d.deltaText ? `<span class="delta ${d.deltaCls}">${d.deltaText}</span>` : ""}<span class="mut">${esc(d.sub)}</span></span>
      <span class="mc-spark">${d.spark || '<span class="mut">Waiting for data</span>'}</span>
    </button>`;
  }).join("");
  const el = $("#cards");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

// ---------------------------------------------------------------- alerts
const STATUS = {
  sent: ["Texted", "ok"], discussed: ["Replied", "dark"], resolved: ["Resolved", "ok"], dry_run: ["Drafted, not sent", "mute"],
  suppressed: ["Held back", "mute"], watching: ["Watching", "warn"], ignored: ["Ignored", "mute"], data_issue: ["Data issue", "mute"], send_failed: ["Send failed", "bad"],
};

function evidenceLine(a) {
  const e = a.evidence || {};
  if (e.kind === "gap") return `No usable samples for about ${e.persistence_h} hours`;
  const m = e.metric;
  const unit = { sleep_hours: "h", rhr_sleep: "bpm", hrv_sleep: "ms" }[m] || METRICS[CARD_OF[m]]?.unit || "";
  const abs = m === "skin_temp" || m === "spo2";
  const diff = abs ? `${sign(e.value - e.baseline, 1)} ${m === "spo2" ? "pts" : "\u00b0C"}` : `${sign(e.pct, 1)}%`;
  return `${fmt(e.value, 1)} ${unit} vs usual ${fmt(e.baseline, 1)} \u00b7 ${diff} \u00b7 ${sign(e.z, 1)}\u03c3 \u00b7 ${e.persistence_h} h${e.ongoing ? " and continuing" : ""}`;
}

function decisionLine(a) {
  const d = a.decision || {};
  if (a.status === "suppressed") return `<span class="who policy">Policy</span> ${esc(d.reason || "Held back by the alert rules")}`;
  if (d.source === "jev") {
    const act = { alert_now: "ask now", watch: "keep watching", ignore: "ignore", data_issue: "data problem" }[d.action] || d.action;
    return `<span class="who jev">Jev</span> ${act} \u00b7 p(alert) ${fmt(d.p_alert, 2)} \u00b7 confidence ${fmt(d.confidence, 2)}${d.floor ? ' \u00b7 <b>safety floor applied</b>' : ""}`;
  }
  if (d.source === "rules") return `<span class="who policy">Rules</span> ${d.floor ? "very large sustained change, safety floor" : d.action === "alert_now" ? "large and sustained" : "not strong enough"}${a.kind === "gap" ? "" : ""}`;
  return "";
}

function checkText(c) {
  const f = c.factor;
  if (f === "pattern") return c.metrics_checked ? `${c.metrics_in_direction} of ${c.metrics_checked} related signals moved the same way` : null;
  const name = { workout: "Workout", poor_sleep: "Poor sleep", caffeine: "Caffeine", alcohol: "Alcohol", stress: "Stress", illness: "Illness", travel: "Travel", medication: "Medication", device_fit: "Sensor fit", hydration: "Hydration", other: "Other" }[f] || f;
  switch (c.verdict) {
    case "within_typical_range": return `${name}: change is within what your workouts usually produce (typical ${sign(c.typical_shift, 1)}, seen ${sign(c.observed_shift, 1)})`;
    case "larger_than_typical": return `${name}: bigger than your workouts usually produce (typical ${sign(c.typical_shift, 1)}, seen ${sign(c.observed_shift, 1)})`;
    case "smaller_than_typical": return `${name}: smaller than your workouts usually produce`;
    case "weak_relationship": return `${name}: sleep length hasn't predicted this signal for you (r = ${fmt(c.r, 2)})`;
    case "within_predicted_range": return `${name}: change is close to what last night's sleep predicts`;
    case "larger_than_predicted": return `${name}: bigger than last night's sleep predicts (predicted ${sign(c.predicted_shift, 1)}, seen ${sign(c.observed_shift, 1)})`;
    case "insufficient_history": return `${name}: not enough earlier history to compare`;
    case "not_checkable": return `${name}: ${c.note || "can't be checked for this signal"}`;
    case "not_measurable": return `${name}: the tracker has no channel that can confirm this`;
    default: return null;
  }
}

function alertCardHtml(a) {
  const [stText, stCls] = STATUS[a.status] || [a.status, "mute"];
  const label = ALERT_LABELS[a.metric] || a.metric;
  const msgs = S.thread.filter((t) => t.alert_id === a.id);
  const note = S.notes.find((n) => n.alert_id === a.id);
  const bubbles = (msgs.length ? msgs : a.text ? [{ direction: "out", text: a.text }] : []).map((m) => `<div class="bub ${m.direction === "in" ? "in" : "out"}">${esc(m.text)}</div>`).join("");
  let annotation = "";
  if (note) {
    const chips = (note.factors || []).map((f) => `<span class="fchip">${esc(f.replace(/_/g, " "))}</span>`).join("");
    const checks = (note.computed || []).map(checkText).filter(Boolean).map((t) => `<li>${esc(t)}</li>`).join("");
    annotation = `<div class="note"><div class="nh">${icon("chat", 16)}<b>Linked to the chart</b></div><p>${esc(note.summary)}</p>${chips ? `<div class="fchips">${chips}</div>` : ""}${checks ? `<ul class="checks">${checks}</ul><p class="fine">These checks compare against this person's own history. They describe the data and do not show what caused it.</p>` : ""}</div>`;
  }
  return `<article class="acard ${S.selectedAlert === a.id ? "sel" : ""}" id="alert-${a.id}" data-alert="${a.id}">
    <header><span class="sev ${a.status === "sent" || a.status === "discussed" ? "hot" : ""}"></span>
      <div class="ah"><h4>${esc(label)}</h4><p>${when(a.ts)} <span class="mut">simulated</span></p></div>
      <span class="status ${stCls}">${stText}</span></header>
    <p class="ev">${esc(evidenceLine(a))}</p>
    ${bubbles ? `<div class="thread">${bubbles}</div>` : ""}
    <p class="dec">${decisionLine(a)}</p>
    ${annotation}</article>`;
}

function renderAlerts() {
  const texted = S.alerts.filter((a) => a.text && TEXTED.has(a.status)).sort((x, y) => y.id - x.id);
  const held = S.alerts.filter((a) => !(a.text && TEXTED.has(a.status))).sort((x, y) => y.id - x.id);
  const p = PERSONAS[S.persona];
  const empty = `<div class="empty-state"><span class="ei">${icon("shield", 26)}</span><h4>${S.lastI < 0 ? "Nothing here yet" : `All quiet for ${p.name}`}</h4><p>${S.lastI < 0 ? "Start the replay and watch the monitor learn what normal looks like." : "The monitor is comparing every signal with this person's own usual range. Alerts appear here when something moves far and stays there."}</p></div>`;
  const heldHtml = held.length ? `<details class="held"><summary>Considered but not texted <span>${held.length}</span></summary>${held.map((a) => `<div class="hrow"><span class="status ${(STATUS[a.status] || [0, "mute"])[1]}">${(STATUS[a.status] || [a.status])[0]}</span><b>${esc(ALERT_LABELS[a.metric] || a.metric)}</b><span class="mut">${when(a.ts)}</span><p class="dec">${decisionLine(a)}</p></div>`).join("")}</details>` : "";
  const html = `${texted.length ? texted.map(alertCardHtml).join("") : empty}${heldHtml}`;
  const el = $("#alerts");
  const open = el.querySelector("details.held")?.open;
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; if (open) el.querySelector("details.held")?.setAttribute("open", ""); }
  $("#alert-count").textContent = texted.length ? String(texted.length) : "";
}

function renderSide() {
  // phone: the thread of the selected (or latest) alert
  const withThread = [...S.alerts].filter((a) => S.thread.some((t) => t.alert_id === a.id)).sort((x, y) => y.id - x.id);
  const pick = S.alerts.find((a) => a.id === S.selectedAlert) || withThread[0];
  const msgs = pick ? S.thread.filter((t) => t.alert_id === pick.id) : [];
  const bubbles = msgs.length ? msgs.map((m) => `<div class="bub ${m.direction === "in" ? "in" : "out"}">${esc(m.text)}</div>`).join("") : `<div class="phone-empty">Texts will appear here as they are sent and answered.</div>`;
  const phone = `<div class="phone"><div class="ph-head"><span class="ph-av">${icon("pulse", 16)}</span><div><b>Signal2Care</b><small>iMessage \u00b7 demo line</small></div></div><div class="ph-body">${bubbles}</div></div>`;
  const pel = $("#phone");
  if (pel.dataset.k !== phone) { pel.dataset.k = phone; pel.innerHTML = phone; const b = pel.querySelector(".ph-body"); if (b) b.scrollTop = b.scrollHeight; }

  // activity feed
  let items = [...S.feed].reverse();
  if (S.hideRoutine) items = items.filter((f) => f.kind !== "review");
  items = items.slice(0, 40);
  const who = { jev: ["Jev", "jev"], llm: ["Language model", "llm"], policy: ["Policy", "policy"], alert: ["iMessage", "out"], monitor: ["Monitor", "mon"] };
  const html = items.map((f) => {
    const [name, cls] = who[f.actor] || [f.actor, "policy"];
    return `<li class="fi"><span class="who ${cls}">${name}</span><p>${esc(f.text)}</p><time>${when(f.ts, true)}</time></li>`;
  }).join("") || `<li class="fi none"><p>Nothing yet. Every hour of replayed data is reviewed here.</p></li>`;
  const el = $("#feed");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
  $("#routine").checked = S.hideRoutine;
}

function renderFoot() {
  const h = S.brain.health;
  const bits = [];
  if (h) {
    bits.push(`Text model <b>${esc((h.text_model || "").split("/").pop())}</b>`, `Decisions <b>${h.jev ? esc((h.jev_model || "").split("/").pop()) : "rules only"}</b>`, `Documents <b>${esc(h.ocr || "")}</b>`);
    if (h.cloud) bits.push(`${fmt(h.cloud.pushed)} records synced`);
  }
  const html = `<div class="foot-l"><p><b>Signal2Care</b> is a hackathon demo. Every reading is synthetic, nothing here is medical advice, and it cannot diagnose anything. In an emergency call 911.</p>
    ${bits.length ? `<p class="sys">${bits.join(" \u00b7 ")}</p>` : ""}</div>
    <div class="foot-r">${S.conn ? '<button class="linkbtn" id="disconnect">Forget this browser</button>' : ""}</div>`;
  const el = $("#foot-inner");
  if (el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
}

// ---------------------------------------------------------------- drawer
function renderDrawer(refresh = false) {
  const key = S.openMetric;
  const m = METRICS[key];
  const dr = $("#drawer");
  if (!m) return;
  const body = $("#dr-body");
  const keepHover = $("#dchart")?.dataset.hover === "1";
  if (refresh && keepHover) return;
  const mk = m.keys[0];
  const alertsHere = S.alerts.filter((a) => alertCard(a) === key && TEXTED.has(a.status));
  const days = { "24h": 1, "3d": 3, "7d": 7, all: 28 }[S.range];
  const i1 = Math.max(0, S.lastI);
  const i0 = Math.max(0, i1 - days * TICKS_PER_DAY + 1);

  let stats = "";
  if (mk && mk !== "steps") {
    const vals = [];
    for (let i = Math.max(0, S.lastI - 95); i <= S.lastI; i++) { const v = valueAt(i, mk); if (v != null) vals.push(v); }
    const o = S.monitor?.review?.[mk];
    const dec = m.dec;
    stats = `<div class="dstats">
      <div><small>Latest</small><b>${vals.length ? (key === "bp" ? `${fmt(valueAt(S.lastI, "bp_sys") ?? 0)}/${fmt(valueAt(S.lastI, "bp_dia") ?? 0)}` : fmt(vals[vals.length - 1], dec)) : "\u2013"}</b><i>${m.unit}</i></div>
      <div><small>Usual now</small><b>${o ? fmt(o.median, dec) : "\u2013"}</b><i>${o ? m.unit : "learning"}</i></div>
      <div><small>Last 24 h range</small><b>${vals.length ? `${fmt(Math.min(...vals), dec)}\u2013${fmt(Math.max(...vals), dec)}` : "\u2013"}</b><i>${m.unit}</i></div>
      <div><small>Vs own baseline</small><b>${o ? sign(o.z, 1) + "\u03c3" : "\u2013"}</b><i>${o ? (Math.abs(o.z) < 2.5 ? "normal" : "unusual") : ""}</i></div></div>`;
  }
  const head = `<div class="dr-head"><span class="mc-ic big" style="--c:${m.color}">${icon(m.icon, 24)}</span><div><h2 id="dr-title">${m.label}${m.extra ? `<em class="xtag">extra signal</em>` : ""}</h2><p>${esc(m.long || m.hint)} \u00b7 ${PERSONAS[S.persona].name}</p></div><button class="x" id="dr-close" aria-label="Close details">${icon("close", 20)}</button></div>`;
  const ranges = `<div class="seg ranges" role="group" aria-label="Time range">${[["24h", "24 hours"], ["3d", "3 days"], ["7d", "7 days"], ["all", "Everything"]].map(([v, n]) => `<button data-range="${v}" aria-pressed="${S.range === v}">${n}</button>`).join("")}</div>`;
  const legend = `<div class="legend"><span><i class="sw band"></i>Usual range for the hour</span><span><i class="sw sleep"></i>Asleep</span><span><i class="sw dot"></i>Alert</span><span><i class="sw dot dark"></i>Alert with a reply</span></div>`;
  const listing = alertsHere.length ? `<h3 class="sub">Alerts on this signal</h3>${alertsHere.sort((a, b) => b.id - a.id).map((a) => `<button class="arow" data-alert="${a.id}"><span class="status ${(STATUS[a.status] || [0, "mute"])[1]}">${(STATUS[a.status] || [a.status])[0]}</span><b>${when(a.ts)}</b><span class="mut">${esc(evidenceLine(a))}</span></button>`).join("")}` : `<p class="mut dr-none">No alerts on this signal in the replay so far.</p>`;
  const note = m.extra ? `<p class="fine">${m.label} is an added synthetic channel. It is not one of the fields WHOOP's API provides, and it is here to show how the monitor treats extra sensors.</p>` : "";
  const hasChart = key !== "sleep" && key !== "steps";
  if (!refresh || dr.dataset.key !== `${key}|${S.range}`) {
    body.innerHTML = `${head}${stats}<div class="rangebar">${hasChart ? ranges : ""}</div><div id="dchart" class="chartbox"></div>${hasChart ? legend : ""}${listing}${note}`;
    dr.dataset.key = `${key}|${S.range}`;
  } else {
    const st = body.querySelector(".dstats");
    if (st && stats) st.outerHTML = stats;
  }
  const box = $("#dchart");
  if (!box) return;
  if (key === "sleep") { drawSleepList(box); return; }
  if (key === "steps") { drawSteps(box, i0, i1); return; }
  const lines = m.keys.map((k, n) => ({ key: k, label: n ? "Diastolic" : key === "bp" ? "Systolic" : m.label, color: n ? "#F4A58E" : m.color, get: (i) => valueAt(i, k) }));
  detailChart(box, {
    lines, i0, i1, unit: m.unit, dec: m.dec, aria: `${m.label} over ${S.range}`,
    band: (i) => { const b = bandAt(mk, i); return b ? { lo: b.lo, hi: b.hi } : null; },
    state: (i) => S.ticks[i]?.state,
    timeLabel: (i) => `${when(S.ticks[i]?.ts || "")}`,
    pins: alertsHere.map((a) => ({ id: a.id, i0: tsToIndex(a.started) ?? tsToIndex(a.ts), i1: tsToIndex(a.ts), label: `${ALERT_LABELS[a.metric]} alert, ${when(a.ts)}`, annotated: S.notes.some((n) => n.alert_id === a.id) })),
  });
}

function drawSleepList(box) {
  const n = nights().slice(-14).reverse();
  box.innerHTML = n.length ? `<div class="nights">${n.map((x) => `<div class="nrow"><span>${dayLabel(x.end)}</span><div class="nbar"><i style="width:${Math.min(100, (x.hours / 10) * 100)}%"></i></div><b>${fmt(x.hours, 1)} h</b></div>`).join("")}</div>` : `<p class="empty">No completed night yet.</p>`;
}

function drawSteps(box, i0, i1) {
  const vals = [];
  for (let h = Math.max(0, Math.floor(i0 / 4)); h <= Math.floor(i1 / 4); h++) { let s = 0; for (let k = 0; k < 4; k++) s += S.ticks[h * 4 + k]?.v.steps || 0; vals.push(s); }
  box.innerHTML = `<div class="stepbars">${bars({ values: vals, color: METRICS.steps.color, w: 640, h: 220 })}</div><p class="fine">Steps per hour across the selected range.</p>`;
}

function openDrawer(key) {
  S.openMetric = key;
  const dr = $("#drawer");
  dr.dataset.key = "";
  dr.hidden = false;
  document.body.classList.add("lock");
  requestAnimationFrame(() => dr.classList.add("open"));
  renderDrawer();
  $("#dr-close")?.focus();
}
function closeDrawer() {
  const dr = $("#drawer");
  dr.classList.remove("open");
  document.body.classList.remove("lock");
  const key = S.openMetric;
  S.openMetric = null;
  setTimeout(() => { if (!S.openMetric) dr.hidden = true; }, 220);
  $(`.mcard[data-m="${key}"]`)?.focus();
}

// ---------------------------------------------------------------- actions
function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.classList.add("out"), 3800);
  setTimeout(() => el.remove(), 4300);
}

function pulse(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("ping");
  void el.offsetWidth;
  el.classList.add("ping");
}

async function send(type, args = {}, persona = S.persona) {
  if (S.preview || !S.conn) { toast('This is a recorded run. Text "demo link" to the Signal2Care number to control a live one.'); return; }
  try {
    await call(S.conn, "/api/command", { method: "POST", body: { type, persona, args } });
    toast("Sent to your monitor\u2026");
    schedule(600);
  } catch (e) {
    toast(e instanceof AuthError ? "This link is no longer valid." : "Couldn't send that. Check your connection.", "bad");
  }
}

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("button, [data-alert], .pin, input#sendalerts, a");
  if (!t) return;
  if (t.dataset.persona) { switchPersona(t.dataset.persona); return; }
  if (t.dataset.group) { S.group = t.dataset.group; renderChips(); renderCards(); return; }
  if (t.classList.contains("mcard")) { openDrawer(t.dataset.m); return; }
  if (t.id === "dr-close") { closeDrawer(); return; }
  if (t.dataset.range) { S.range = t.dataset.range; renderDrawer(); return; }
  if (t.id === "play") {
    const st = stateOf();
    if (st?.status === "running") send("stop");
    else {
      const sel = $("#startday");
      const fresh = !st || !st.cursor || st.status === "finished";
      send("play", fresh ? { start_day: Number(sel?.value ?? S.startDay) } : {});
    }
    return;
  }
  if (t.id === "reset") { send("reset"); return; }
  if (t.dataset.speed) { send("speed", { speed: Number(t.dataset.speed) }); return; }
  if (t.id === "disconnect") { forgetConn(); location.reload(); return; }
  const aid = t.dataset.alert || t.closest("[data-alert]")?.dataset.alert;
  if (aid) { focusAlert(Number(aid)); }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && S.openMetric) closeDrawer();
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.classList?.contains("pin")) { ev.preventDefault(); focusAlert(Number(ev.target.dataset.alert)); }
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "sendalerts") send("send_alerts", { value: ev.target.checked }, undefined);
  if (ev.target.id === "routine") { S.hideRoutine = ev.target.checked; renderSide(); }
  if (ev.target.id === "startday") S.startDay = Number(ev.target.value);
});

function focusAlert(id) {
  S.selectedAlert = id;
  if (S.openMetric) closeDrawer();
  renderAlerts();
  renderSide();
  const el = document.getElementById(`alert-${id}`);
  el?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
}

function switchPersona(id) {
  if (id === S.persona) return;
  S.persona = id;
  S.selectedAlert = null;
  resetLocal();
  for (const el of document.querySelectorAll("[data-k]")) delete el.dataset.k;
  render();
  poll();
}

// ------------------------------------------------------------------ boot
async function boot() {
  S.conn = loadConn();
  if (S.conn) { render(); poll(); return; }
  try {
    const r = await fetch("sample.json", { cache: "no-store" });
    if (!r.ok) throw new Error("no sample");
    const sample = await r.json();
    S.preview = true;
    S.persona = sample.persona || "maya";
    S.states = sample.states;
    apply({ ...sample, brain: { online: false } });
    S.brain = { online: false, health: sample.health };
    S.states = sample.states;
  } catch {
    S.preview = true;
  }
  render();
}
$("#drawer-scrim").addEventListener("click", closeDrawer);
boot();
