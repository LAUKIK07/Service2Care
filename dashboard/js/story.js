// Everything that turns an alert into an "event" on the page: the card, its receipt, the conversation, the interpretation
// versions, the story timeline and the health record. Pure functions: data in, HTML string out. Every piece of dynamic text
// (the person's own words included) goes through esc() before it reaches the DOM.
import { ALERT_LABELS, CARD_OF, METRICS, esc, fmt, humanize, icon, localClock, localStamp, parseIso, sign, when } from "./format.js";

// ----------------------------------------------------------------- status
// [plain label, css tone]. Anything the service adds later falls back to a humanised version of its name.
export const STATUS = {
  sent: ["Texted", "ok"], discussed: ["Replied", "dark"], resolved: ["Resolved", "ok"], dry_run: ["Drafted, not sent", "mute"],
  suppressed: ["Held back", "mute"], watching: ["Watching", "warn"], ignored: ["Ignored", "mute"], data_issue: ["Data issue", "mute"],
  send_failed: ["Send failed", "bad"], sending: ["Sending\u2026", "warn"],
  held_consent: ["Waiting: no consent yet", "wait"], held_paused: ["Waiting: paused", "wait"], held_quiet: ["Waiting: quiet hours", "wait"],
  held_open_story: ["Waiting: another chat open", "wait"], held_rate: ["Waiting: too soon", "wait"],
  digested: ["Combined into a summary", "mute"], cancelled: ["Cancelled", "mute"], expired: ["Expired", "mute"],
};
export const statusInfo = (s) => STATUS[s] || [humanize(s) || "Unknown", "mute"];

// Alerts that get a full card (they were texted, are being sent, or are waiting to be). The rest are listed as "considered".
export const CARD_STATUS = new Set(["sent", "discussed", "resolved", "dry_run", "send_failed", "sending", "held_consent", "held_paused", "held_quiet", "held_open_story", "held_rate"]);
export const isHeld = (s) => typeof s === "string" && s.startsWith("held_");
export const labelOf = (a) => ALERT_LABELS[a.metric] || humanize(a.metric);

// -------------------------------------------------------------- one-liners
const unitOf = (m) => ({ sleep_hours: "h", rhr_sleep: "bpm", hrv_sleep: "ms" }[m] || METRICS[CARD_OF[m]]?.unit || "");

export function evidenceLine(a) {
  const e = a.evidence || {};
  if (e.kind === "gap") return `No usable samples for about ${e.persistence_h} hours`;
  if (e.value == null || e.baseline == null) return "";
  const m = e.metric;
  const abs = m === "skin_temp" || m === "spo2";
  const diff = abs ? `${sign(e.value - e.baseline, 1)} ${m === "spo2" ? "pts" : "\u00b0C"}` : `${sign(e.pct, 1)}%`;
  return `${fmt(e.value, 1)} ${unitOf(m)} vs usual ${fmt(e.baseline, 1)} \u00b7 ${diff} \u00b7 ${sign(e.z, 1)}\u03c3 \u00b7 ${e.persistence_h} h${e.ongoing ? " and continuing" : ""}`;
}

export function decisionLine(a) {
  const d = a.decision || {};
  if (a.status === "suppressed") return `<span class="who policy">Policy</span> ${esc(d.reason || "Held back by the alert rules")}`;
  if (isHeld(a.status) && d.source !== "jev" && d.source !== "rules") return "";
  if (d.source === "jev") {
    const act = { alert_now: "ask now", watch: "keep watching", ignore: "ignore", data_issue: "data problem" }[d.action] || d.action;
    return `<span class="who jev">Jev</span> ${esc(act)} \u00b7 p(alert) ${fmt(d.p_alert, 2)} \u00b7 confidence ${fmt(d.confidence, 2)}${d.floor ? " \u00b7 <b>safety floor applied</b>" : ""}`;
  }
  if (d.source === "rules") return `<span class="who policy">Rules</span> ${d.floor ? "very large sustained change, safety floor" : d.action === "alert_now" ? "large and sustained" : "not strong enough"}`;
  return "";
}

