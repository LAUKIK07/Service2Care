// Dependency-free SVG charts: card sparklines and the interactive detail chart.
import { TICKS_PER_DAY, clock, dayLabel, esc, fmt } from "./format.js";

function linePath(xs, ys) {
  let d = "";
  let pen = false;
  for (let k = 0; k < xs.length; k++) {
    if (ys[k] == null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${xs[k].toFixed(1)} ${ys[k].toFixed(1)}`;
    pen = true;
  }
  return d;
}

function areaPath(xs, ys, base) {
  // closed shape per continuous run
  let d = "";
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      d += `M${run[0][0].toFixed(1)} ${base}` + run.map(([x, y]) => `L${x.toFixed(1)} ${y.toFixed(1)}`).join("") + `L${run[run.length - 1][0].toFixed(1)} ${base}Z`;
    }
    run = [];
  };
  for (let k = 0; k < xs.length; k++) {
    if (ys[k] == null) flush(); else run.push([xs[k], ys[k]]);
  }
  flush();
  return d;
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1;
  const raw = span / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const start = Math.ceil(lo / step) * step;
  const out = [];
  for (let v = start; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return { ticks: out, step };
}

// ---------------------------------------------------------------- sparkline
// values: array (null = no data). band: [{lo,hi}|null]. marks: indexes into values.
export function sparkline({ values, color, band = null, marks = [], w = 280, h = 64, id = "sp" }) {
  const finite = values.filter((v) => v != null);
  if (finite.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="No data yet"></svg>`;
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  if (band) for (const b of band) if (b) { lo = Math.min(lo, b.lo); hi = Math.max(hi, b.hi); }
  const pad = (hi - lo || 1) * 0.12;
  lo -= pad; hi += pad;
  const n = values.length;
  const X = (k) => 2 + (k / (n - 1)) * (w - 4);
  const Y = (v) => h - 4 - ((v - lo) / (hi - lo)) * (h - 8);
  const xs = values.map((_, k) => X(k));
  const ys = values.map((v) => (v == null ? null : Y(v)));
  let bandSvg = "";
  if (band) {
    const top = [], bot = [];
    band.forEach((b, k) => { if (b) { top.push(`${X(k).toFixed(1)} ${Y(b.hi).toFixed(1)}`); bot.push(`${X(k).toFixed(1)} ${Y(b.lo).toFixed(1)}`); } });
    if (top.length > 2) bandSvg = `<path d="M${top.join("L")}L${bot.reverse().join("L")}Z" fill="${color}" opacity=".10"/>`;
  }
  const markSvg = marks.map((k) => `<circle cx="${X(k).toFixed(1)}" cy="${(ys[k] ?? h / 2).toFixed(1)}" r="3.6" fill="#fff" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`).join("");
  const lastK = ys.map((y, k) => (y == null ? -1 : k)).reduce((a, b) => Math.max(a, b), -1);
  const dot = lastK >= 0 ? `<circle cx="${X(lastK).toFixed(1)}" cy="${ys[lastK].toFixed(1)}" r="3.2" fill="${color}"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Last 48 hours trend">
    <defs><linearGradient id="g-${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".22"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    ${bandSvg}
    <path d="${areaPath(xs, ys, h)}" fill="url(#g-${id})"/>
    <path d="${linePath(xs, ys)}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
    ${markSvg}${dot}</svg>`;
}

// ------------------------------------------------------------- bar sparkline
export function bars({ values, color, w = 280, h = 64 }) {
  const max = Math.max(1, ...values.filter((v) => v != null));
  const n = values.length;
  const bw = (w - 4) / n;
  const rects = values.map((v, k) => {
    if (v == null) return "";
    const bh = Math.max(1.5, (v / max) * (h - 8));
    return `<rect x="${(2 + k * bw + 1).toFixed(1)}" y="${(h - 4 - bh).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${color}" opacity="${k === n - 1 ? 1 : 0.55}"/>`;
  }).join("");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Trend">${rects}</svg>`;
}

// ------------------------------------------------------------- detail chart
// cfg: { lines:[{key,label,color,get(i)->number|null}], i0, i1, band(i)->{lo,hi}|null, state(i)->string,
//        pins:[{id,i0,i1,label,annotated}], unit, dec, extraTip(i)->html }
export function detailChart(el, cfg) {
  const W = Math.max(320, el.clientWidth || 640);
  const H = 320;
  const m = { l: 46, r: 14, t: 18, b: 30 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const { i0, i1 } = cfg;
  const count = i1 - i0 + 1;
  const X = (i) => m.l + ((i - i0) / Math.max(1, count - 1)) * iw;

  // data range
  let lo = Infinity, hi = -Infinity;
  for (const ln of cfg.lines) for (let i = i0; i <= i1; i++) { const v = ln.get(i); if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
  if (!Number.isFinite(lo)) { el.innerHTML = `<p class="empty">No data in this range yet.</p>`; return; }
  const bandPts = [];
  if (cfg.band) for (let i = i0; i <= i1; i++) { const b = cfg.band(i); if (b) { bandPts.push([i, b]); lo = Math.min(lo, b.lo); hi = Math.max(hi, b.hi); } }
  const pad = (hi - lo || 1) * 0.1;
  lo -= pad; hi += pad;
  const Y = (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;

  // sleep shading
  let shade = "";
  if (cfg.state) {
    let start = null;
    for (let i = i0; i <= i1 + 1; i++) {
      const asleep = i <= i1 && cfg.state(i) === "sleep";
      if (asleep && start == null) start = i;
      if (!asleep && start != null) { shade += `<rect x="${X(start).toFixed(1)}" y="${m.t}" width="${Math.max(1, X(i - 1) - X(start)).toFixed(1)}" height="${ih}" fill="#6366F1" opacity=".07"/>`; start = null; }
    }
  }

  // grid + axes
  const { ticks } = niceTicks(lo, hi, 4);
  const dec = cfg.dec ?? 0;
  const grid = ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}" stroke="#EBEBEB"/><text x="${m.l - 8}" y="${(Y(t) + 4).toFixed(1)}" text-anchor="end" class="ax">${fmt(t, dec)}</text>`).join("");
  let xt = "";
  const fine = count <= TICKS_PER_DAY * 1.6;
  const stepTicks = fine ? 24 : TICKS_PER_DAY * (count > TICKS_PER_DAY * 12 ? 3 : count > TICKS_PER_DAY * 5 ? 2 : 1);
  for (let i = Math.ceil(i0 / stepTicks) * stepTicks; i <= i1; i += stepTicks) {
    xt += `<line x1="${X(i).toFixed(1)}" x2="${X(i).toFixed(1)}" y1="${m.t}" y2="${m.t + ih}" stroke="#F2F2F2"/><text x="${X(i).toFixed(1)}" y="${H - 9}" text-anchor="middle" class="ax">${fine ? clock(i) : dayLabel(i)}</text>`;
  }

  // band
  let bandSvg = "";
  if (bandPts.length > 2) {
    const top = bandPts.map(([i, b]) => `${X(i).toFixed(1)} ${Y(b.hi).toFixed(1)}`);
    const bot = bandPts.map(([i, b]) => `${X(i).toFixed(1)} ${Y(b.lo).toFixed(1)}`).reverse();
    bandSvg = `<path d="M${top.join("L")}L${bot.join("L")}Z" fill="${cfg.lines[0].color}" opacity=".11"/>`;
  }

  // lines
  const idx = [];
  for (let i = i0; i <= i1; i++) idx.push(i);
  const xs = idx.map(X);
  const paths = cfg.lines.map((ln, k) => {
    const ys = idx.map((i) => { const v = ln.get(i); return v == null ? null : Y(v); });
    const area = k === 0 && cfg.lines.length === 1 ? `<path d="${areaPath(xs, ys, m.t + ih)}" fill="url(#dg)"/>` : "";
    return `${area}<path d="${linePath(xs, ys)}" fill="none" stroke="${ln.color}" stroke-width="${k ? 1.6 : 2.2}" stroke-linejoin="round" stroke-linecap="round" ${k ? 'opacity=".75"' : ""}/>`;
  }).join("");

  // alert pins
  const pins = (cfg.pins || []).filter((p) => p.i1 >= i0 && p.i0 <= i1).map((p) => {
    const a = X(Math.max(p.i0, i0)), b = X(Math.min(p.i1, i1));
    const col = p.annotated ? "#222222" : "#FF385C";
    return `<g class="pin" data-alert="${p.id}" tabindex="0" role="button" aria-label="${esc(p.label)}">
      <rect x="${a.toFixed(1)}" y="${m.t}" width="${Math.max(2, b - a).toFixed(1)}" height="${ih}" fill="#FF385C" opacity=".09"/>
      <line x1="${b.toFixed(1)}" x2="${b.toFixed(1)}" y1="${m.t + 12}" y2="${m.t + ih}" stroke="${col}" stroke-dasharray="3 3" opacity=".7"/>
      <circle cx="${b.toFixed(1)}" cy="${m.t + 8}" r="10" fill="${col}"/>
      <svg x="${(b - 6).toFixed(1)}" y="${m.t + 2}" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="${p.annotated ? "M4 5.5h16v10.5H9.5L5 20v-4H4z" : "M12 6v7M12 17.5v.1"}"/></svg>
    </g>`;
  }).join("");

  el.innerHTML = `<svg class="dchart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.aria || "Detail chart")}">
    <defs><linearGradient id="dg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${cfg.lines[0].color}" stop-opacity=".18"/><stop offset="1" stop-color="${cfg.lines[0].color}" stop-opacity="0"/></linearGradient></defs>
    ${shade}${grid}${xt}${bandSvg}${paths}${pins}
    <g class="hover" style="display:none"><line class="hl" y1="${m.t}" y2="${m.t + ih}" stroke="#222" stroke-opacity=".35"/>${cfg.lines.map((l) => `<circle class="hd" r="4.5" fill="#fff" stroke="${l.color}" stroke-width="2.2"/>`).join("")}</g>
    <rect class="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/>
  </svg><div class="tip" role="status" hidden></div>`;

  const svg = el.querySelector("svg");
  const hit = svg.querySelector(".hit");
  const hover = svg.querySelector(".hover");
  const hl = svg.querySelector(".hl");
  const dots = [...svg.querySelectorAll(".hd")];
  const tip = el.querySelector(".tip");
  const move = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(i0, Math.min(i1, Math.round(i0 + ((px - m.l) / iw) * (count - 1))));
    const x = X(i);
    hover.style.display = "";
    hl.setAttribute("x1", x); hl.setAttribute("x2", x);
    const rows = [];
    cfg.lines.forEach((ln, k) => {
      const v = ln.get(i);
      if (v == null) { dots[k].style.display = "none"; return; }
      dots[k].style.display = "";
      dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(v));
      rows.push(`<span class="sw" style="background:${ln.color}"></span>${esc(ln.label || "")} <b>${fmt(v, dec)}</b> ${esc(cfg.unit || "")}`);
    });
    const b = cfg.band && cfg.band(i);
    const extra = cfg.extraTip ? cfg.extraTip(i) : "";
    tip.innerHTML = `<div class="tt">${esc(cfg.timeLabel(i))}</div>${rows.map((r) => `<div>${r}</div>`).join("")}${b ? `<div class="mut">Usual here: ${fmt(b.lo, dec)}\u2013${fmt(b.hi, dec)}</div>` : ""}${extra}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const left = Math.min(Math.max(8, (x / W) * r.width - tw / 2), r.width - tw - 8);
    tip.style.left = `${left}px`;
    tip.style.top = "8px";
    el.dataset.hover = "1";
  };
  const leave = () => { hover.style.display = "none"; tip.hidden = true; delete el.dataset.hover; };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", leave);
}
