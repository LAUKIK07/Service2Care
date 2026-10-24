import { ALERT_LABELS, CARD_OF, GROUPS, METRICS, PERSONAS as BUILTIN, TICKS_PER_DAY, clock, dayLabel, esc, fmt, humanize, icon, localClock, median, robustSpread, safeHue, sign, tsToIndex, when } from "./format.js";
import { bars, detailChart, sparkline } from "./charts.js";
import { AuthError, HttpError, call, forgetConn, loadConn } from "./api.js";
import { CARD_STATUS, alertCardHtml, conversationHtml, consideredRowHtml, evidenceLine, isHeld, labelOf, recordHtml, statusInfo, tickCountdowns, timelineHtml } from "./story.js";

const $ = (sel, root = document) => root.querySelector(sel);
const TOTAL = 28 * TICKS_PER_DAY;
const SPEEDS = [[1, "Slow", "one simulated day in about 96 seconds"], [4, "Normal", "about 24 seconds per day"], [12, "Fast", "about 8 seconds per day"], [40, "Turbo", "about 2.4 seconds per day"]];
const RECORD_KEY = "s2c.record.hidden";
const query = new URLSearchParams(location.search);       // ?expand=receipt  ?goto=alert-101  ?mini=1  ?metric=skin_temp&range=all (for sharing and testing)

const readFlag = (k) => { try { return localStorage.getItem(k) === "1"; } catch { return false; } };

const S = {
  conn: null, preview: false, persona: "maya", personas: [], linked: null, profile: null,
  ticks: [], lastI: -1, feed: [], lastFeed: 0,
  alerts: [], notes: [], thread: [], monitor: null, rev: -1,
  states: {}, brain: { online: false }, sendAlerts: false,
  group: "all", openMetric: null, range: ["24h", "3d", "7d", "all"].includes(query.get("range")) ? query.get("range") : "3d", selectedAlert: null,
  startDay: 16, hideRoutine: true, seenCmd: new Map(), loadedAt: Date.now(), error: null, timer: null, polling: false,
  bandCache: new Map(), lastCounts: {}, cardCache: new Map(),
  open: new Set(), recordHidden: readFlag(RECORD_KEY), past: query.get("mini") === "1", pendingGoto: query.get("goto"),
};

// ------------------------------------------------------------- the people
// The list comes from the service (it grows when a real dataset is added). Old payloads and preview mode use the built-in three.
const personaList = () => (S.personas.length
  ? S.personas
  : Object.entries(BUILTIN).map(([id, p]) => ({ id, ...p, kind: "synthetic", source: "Seeded physiology simulator", license: "Invented", citation: "", channels: [] })));
const pinfo = (id = S.persona) => personaList().find((p) => p.id === id) || { id, name: id, tag: "", blurb: "", hue: "#6A6A6A", kind: "synthetic", channels: [] };
const initial = (p) => esc((p.name || p.id || "?").trim().slice(0, 1).toUpperCase());
const kindTip = (p) => `${p.kind === "real" ? "Real data" : "Simulated"}${p.source ? ` \u00b7 Source: ${p.source}` : ""}${p.license ? ` \u00b7 License: ${p.license}` : ""}`;
const hasReal = () => personaList().some((p) => p.kind === "real");

function visibleMetrics() {
  const ch = pinfo().channels || [];
  const all = Object.entries(METRICS);
  if (!ch.length) return all;
  const keep = all.filter(([k, m]) => k === "sleep" || m.keys.some((x) => ch.includes(x)));
  return keep.length >= 3 ? keep : all;           // a channel list that matches nothing must not blank the page
}

// ------------------------------------------------------------------ data
function resetLocal() {
  Object.assign(S, { ticks: [], lastI: -1, feed: [], lastFeed: 0, alerts: [], notes: [], thread: [], monitor: null, profile: null, rev: -1, bandCache: new Map(), cardCache: new Map() });
  $("#alerts")?.replaceChildren();
}

