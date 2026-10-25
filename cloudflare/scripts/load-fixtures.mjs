#!/usr/bin/env node
// Loads the test fixtures into a LOCAL Worker so the dashboard can be viewed against realistic stories.
//
//   cd cloudflare
//   npx wrangler d1 execute signal2care --local --persist-to /tmp/s2c-dev --file=schema.sql
//   npx wrangler dev --local --port 8788 --persist-to /tmp/s2c-dev          (another terminal)
//   node scripts/load-fixtures.mjs [--heartbeat]
//   python3 -m http.server 8080 --directory ../dashboard                     (another terminal)
//   open http://127.0.0.1:8080/#api=http%3A%2F%2F127.0.0.1%3A8788&k=<DASH_TOKEN from .dev.vars>
//
// What is loaded: the persona list (fixtures/personas.json), Arya's readings, monitor and feed from dashboard/sample.json
// (read only), Arya's alerts and stories (fixtures/stories-maya.json), her health record (fixtures/profile-maya.json), a drafted alert for Daniel,
// and a "real data" person (real-demo) made from Arya's readings with some channels removed.
// Times written as "@NOW-12m" / "@NOW+6m" in the fixtures are replaced with real times, so the open story's window closes a few minutes after loading.
// --heartbeat keeps posting the brain's health every 10 s so the dashboard shows "Monitor online" until you press Ctrl-C.
//
// Local only: refuses any URL that is not localhost. Tokens come from .dev.vars and are never printed.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const base = opt("--base", "http://127.0.0.1:8788");
if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base)) { console.error(`refusing to load fixtures into ${base} (local only)`); process.exit(2); }

const vars = Object.fromEntries(readFileSync(join(root, ".dev.vars"), "utf8").split("\n").map((l) => l.match(/^([A-Z_]+)=(.*)$/)).filter(Boolean).map((m) => [m[1], m[2].replace(/^["']|["']$/g, "")]));
if (!vars.INGEST_TOKEN) { console.error("INGEST_TOKEN missing in .dev.vars"); process.exit(2); }

const load = (rel) => JSON.parse(readFileSync(join(root, rel), "utf8"));
const sample = load(opt("--sample", "../dashboard/sample.json"));

// "@NOW-12m" -> an ISO UTC time 12 minutes ago
const now = Date.now();
const unit = { s: 1000, m: 60000, h: 3600000 };
const fill = (v) => {
  if (typeof v === "string") {
    const m = /^@NOW([+-])(\d+)([smh])$/.exec(v);
    return m ? new Date(now + (m[1] === "-" ? -1 : 1) * Number(m[2]) * unit[m[3]]).toISOString().replace("Z", "+00:00") : v;
  }
  if (Array.isArray(v)) return v.map(fill);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fill(x)]));
  return v;
};
// fixtures carry JSON-in-string fields (evidence, qa, versions, ...): fill the times inside them as well
const fillDeep = (v) => {
  if (typeof v === "string" && /^[[{]/.test(v)) { try { return JSON.stringify(fill(JSON.parse(v))); } catch { return v; } }
  if (Array.isArray(v)) return v.map(fillDeep);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fillDeep(x)]));
  return fill(v);
};
const fixture = (name) => fillDeep(load(`fixtures/${name}.json`).items);

const DROP = new Set(["bp_sys", "bp_dia", "eda", "stress"]);          // channels the "real" fixture person does not have
const items = [];
for (const p of ["maya", "daniel", "priya", "real-demo"]) items.push({ k: "reset", d: { persona: p } });
items.push(...fixture("personas"));
for (const t of sample.series) items.push({ k: "series", d: { persona: "maya", ...t } });
for (const t of sample.series) items.push({ k: "series", d: { persona: "real-demo", ...t, v: Object.fromEntries(Object.entries(t.v).filter(([k]) => !DROP.has(k))) } });
items.push({ k: "state", d: { ...sample.states.maya, persona: "maya" } }, { k: "state", d: { ...sample.states.maya, persona: "real-demo" } });
if (sample.monitor) items.push({ k: "monitor", d: { ...sample.monitor, persona: "maya" } });
for (const f of sample.feed) items.push({ k: "feed", d: { ...f, persona: "maya" } });
items.push(...fixture("stories-maya"), ...fixture("profile-maya"), ...fixture("stories-daniel"));
const health = () => ({ k: "health", d: { text_model: "fixture/text-model", jev: true, jev_model: "typesafe/jev-1.14", ocr: "fixture", cloud: { pushed: items.length, dropped: 0 } } });
items.push({ k: "setting", d: { key: "send_alerts", value: "1" } }, health());

async function post(batch) {
  const res = await fetch(`${base}/api/ingest`, { method: "POST", headers: { authorization: `Bearer ${vars.INGEST_TOKEN}`, "content-type": "application/json" }, body: JSON.stringify({ items: batch }) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`ingest answered HTTP ${res.status}: ${JSON.stringify(body)}`);
  return body;
}

let sent = 0, dropped = 0;
for (let i = 0; i < items.length; i += 300) {
  const r = await post(items.slice(i, i + 300));
  sent += r.items; dropped += r.dropped;
  if (r.unknown_personas?.length) console.warn(`unknown persons in batch: ${r.unknown_personas.join(", ")}`);
}
console.log(`loaded ${sent} items (${dropped} dropped) into ${base}`);

if (args.includes("--heartbeat")) {
  console.log("heartbeat on: posting health every 10 s (Ctrl-C to stop)");
  setInterval(() => post([health()]).catch((e) => console.warn(e.message)), 10000);
} else {
  await post([health()]);
}