export function checkText(c) {
  const f = c.factor;
  if (f === "pattern") return c.metrics_checked ? `${c.metrics_in_direction} of ${c.metrics_checked} related signals moved the same way` : null;
  const name = { workout: "Workout", poor_sleep: "Poor sleep", caffeine: "Caffeine", alcohol: "Alcohol", stress: "Stress", illness: "Illness", travel: "Travel", medication: "Medication", device_fit: "Sensor fit", hydration: "Hydration", other: "Other" }[f] || humanize(f);
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

// ----------------------------------------------------------------- receipt
const dd = (label, body) => `<dt>${label}</dt><dd>${body}</dd>`;
const cap = (s) => { const t = String(s ?? ""); return t ? t[0].toUpperCase() + t.slice(1) : t; };

// "How I decided": the numbers behind one alert, so it can be checked by hand.
export function receiptHtml(a, persona = {}) {
  const e = a.evidence || {};
  const r = e.receipt;
  if (!r || typeof r !== "object") return "";
  const unit = unitOf(r.metric || e.metric);
  const base = r.baseline || {};
  const calc = r.calculation || {};
  const win = r.window || {};
  const readings = Array.isArray(r.readings) ? r.readings : [];
  const table = readings.length
    ? `<table class="rt"><caption>Hourly medians in the window</caption><thead><tr><th scope="col">Hour</th><th scope="col">Median${unit ? ` (${esc(unit)})` : ""}</th><th scope="col">Samples</th></tr></thead><tbody>${readings.map((x) => `<tr><th scope="row">${esc(when(`${x.hour}:00`) || x.hour)}</th><td>${fmt(x.median, 2)}</td><td>${fmt(x.samples)}</td></tr>`).join("")}</tbody></table>`
    : "";
  // Worked example, only when the numbers in the receipt really give that z.
  let worked = "";
  if (e.value != null && base.median != null && base.scale) {
    const z = (e.value - base.median) / base.scale;
    if (calc.z != null && Math.abs(z - calc.z) <= 0.25) worked = `<br><span class="mono">z = (${fmt(e.value, 2)} \u2212 ${fmt(base.median, 2)}) / ${fmt(base.scale, 2)} = ${sign(calc.z, 1)}</span>`;
  }
  const zLine = calc.z != null ? `<span class="mono">${esc(calc.formula || "z = (reading \u2212 median) / scale")}</span>${worked || `<br><span class="mono">z = ${sign(calc.z, 1)}</span>`}${calc.percent_change != null ? `<br>That is ${sign(calc.percent_change, 1)}% from the usual value.` : ""}` : "";
  const ctx = { sleep: "while asleep", awake: "while awake", active: "while exercising" }[r.context] || "";
  const windowLine = win.started || win.detected
    ? `${win.started ? `Started ${esc(when(win.started))}` : ""}${win.detected ? `, noticed ${esc(when(win.detected))}` : ""}${win.hours != null ? `, lasting about ${fmt(win.hours)} h` : ""}${ctx ? `, ${ctx}` : ""}${win.quality ? `. Signal quality: ${esc(win.quality)}.` : "."}`
    : "";
  const d = a.decision || {};
  const q = d.question && typeof d.question === "object" ? d.question : null;
  const gap = r.kind === "gap";                      // a silent tracker has no usual value or z to show
  const u = unit ? ` ${esc(unit)}` : "";
  const rows = [
    windowLine ? dd("The window", windowLine) : "",
    !gap && (base.definition || base.median != null) ? dd("Usual value", `${esc(cap(base.definition || "Median of this person's earlier readings"))}.${base.median != null ? ` Here: <b>${fmt(base.median, 2)}${u}</b>, typical spread ${fmt(base.scale, 2)}${base.samples != null ? `, from ${fmt(base.samples)} earlier readings` : ""}.` : ""}`) : "",
    !gap && zLine ? dd("The arithmetic", zLine) : "",
    r.rule ? dd("The rule", `Alert when there is ${esc(r.rule)}.`) : "",
    r.source ? dd("Data source", esc(r.source)) : "",
    q?.question_kind ? dd("The question", `${esc(humanize(q.question_kind))}${q.source ? ` (picked by ${esc(q.source === "jev" ? "Jev" : q.source)}` : ""}${q.confidence != null ? `, confidence ${fmt(q.confidence, 2)}` : ""}${q.source ? ")" : ""}`) : "",
  ].join("");
  const synth = r.synthetic === true || (r.synthetic == null && persona.kind !== "real")
    ? `<p class="fine rc-note">${esc(persona.name || "This person")} is simulated. These readings were generated by a program, not measured on a real person.</p>`
    : "";
  return `<details class="receipt" data-ok="r-${a.id}"><summary data-fid="rc-${a.id}"><span>How I decided</span><span class="mut">the numbers behind this alert</span></summary>
    <div class="rc">${table}<dl class="rcdl">${rows}</dl>${synth}</div></details>`;
}

// ------------------------------------------------------------ conversation
const CARD_TAG = /\n?\[card image\]\s*$/;

export function pollOptions(m) {
  if (Array.isArray(m.options) && m.options.length) return m.options.map(String);
  const t = String(m.text || "");
  return t.startsWith("[poll]") ? t.slice(6).split("|").map((x) => x.trim()).filter(Boolean) : [];
}

const KIND_LABEL = { question: "Question", update: "What changed", closing: "Saved to the record", explain: "Explanation", alert: "Alert", reply: "" };

export function bubbleHtml(m) {
  const inbound = m.direction === "in";
  const time = m.created_at ? `<time class="bt" datetime="${esc(m.created_at)}">${esc(localClock(m.created_at))}</time>` : "";
  const kind = m.kind || (inbound ? "reply_in" : "");
  if (kind === "system") return `<div class="sysmsg">${esc(m.text)}</div>`;
  if (kind === "poll") {
    const opts = pollOptions(m);
    return `<div class="bub out poll"><span class="bub-k">${icon("list", 13)} Tap to answer</span><ul class="opts">${opts.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>${time}</div>`;
  }
  const raw = String(m.text ?? "");
  const hasCard = CARD_TAG.test(raw);
  const text = raw.replace(CARD_TAG, "").trim();
  const label = inbound ? "" : KIND_LABEL[kind] || "";
  return `<div class="bub ${inbound ? "in" : "out"} ${esc(kind.replace(/[^a-z_]/g, ""))}">${label ? `<span class="bub-k">${esc(label)}</span>` : ""}${hasCard ? `<span class="cardtag">${icon("image", 14)} Picture card</span>` : ""}${text ? `<span class="bt-text">${esc(text)}</span>` : ""}${time}</div>`;
}

// What to show as the conversation for one alert. Falls back to the draft text, or to the saved Q&A, when no messages are stored.
export function conversationHtml(a, msgs, note) {
  const parts = msgs.map(bubbleHtml);
  if (!msgs.length && a.text) {
    const label = a.status === "send_failed" ? "Draft, not delivered" : a.status === "sending" ? "Sending now" : isHeld(a.status) || a.status === "dry_run" ? "Draft, not sent" : "Alert";
    parts.push(`<div class="bub out draft"><span class="bub-k">${label}</span><span class="bt-text">${esc(a.text)}</span></div>`);
  }
  if (note && Array.isArray(note.qa) && note.qa.length && !msgs.some((m) => m.direction === "in")) {
    for (const x of note.qa) {
      if (x.q) parts.push(`<div class="bub out question"><span class="bub-k">Question</span><span class="bt-text">${esc(x.q)}</span></div>`);
      if (x.a) parts.push(`<div class="bub in reply_in"><span class="bt-text">${esc(x.a)}</span>${x.at ? `<time class="bt">${esc(localClock(x.at))}</time>` : ""}</div>`);
    }
  }
  return parts.join("");
}

// The live line under the conversation. The countdown text is filled in by tickCountdowns(), so this string stays the same
// from one poll to the next and the card is not re-rendered every second.
export function storyStateHtml(note) {
  if (!note) return "";
  if (note.status === "open") {
    const at = localClock(note.closes_at);
    return `<p class="sstate listening"><span class="livedot" aria-hidden="true"></span><span>Listening${at ? `, window closes at <b>${esc(at)}</b>` : ""}</span>${note.closes_at ? `<span class="cd" data-closes="${esc(note.closes_at)}" aria-hidden="true"></span>` : ""}</p>`;
  }
  return `<p class="sstate saved">${icon("check", 15)}<span>Saved to the record</span></p>`;
}

// Fills every countdown on the page. Touches text only; never re-renders.
export function tickCountdowns(root = document) {
  const now = Date.now();
  for (const el of root.querySelectorAll("[data-closes]")) {
    const end = parseIso(el.dataset.closes);
    if (end == null) continue;
    const left = Math.round((end - now) / 1000);
    const text = left > 0 ? `\u00b7 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left` : "\u00b7 window ended, saving soon";
    if (el.textContent !== text) el.textContent = text;
  }
}

// ----------------------------------------------------- interpretation steps
const VKIND = { from_data: "From the data", recheck: "Re-checked after you shared context", user_correction: "Updated after your correction" };

export function versionsHtml(note) {
  const vs = note && Array.isArray(note.versions) ? note.versions.filter((v) => v && typeof v === "object") : [];
  if (!vs.length) return "";
  const items = vs.map((v, n) => {
    const before = v.changed ? v.before || vs[n - 1]?.text : null;
    const body = before
      ? `<div class="diff"><div class="before"><small>Before</small><p>${esc(before)}</p></div><div class="now"><small>Now</small><p>${esc(v.text)}</p></div></div>`
      : `<p class="vtext">${esc(v.text)}</p>`;
    const basis = v.basis && typeof v.basis === "object" ? v.basis : {};
    const sig = Array.isArray(basis.signature) ? basis.signature.filter((x) => typeof x === "string").map((x) => `<span class="fchip soft">${esc(humanize(x))}</span>`).join("") : "";
    const basisHtml = basis.note || sig ? `<p class="basis">${basis.note ? `Based on: ${esc(basis.note)}` : "Based on"} ${sig}</p>` : "";
    const tag = v.changed ? `<span class="chg">Changed</span>` : n > 0 ? `<span class="same">No change</span>` : "";
    return `<li class="${n === vs.length - 1 ? "cur" : ""}"><span class="vdot">v${esc(v.v ?? n + 1)}</span><div class="vbody"><div class="vh"><b>${esc(VKIND[v.kind] || humanize(v.kind) || "Interpretation")}</b>${tag}${v.at ? `<time datetime="${esc(v.at)}">${esc(localClock(v.at))}</time>` : ""}</div>${body}${basisHtml}</div></li>`;
  }).join("");
  return `<section class="interp" aria-label="Interpretation timeline"><h5>Interpretation timeline</h5><ol class="vlist">${items}</ol></section>`;
}

// Checks (plain language), factor chips and the saved summary.
export function noteHtml(note) {
  if (!note) return "";
  const chips = (note.factors || []).map((f) => `<span class="fchip">${esc(humanize(f))}</span>`).join("");
  const checks = (note.computed || []).map(checkText).filter(Boolean).map((t) => `<li>${esc(t)}</li>`).join("");
  const open = note.status === "open";
  const summary = note.summary
    ? `<p class="summary"><b>Summary</b> ${esc(note.summary)}</p>`
    : open ? `<p class="summary mut">The summary is written when the conversation closes.</p>` : "";
  if (!chips && !checks && !summary) return "";
  return `<div class="note"><div class="nh">${icon("chat", 16)}<b>${open ? "Checks so far" : "Linked to the chart"}</b></div>${chips ? `<div class="fchips">${chips}</div>` : ""}${checks ? `<ul class="checks">${checks}</ul><p class="fine">These checks compare against this person's own history. They describe the data and do not show what caused it.</p>` : ""}${summary}</div>`;
}

export const eventIdHtml = (a) => (a.event_id
  ? `<div class="eid"><span class="eid-l">Event</span><code>${esc(a.event_id)}</code><button type="button" class="copy" data-copy="${esc(a.event_id)}" data-fid="copy-${a.id}" aria-label="Copy event id ${esc(a.event_id)}">${icon("copy", 14)}<span>Copy</span></button></div>`
  : "");

// ----------------------------------------------------------------- the card
export function alertCardHtml(a, { msgs = [], note = null, selected = false, persona = {} } = {}) {
  const [stText, stCls] = statusInfo(a.status);
  const hot = a.status === "sent" || a.status === "discussed";
  const dec = decisionLine(a);
  const convo = conversationHtml(a, msgs, note);
  const state = storyStateHtml(note);
  const src = persona.kind === "real" ? "public dataset" : "simulated";
  const ev = evidenceLine(a);
  return `<article class="acard ${selected ? "sel" : ""} ${isHeld(a.status) ? "waiting" : ""}" id="alert-${a.id}" data-card="${a.id}" tabindex="-1" aria-labelledby="ah-${a.id}">
    <header><span class="sev ${hot ? "hot" : ""}"></span>
      <div class="ah"><h4 id="ah-${a.id}">${esc(labelOf(a))}</h4><p>${esc(when(a.ts))} <span class="mut">${src}</span></p></div>
      <span class="status ${stCls}">${esc(stText)}</span></header>
    ${eventIdHtml(a)}
    ${ev ? `<p class="ev">${esc(ev)}</p>` : ""}
    ${receiptHtml(a, persona)}
    ${convo || state ? `<div class="convo"><div class="convo-h"><h5>Conversation</h5>${state}</div>${convo ? `<div class="thread">${convo}</div>` : ""}</div>` : ""}
    ${versionsHtml(note)}
    ${noteHtml(note)}
    ${dec ? `<p class="dec">${dec}</p>` : ""}
  </article>`;
}

// An alert that was considered but not texted.
export function consideredRowHtml(a, persona = {}) {
  const [stText, stCls] = statusInfo(a.status);
  const dec = decisionLine(a);
  return `<div class="hrow" id="alert-${a.id}"><span class="status ${stCls}">${esc(stText)}</span><b>${esc(labelOf(a))}</b><span class="mut">${esc(when(a.ts))}</span>${a.event_id ? `<code class="eidm">${esc(a.event_id)}</code>` : ""}${dec ? `<p class="dec">${dec}</p>` : ""}${receiptHtml(a, persona)}</div>`;
}

// -------------------------------------------------------- story timeline
const clamp = (s, n) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}\u2026` : s);

export function storyLine(note) {
  if (note.summary) return note.summary;
  const last = Array.isArray(note.versions) && note.versions.length ? note.versions[note.versions.length - 1] : null;
  if (last?.text) return last.text;
  return note.status === "open" ? "Listening for a reply\u2026" : "Nothing was attached to the chart.";
}

export function timelineHtml(notes, alertsById, selected) {
  const stamp = (n) => alertsById.get(n.alert_id)?.ts || n.end_ts || n.start_ts || "";
  const list = [...notes].sort((x, y) => (stamp(y) > stamp(x) ? 1 : stamp(y) < stamp(x) ? -1 : y.alert_id - x.alert_id));
  return `<ol class="stl">${list.map((n) => {
    const a = alertsById.get(n.alert_id);
    const open = n.status === "open";
    const label = a ? labelOf(a) : ALERT_LABELS[n.metric] || humanize(n.metric);
    const chips = (n.factors || []).map((f) => `<span class="fchip">${esc(humanize(f))}</span>`).join("");
    const vcount = Array.isArray(n.versions) && n.versions.length > 1 ? `<span class="stl-v">${n.versions.length} versions</span>` : "";
    return `<li><button type="button" class="stl-item ${selected === n.alert_id ? "sel" : ""}" data-goto="${n.alert_id}" data-fid="stl-${n.alert_id}" aria-label="${esc(`${label}, ${when(stamp(n))}, ${open ? "open" : "closed"}. Go to the alert.`)}">
      <span class="stl-dot ${open ? "open" : "closed"}" aria-hidden="true"></span>
      <span class="stl-main"><span class="stl-top"><time>${esc(when(stamp(n)))}</time><b>${esc(label)}</b><span class="stl-state ${open ? "open" : "closed"}">${open ? "Open" : "Closed"}</span>${vcount}</span>
      <span class="stl-sum">${esc(clamp(storyLine(n), 170))}</span>${chips ? `<span class="fchips">${chips}</span>` : ""}</span></button></li>`;
  }).join("")}</ol>`;
}

// ------------------------------------------------------- health record
const FLAG_TEXT = {
  lung_condition: "Lung note on file", allergies: "Allergy note on file", past_surgery: "Past surgery noted", heart_or_bp_note: "Heart or blood pressure note on file",
};
const ORIGIN_TEXT = { onboarding: "told in setup", correction: "corrected by you", photo: "from a photo you confirmed", chat: "mentioned in chat" };
export const flagText = (f) => FLAG_TEXT[f] || `${humanize(f)} note on file`;

// Phrased as notes the person gave, never as conditions we found. All text is theirs, escaped.
export function recordHtml({ name, profile, hidden }) {
  const items = profile && Array.isArray(profile.items) ? profile.items : [];
  const flags = profile && Array.isArray(profile.flags) ? profile.flags : [];
  const head = `<div class="rec-h"><h2>${icon("record", 18)}Health record</h2><span class="rec-for mut">${esc(name)}</span>
      <button type="button" class="linkbtn rec-toggle" data-act="record-toggle" aria-expanded="${!hidden}"${hidden ? "" : ' aria-controls="rec-body"'}>${hidden ? "Show" : "Hide"}</button></div>`;
  if (hidden) return head;
  const body = items.length
    ? `${flags.length ? `<ul class="flags" aria-label="Notes on file">${flags.map((f) => `<li class="flag">${esc(flagText(f))}</li>`).join("")}</ul>` : ""}
       <ul class="ritems">${items.map((i) => `<li><span class="rl">${esc(i.label || humanize(i.key))}</span><span class="rv">${esc(i.value)}</span><span class="origin ${esc(String(i.origin || "").replace(/[^a-z]/g, ""))}">${esc(ORIGIN_TEXT[i.origin] || (i.origin ? humanize(i.origin) : "noted"))}${i.at ? ` \u00b7 ${esc(localStamp(i.at))}` : ""}</span></li>`).join("")}</ul>`
    : `<p class="rec-empty">Nothing on file yet. What ${esc(name)} shares in setup or in a chat appears here, in their own words.</p>`;
  return `${head}<div id="rec-body">${body}<p class="fine rec-note">These are the person's own words. Nothing here is a diagnosis.${profile && profile.updated_at ? ` Updated ${esc(localStamp(profile.updated_at))}.` : ""}</p></div>`;
}