function apply(d) {
  if (Array.isArray(d.personas) && d.personas.length) S.personas = d.personas;
  if (d.linked !== undefined) S.linked = d.linked;
  // The run was reset or restarted: the service now holds fewer readings than we do. Start over from zero.
  const epochChanged = d.epoch !== undefined && S.epoch != null && d.epoch !== S.epoch;
  if (d.epoch !== undefined) S.epoch = d.epoch;
  if (epochChanged || (d.max_i !== undefined && S.lastI >= 0 && (d.max_i === null || d.max_i < S.lastI))) {
    resetLocal();
    S.states = d.states || S.states;
    S.refetch = true;
    return;
  }
  for (const r of d.series || []) { S.ticks[r.i] = r; if (r.i > S.lastI) S.lastI = r.i; }
  for (const f of d.feed || []) { if (f.id > S.lastFeed) { S.feed.push(f); S.lastFeed = f.id; } }
  if (S.feed.length > 400) S.feed = S.feed.slice(-300);
  if (d.alerts) {
    S.alerts = d.alerts; S.notes = d.annotations || []; S.thread = d.thread || []; S.monitor = d.monitor;
    S.profile = d.profile || null; S.rev = d.rev;
    if (query.get("expand")) for (const a of S.alerts) S.open.add(`r-${a.id}`);
  }
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

function dropToPerson(list, linked) {
  if (Array.isArray(list) && list.length) S.personas = list;
  S.linked = linked ?? S.linked;
  const next = S.linked && S.personas.some((p) => p.id === S.linked) ? S.linked : S.personas[0]?.id;
  if (!next || next === S.persona) return false;
  S.persona = next;
  S.selectedAlert = null;
  resetLocal();
  for (const el of document.querySelectorAll("[data-k]")) delete el.dataset.k;
  return true;
}

async function poll() {
  if (S.polling || S.preview || !S.conn) return;
  S.polling = true;
  try {
    const q = new URLSearchParams({ persona: S.persona, after_i: S.lastI, after_feed: S.lastFeed, rev: S.rev });
    const d = await call(S.conn, `/api/live?${q}`);
    S.error = null;
    const before = S.alerts.length;
    apply(d);
    if (S.persona && S.personas.length && !S.personas.some((p) => p.id === S.persona) && dropToPerson(S.personas, S.linked)) S.refetch = true;
    render();
    if (S.alerts.length > before && before >= 0) pulse("alert-count");
  } catch (e) {
    if (e instanceof HttpError && e.status === 400 && e.body?.error === "unknown persona" && dropToPerson(e.body.personas, e.body.linked)) {
      S.refetch = true;                // the person we asked for is no longer in the list: continue with another one
      render();
    } else {
      S.error = e instanceof AuthError ? "auth" : "network";
      renderBanner();
    }
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
document.addEventListener("visibilitychange", () => { if (!document.hidden) { schedule(50); tickCountdowns(); } });

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
  // A night is a stretch of sleep ticks; awakenings of up to two hours inside it do not end the night.
  const out = [];
  let asleep = 0, last = -1;
  const close = () => { if (asleep >= 12) out.push({ end: last, hours: asleep / 4 }); asleep = 0; };
  for (let i = 0; i <= S.lastI; i++) {
    const t = S.ticks[i];
    if (t && t.state === "sleep") { if (asleep && i - last > 9) close(); asleep++; last = i; }
    else if (asleep && i - last > 9) close();
  }
  return out;                       // a night still in progress is not counted
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
const HELD_BACK = new Set(["suppressed", "watching", "ignored", "data_issue", "digested", "cancelled", "expired"]);

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
// Replace an element's HTML only when the HTML changed, so polling never rebuilds what the person is looking at.
function put(sel, html) {
  const el = $(sel);
  if (el && el.dataset.k !== html) { el.dataset.k = html; el.innerHTML = html; }
  return el;
}

function render() {
  renderBanner();
  renderTop();
  renderHero();
  renderMini();
  renderPipeline();
  renderChips();
  renderCards();
  renderTimeline();
  renderRecord();
  renderAlerts();
  renderSide();
  renderFoot();
  if (S.openMetric) renderDrawer(true);
  jumpIfAsked();
}

function renderBanner() {
  let html = "";
  if (S.preview) html = `<div class="banner info">${icon("info", 18)}<div><b>You're viewing a recorded demo run.</b> It used the real monitor, Jev and language model, but no texts were actually sent and the two replies were scripted. To control a live run, text <code>demo link</code> to the Signal2Care number and open the link it sends back.</div></div>`;
  else if (!S.conn) html = "";
  else if (S.error === "auth") html = `<div class="banner bad">${icon("shield", 18)}<div><b>This link is no longer valid.</b> Text <code>demo link</code> to the Signal2Care number to get a fresh one.</div></div>`;
  else if (S.error === "network") html = `<div class="banner warn">${icon("info", 18)}<div><b>Can't reach the dashboard service.</b> Retrying automatically.</div></div>`;
  else if (!S.brain.online) html = `<div class="banner warn">${icon("chip", 18)}<div><b>The monitor isn't running.</b> Data below is the last saved state. Start it on your Mac with <code>scripts/serve.sh</code>.</div></div>`;
  put("#banner", html);
}

function renderTop() {
  const tabs = personaList().map((p) => {
    const real = p.kind === "real";
    const linked = S.linked === p.id;
    const tip = `${kindTip(p)}${linked ? " \u00b7 Linked to the phone" : ""}`;
    return `<button type="button" role="tab" aria-selected="${p.id === S.persona}" data-persona="${esc(p.id)}" class="tab" title="${esc(tip)}" aria-label="${esc(`${p.name}${p.tag ? `, ${p.tag}` : ""}. ${tip}`)}">
      <span class="av" style="--c:${safeHue(p.hue)}">${initial(p)}</span>
      <span class="tn"><span class="tn1">${esc(p.name)}${linked ? `<span class="lk" aria-hidden="true">${icon("phone", 13)}</span>` : ""}</span>
        <span class="tn2"><small>${esc(p.tag || "")}</small><span class="kind ${real ? "real" : "sim"}">${real ? "Real data" : "Simulated"}</span></span></span></button>`;
  }).join("");
  const live = S.preview ? `<span class="pill muted"><i></i>Recorded</span>` : S.brain.online ? `<span class="pill live"><i></i>Monitor online</span>` : `<span class="pill muted"><i></i>Monitor offline</span>`;
  put("#top-inner", `<div class="brand"><span class="logo">${icon("pulse", 20)}</span><span class="bname"><span class="bn">Signal2Care</span><span class="tag">demo \u00b7 ${hasReal() ? "synthetic and public data" : "synthetic data"}</span></span></div>
    <nav class="tabs" role="tablist" aria-label="Choose a person">${tabs}</nav><div class="top-r">${live}</div>`);
}

function playLabel() {
  const st = stateOf();
  const running = st?.status === "running";
  const cursor = Math.max(st?.cursor ?? 0, S.lastI + 1);
  return { running, text: running ? "Pause" : cursor && st?.status !== "finished" ? "Resume" : "Start replay" };
}

function renderHero() {
  const p = pinfo();
  const st = stateOf();
  const t = lastTick();
  const status = st?.status || "idle";
  const day = Math.max(0, Math.floor(S.lastI / TICKS_PER_DAY));
  const cursor = Math.max(st?.cursor ?? 0, S.lastI + 1);
  const { running, text: playText } = playLabel();
  const simState = t?.state;
  const stateChip = simState ? `<span class="state ${esc(simState)}">${esc({ sleep: "Sleeping", awake: "Awake", active: "Exercising" }[simState] || simState)}</span>` : "";
  const speed = st?.speed ?? 4;
  const nearest = SPEEDS.reduce((a, b) => (Math.abs(b[0] - speed) < Math.abs(a[0] - speed) ? b : a));
  const dots = S.alerts.filter((a) => TEXTED.has(a.status)).map((a) => {
    const i = tsToIndex(a.ts);
    return i == null ? "" : `<button type="button" class="tdot ${a.status === "dry_run" ? "draft" : ""}" style="left:${(i / TOTAL) * 100}%" data-alert="${a.id}" aria-label="${esc(`${labelOf(a)} alert, ${when(a.ts)}`)}" title="${esc(`${labelOf(a)} \u00b7 ${when(a.ts)}`)}"></button>`;
  }).join("");
  const dayTicks = Array.from({ length: 27 }, (_, k) => `<i style="left:${((k + 1) / 28) * 100}%"></i>`).join("");
  const offline = !S.preview && !S.brain.online;
  const startOpts = Array.from({ length: 28 }, (_, d) => `<option value="${d}" ${d === S.startDay ? "selected" : ""}>Day ${d + 1} \u00b7 ${dayLabel(d * TICKS_PER_DAY)}</option>`).join("");
  const linkedName = S.linked ? pinfo(S.linked).name : null;
  const source = p.kind === "real" && (p.source || p.license)
    ? `<p class="src"><b>Real data.</b> ${p.source ? `Source: ${esc(p.source)}.` : ""} ${p.license ? `License: ${esc(p.license)}.` : ""} ${p.citation ? `<span class="cite">${esc(p.citation)}</span>` : ""}</p>`
    : "";
  put("#hero", `<div class="hero-main">
      <div class="who"><span class="av lg" style="--c:${safeHue(p.hue)}">${initial(p)}</span>
        <div><h1>${esc(p.name)}<small>${esc(p.tag || "")}</small><span class="kind ${p.kind === "real" ? "real" : "sim"}" title="${esc(kindTip(p))}">${p.kind === "real" ? "Real data" : "Simulated"}</span></h1><p>${esc(p.blurb || "")}</p>${source}</div></div>
      <div class="clockbox" aria-live="off">
        <div class="clock">${t ? clock(S.lastI) : "--:--"}</div>
        <div class="date">${t ? `${esc(when(t.ts).split(" \u00b7 ")[0])} \u00b7 day ${day + 1} of 28` : "Not started"}</div>${stateChip}
      </div></div>
    <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="${TOTAL}" aria-valuenow="${cursor}" aria-label="Replay progress"><div class="fill" style="width:${(cursor / TOTAL) * 100}%"></div>${dayTicks}${dots}</div>
    <div class="track-l"><span>${dayLabel(0)}</span><span>${running ? "Replaying" : status === "finished" ? "Finished" : cursor ? "Paused" : "Ready"}</span><span>${dayLabel(TOTAL - 1)}</span></div>
    <div class="controls">
      <button type="button" class="btn primary" id="play" data-act="play" ${offline ? "disabled" : ""}>${icon(running ? "pause" : "play", 18)}<span>${playText}</span></button>
      <button type="button" class="btn" id="reset" ${offline || !cursor ? "disabled" : ""}>${icon("reset", 18)}<span>Reset</span></button>
      <label class="field"><span>Start at</span><select id="startday" ${offline || running ? "disabled" : ""}>${startOpts}</select></label>
      <div class="seg" role="group" aria-label="Replay speed">${SPEEDS.map(([v, n, tip]) => `<button type="button" data-speed="${v}" aria-pressed="${nearest[0] === v}" title="${tip}" ${offline ? "disabled" : ""}>${n}</button>`).join("")}</div>
      <label class="switch" title="Off: alerts are drafted and shown here but nothing is texted"><input type="checkbox" id="sendalerts" ${S.sendAlerts ? "checked" : ""} ${offline ? "disabled" : ""}><span class="knob"></span><span class="sl">${icon("chat", 16)} Text me alerts</span></label>
    </div>
    <p class="hint">${S.sendAlerts ? "Detected changes are texted from the Signal2Care number. Reply there and your words land on the chart." : "Alert texts are off. Detected changes are drafted here and nothing is sent."}${linkedName && S.linked !== S.persona ? ` Only ${esc(linkedName)} is linked to a phone; other people's alerts stay on this page.` : ""}</p>`);
}

// The slim bar that follows the page once the hero has scrolled away.
function renderMini() {
  const p = pinfo();
  const t = lastTick();
  const { running, text } = playLabel();
  const offline = !S.preview && !S.brain.online;
  const day = Math.max(0, Math.floor(S.lastI / TICKS_PER_DAY));
  const online = S.preview ? `<span class="pill muted sm"><i></i>Recorded</span>` : S.brain.online ? `<span class="pill live sm"><i></i>Monitor online</span>` : `<span class="pill muted sm"><i></i>Offline</span>`;
  put("#mini-inner", `<span class="av sm" style="--c:${safeHue(p.hue)}">${initial(p)}</span><b class="mn">${esc(p.name)}</b>
    <span class="mclock" aria-label="Simulated time">${t ? clock(S.lastI) : "--:--"}</span><span class="mday mut">${t ? `day ${day + 1}` : ""}</span>
    <button type="button" class="btn sm ${running ? "" : "primary"}" data-act="play" ${offline ? "disabled" : ""}>${icon(running ? "pause" : "play", 16)}<span>${text}</span></button>${online}`);
  $("#minibar").hidden = !S.past;
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
  const held = S.alerts.filter((a) => HELD_BACK.has(a.status) || isHeld(a.status)).length;
  const counts = { stream: S.lastI, monitor: S.monitor?.ts, jev: jevItems.length, llm: llmItems.length, out: sent + drafted };
  const flash = (k) => (S.lastCounts[k] !== undefined && S.lastCounts[k] !== counts[k] ? "flash" : "");
  const topName = top ? `${METRICS[CARD_OF[top[0]]]?.label || ALERT_LABELS[top[0]] || top[0]}` : null;
  // [key, icon, name, headline, detail]. The detail is the tooltip and the screen-reader text; the strip itself stays one row.
  const nodes = [
    ["stream", "pulse", "Wearable", t ? `${fmt(S.lastI + 1)} readings` : "Waiting for data", t ? `${t.quality === "good" ? "Signal good" : "Sensor gap"} at ${clock(S.lastI)}. One reading every 15 minutes.` : "No readings yet"],
    ["monitor", "chip", "Monitor", top ? `${Object.keys(rev).length} signals checked` : "Learning normal", top ? `Largest move: ${topName} ${sign(top[1].z)}\u03c3` : "Needs about 5 days of history"],
    ["jev", "diamond", "Jev", jevItems.length ? `${jevItems.length} decisions` : "No decisions yet", jevLast ? jevLast.text.replace(/^Reviewed /, "") : "Judges whether a change is worth a text"],
    ["llm", "chat", "Language model", llmItems.length ? `${llmItems.length} notes` : "Standing by", `Writes the text and links replies. ${held} held back.`],
    ["out", "chat", "iMessage", `${sent} sent \u00b7 ${drafted} drafted`, S.sendAlerts ? "Texting is on" : "Texting is off (drafts only)"],
  ];
  const running = stateOf()?.status === "running";
  const html = nodes.map(([k, ic, name, a, b], n) => `${n ? `<div class="link ${running ? "run" : ""}" aria-hidden="true"><i></i></div>` : ""}<div class="node ${flash(k)}" title="${esc(b)}"><span class="nic">${icon(ic, 18)}</span><div class="nt"><h3>${esc(name)}</h3><p class="l1">${esc(a)}</p></div><span class="sr">${esc(b)}</span></div>`).join("");
  S.lastCounts = counts;
  put("#pipe", html);
}

function renderChips() {
  put("#chips", GROUPS.map(([id, name]) => `<button type="button" class="chip" data-group="${id}" aria-pressed="${S.group === id}">${name}</button>`).join(""));
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
  const list = visibleMetrics().filter(([, m]) => S.group === "all" || m.group === S.group);
  const html = list.map(([key, m]) => {
    const d = cardData(key);
    const st = cardStatus(key);
    const unit = key === "steps" ? "steps today" : m.unit;
    return `<button type="button" class="mcard" data-m="${key}" aria-label="${esc(m.label)} details" style="--c:${m.color}">
      <span class="mc-top"><span class="mc-ic">${icon(m.icon, 18)}</span><span class="mc-title">${m.label}</span><span class="badge ${st.cls}">${st.text}</span></span>
      <span class="mc-val"><b>${d.value}</b><i>${d.value === "No signal" || d.value === "\u2013" ? "" : unit}</i></span>
      <span class="mc-sub">${d.deltaText ? `<span class="delta ${d.deltaCls}">${d.deltaText}</span>` : ""}<span class="mut">${esc(d.sub)}</span>${m.extra ? `<em class="xtag" title="Not a field of the WHOOP API. Added as a synthetic channel for this demo.">extra signal</em>` : ""}</span>
      <span class="mc-spark">${d.spark || '<span class="mut">Waiting for data</span>'}</span>
    </button>`;
  }).join("");
  put("#cards", html);
}

// ---------------------------------------------------------------- stories
function renderTimeline() {
  const byId = new Map(S.alerts.map((a) => [a.id, a]));
  const html = S.notes.length
    ? timelineHtml(S.notes, byId, S.selectedAlert)
    : `<p class="empty-line">No stories yet. When someone answers a text, the conversation is kept here as a story attached to the alert.</p>`;
  const open = S.notes.filter((n) => n.status === "open").length;
  put("#timeline", html);
  $("#story-count").textContent = S.notes.length ? String(S.notes.length) : "";
  $("#story-open").textContent = open ? `${open} open` : "";
}

function renderRecord() {
  const el = $("#record");
  const items = S.profile?.items?.length;
  const show = !!items || (S.linked != null && S.linked === S.persona);       // an old service sends neither: leave the panel out
  el.hidden = !show;
  if (show) put("#record", recordHtml({ name: pinfo().name, profile: S.profile, hidden: S.recordHidden }));
}

// Keep a keyed list of elements in step with fresh HTML: unchanged items are left alone (scroll, focus, selection stay), changed ones are swapped.
function patchList(root, entries, cache) {
  const have = new Map([...root.children].map((c) => [c.dataset.key, c]));
  let prev = null;
  for (const { key, html } of entries) {
    let el = have.get(key);
    if (!el || cache.get(key) !== html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      const fresh = tpl.content.firstElementChild;
      fresh.dataset.key = key;
      if (el) el.replaceWith(fresh);
      el = fresh;
      cache.set(key, html);
    }
    const want = prev ? prev.nextElementSibling : root.firstElementChild;
    if (want !== el) root.insertBefore(el, want);
    prev = el;
    have.delete(key);
  }
  for (const el of have.values()) { cache.delete(el.dataset.key); el.remove(); }
}

function keepFocus(fn) {
  const fid = document.activeElement?.dataset?.fid;
  fn();
  if (!fid) return;
  const el = document.querySelector(`[data-fid="${CSS.escape(fid)}"]`);
  if (el && document.activeElement !== el) el.focus({ preventScroll: true });
}

function applyOpen(root) {
  for (const d of root.querySelectorAll("details[data-ok]")) if (S.open.has(d.dataset.ok) && !d.open) d.open = true;
}

function renderAlerts() {
  const byThread = new Map();
  for (const m of S.thread) (byThread.get(m.alert_id) || byThread.set(m.alert_id, []).get(m.alert_id)).push(m);
  const noteOf = new Map(S.notes.map((n) => [n.alert_id, n]));
  const p = pinfo();
  const isOpen = (a) => noteOf.get(a.id)?.status === "open";
  const sorted = [...S.alerts].sort((x, y) => (isOpen(y) - isOpen(x)) || y.id - x.id);       // a conversation still listening goes first
  const cards = sorted.filter((a) => CARD_STATUS.has(a.status));
  const held = sorted.filter((a) => !CARD_STATUS.has(a.status));
  const entries = cards.map((a) => ({ key: `a${a.id}`, html: alertCardHtml(a, { msgs: byThread.get(a.id) || [], note: noteOf.get(a.id) || null, selected: S.selectedAlert === a.id, persona: p }) }));
  if (!cards.length) {
    entries.push({ key: "empty", html: `<div class="empty-state"><span class="ei">${icon("shield", 26)}</span><h4>${S.lastI < 0 ? "Nothing here yet" : `All quiet for ${esc(p.name)}`}</h4><p>${S.lastI < 0 ? "Start the replay and watch the monitor learn what normal looks like." : "The monitor is comparing every signal with this person's own usual range. Alerts appear here when something moves far and stays there."}</p></div>` });
  }
  if (held.length) {
    entries.push({ key: "held", html: `<details class="held" data-ok="held"><summary data-fid="held-sum">Considered but not texted <span>${held.length}</span></summary>${held.map((a) => consideredRowHtml(a, p)).join("")}</details>` });
  }
  keepFocus(() => patchList($("#alerts"), entries, S.cardCache));
  applyOpen($("#alerts"));
  tickCountdowns($("#alerts"));
  $("#alert-count").textContent = cards.length ? String(cards.length) : "";
}

function renderSide() {
  // phone: the conversation of the selected (or latest) alert
  const withThread = [...S.alerts].filter((a) => S.thread.some((t) => t.alert_id === a.id)).sort((x, y) => y.id - x.id);
  const pick = S.alerts.find((a) => a.id === S.selectedAlert) || withThread[0];
  const msgs = pick ? S.thread.filter((t) => t.alert_id === pick.id) : [];
  const note = pick ? S.notes.find((n) => n.alert_id === pick.id) : null;
  const inner = pick && (msgs.length || pick.text) ? conversationHtml(pick, msgs, note) : "";
  const bubbles = inner || `<div class="phone-empty">Texts will appear here as they are sent and answered.</div>`;
  const phone = `<div class="phone"><div class="ph-head"><span class="ph-av">${icon("pulse", 16)}</span><div><b>Signal2Care</b><small>iMessage \u00b7 demo line${pick && pick.event_id ? ` \u00b7 ${esc(pick.event_id.slice(-26))}` : ""}</small></div></div><div class="ph-body" tabindex="0" role="log" aria-label="Messages for the selected alert">${bubbles}</div></div>`;
  const pel = $("#phone");
  if (pel.dataset.k !== phone) { pel.dataset.k = phone; pel.innerHTML = phone; const b = pel.querySelector(".ph-body"); if (b) b.scrollTop = b.scrollHeight; }

  // activity feed
  let items = [...S.feed].reverse();
  if (S.hideRoutine) items = items.filter((f) => f.kind !== "review");
  items = items.slice(0, 40);
  const who = { jev: ["Jev", "jev"], llm: ["Language model", "llm"], policy: ["Policy", "policy"], alert: ["iMessage", "out"], monitor: ["Monitor", "mon"] };
  const html = items.map((f) => {
    const [name, cls] = who[f.actor] || [humanize(f.actor), "policy"];
    return `<li class="fi"><span class="who ${cls}">${esc(name)}</span><p>${esc(f.text)}</p><time>${esc(when(f.ts, true))}</time></li>`;
  }).join("") || `<li class="fi none"><p>Nothing yet. Every hour of replayed data is reviewed here.</p></li>`;
  put("#feed", html);
  $("#routine").checked = S.hideRoutine;
}

function renderFoot() {
  const h = S.brain.health;
  const bits = [];
  if (h) {
    bits.push(`Text model <b>${esc((h.text_model || "").split("/").pop())}</b>`, `Decisions <b>${h.jev ? esc((h.jev_model || "").split("/").pop()) : "rules only"}</b>`, `Documents <b>${esc(h.ocr || "")}</b>`);
    if (h.cloud) bits.push(`${fmt(h.cloud.pushed)} records synced`);
  }
  put("#foot-inner", `<div class="foot-l"><p><b>Signal2Care</b> is a hackathon demo. ${hasReal() ? "Readings are either invented or taken from public research datasets, and they are not about anyone using this page." : "Every reading is synthetic."} Nothing here is medical advice, and it cannot diagnose anything. In an emergency call 911.</p>
    ${bits.length ? `<p class="sys">${bits.join(" \u00b7 ")}</p>` : ""}</div>
    <div class="foot-r">${S.conn ? '<button type="button" class="linkbtn" id="disconnect">Forget this browser</button>' : ""}</div>`);
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
  const head = `<div class="dr-head"><span class="mc-ic big" style="--c:${m.color}">${icon(m.icon, 24)}</span><div><h2 id="dr-title">${m.label}${m.extra ? `<em class="xtag">extra signal</em>` : ""}</h2><p>${esc(m.long || m.hint)} \u00b7 ${esc(pinfo().name)}</p></div><button type="button" class="x" id="dr-close" aria-label="Close details">${icon("close", 20)}</button></div>`;
  const ranges = `<div class="seg ranges" role="group" aria-label="Time range">${[["24h", "24 hours"], ["3d", "3 days"], ["7d", "7 days"], ["all", "Everything"]].map(([v, n]) => `<button type="button" data-range="${v}" aria-pressed="${S.range === v}">${n}</button>`).join("")}</div>`;
  const legend = `<div class="legend"><span><i class="sw band"></i>Usual range for the hour</span><span><i class="sw sleep"></i>Asleep</span><span><i class="sw dot"></i>Alert</span><span><i class="sw dot dark"></i>Alert with a conversation</span></div>`;
  const listing = alertsHere.length ? `<h3 class="sub">Alerts on this signal</h3>${alertsHere.sort((a, b) => b.id - a.id).map((a) => {
    const note = S.notes.find((n) => n.alert_id === a.id);
    return `<button type="button" class="arow" data-alert="${a.id}"><span class="status ${statusInfo(a.status)[1]}">${esc(statusInfo(a.status)[0])}</span><b>${esc(when(a.ts))}</b><span class="mut">${esc(note?.summary || evidenceLine(a))}</span></button>`;
  }).join("")}` : `<p class="mut dr-none">No alerts on this signal in the replay so far.</p>`;
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
  // The pins carry the story: its summary is part of the pin's label and appears in the hover note.
  const pins = alertsHere.map((a) => {
    const n = S.notes.find((x) => x.alert_id === a.id);
    const summary = n ? (n.summary || (n.status === "open" ? "Conversation open, listening for a reply." : "")) : "";
    return { id: a.id, i0: tsToIndex(a.started) ?? tsToIndex(a.ts), i1: tsToIndex(a.ts), annotated: !!n, summary,
      label: `${labelOf(a)} alert, ${when(a.ts)}${summary ? `. ${summary}` : ""}` };
  });
  detailChart(box, {
    lines, i0, i1, unit: m.unit, dec: m.dec, aria: `${m.label} over ${S.range}`,
    band: (i) => { const b = bandAt(mk, i); return b ? { lo: b.lo, hi: b.hi } : null; },
    state: (i) => S.ticks[i]?.state,
    timeLabel: (i) => `${when(S.ticks[i]?.ts || "")}`,
    pins,
    extraTip: (i) => { const p = pins.find((x) => x.summary && i >= x.i0 && i <= x.i1); return p ? `<div class="tipnote"><b>Alert</b> ${esc(p.summary)}</div>` : ""; },
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

function togglePlay() {
  const st = stateOf();
  if (st?.status === "running") { send("stop"); return; }
  const sel = $("#startday");
  const fresh = !st || !st.cursor || st.status === "finished";
  send("play", fresh ? { start_day: Number(sel?.value ?? S.startDay) } : {});
}

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch { /* nothing more to try */ }
    ta.remove();
  }
  const label = btn.querySelector("span");
  if (label) { label.textContent = "Copied"; setTimeout(() => { label.textContent = "Copy"; }, 1600); }
  toast("Event id copied");
}

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("button, [data-goto], [data-alert], .pin, a");
  if (!t) return;
  if (t.dataset.persona) { switchPersona(t.dataset.persona); return; }
  if (t.dataset.group) { S.group = t.dataset.group; renderChips(); renderCards(); return; }
  if (t.classList.contains("mcard")) { openDrawer(t.dataset.m); return; }
  if (t.id === "dr-close") { closeDrawer(); return; }
  if (t.dataset.range) { S.range = t.dataset.range; renderDrawer(); return; }
  if (t.dataset.copy) { copyText(t.dataset.copy, t); return; }
  if (t.dataset.act === "record-toggle") {
    S.recordHidden = !S.recordHidden;
    try { localStorage.setItem(RECORD_KEY, S.recordHidden ? "1" : "0"); } catch { /* private mode */ }
    renderRecord();
    $('[data-act="record-toggle"]')?.focus();
    return;
  }
  if (t.dataset.act === "play" || t.id === "play") { togglePlay(); return; }
  if (t.id === "reset") { send("reset"); return; }
  if (t.dataset.speed) { send("speed", { speed: Number(t.dataset.speed) }); return; }
  if (t.id === "disconnect") { forgetConn(); location.reload(); return; }
  const gid = t.dataset.goto || t.dataset.alert || t.closest("[data-alert]")?.dataset.alert;
  if (gid) focusAlert(Number(gid));
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
// <details> fires "toggle" without bubbling: remember what the person opened so a refresh of the card does not close it.
document.addEventListener("toggle", (ev) => {
  const k = ev.target?.dataset?.ok;
  if (!k) return;
  if (ev.target.open) S.open.add(k); else S.open.delete(k);
}, true);

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function focusAlert(id, instant = false) {
  S.selectedAlert = id;
  if (S.openMetric) closeDrawer();
  if (!S.alerts.some((a) => a.id === id && CARD_STATUS.has(a.status))) S.open.add("held");
  renderAlerts();
  renderSide();
  renderTimeline();
  const el = document.getElementById(`alert-${id}`);
  if (!el) return;
  applyOpen($("#alerts"));
  el.scrollIntoView({ behavior: reduced() || instant ? "auto" : "smooth", block: "start" });
  el.focus({ preventScroll: true });
}

// ?goto=alert-101 (or any element id) scrolls there once it exists. Used for links and for screenshots.
function jumpIfAsked() {
  if (!S.pendingGoto) return;
  const m = /^alert-(\d+)$/.exec(S.pendingGoto);
  if (m) {
    if (!document.getElementById(S.pendingGoto)) return;
    S.pendingGoto = null;
    focusAlert(Number(m[1]), true);
    return;
  }
  const el = document.getElementById(S.pendingGoto);
  if (!el || el.hidden) return;
  S.pendingGoto = null;
  el.scrollIntoView({ behavior: "auto", block: "start" });
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
function watchHero() {
  if (query.get("mini") === "1" || !("IntersectionObserver" in window)) return;
  new IntersectionObserver(([e]) => {
    const past = !e.isIntersecting && e.boundingClientRect.top < 0;
    if (past !== S.past) { S.past = past; $("#minibar").hidden = !past; }
  }, { rootMargin: "-80px 0px 0px 0px" }).observe($("#hero"));
}

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
watchHero();
setInterval(() => { if (!document.hidden) tickCountdowns(); }, 1000);
boot().then(() => {
  const m = query.get("metric");        // shareable link to one signal
  if (m && METRICS[m]) setTimeout(() => openDrawer(m), 400);
});
