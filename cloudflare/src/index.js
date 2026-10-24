// Signal2Care API on Cloudflare Workers + D1.
//
//   brain (this machine) --POST /api/ingest--> D1 <--GET /api/live-- dashboard (GitHub Pages)
//   dashboard --POST /api/command--> D1 --GET /api/commands--> brain
//
// Two secrets: INGEST_TOKEN (the brain) and DASH_TOKEN (the dashboard). No accounts, no sessions.
//
// Schema version 2 (see schema.sql and migrations/0002_story.sql): an alert is an "event" with a conversation
// ("story") attached, the person's own health notes ("profile") and a list of the people that can be replayed
// ("personas"). Phone numbers never reach this Worker.

const BUILTIN_PERSONAS = ["maya", "daniel", "priya"];
const PERSONA_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/;
const HUE = /^#[0-9A-Fa-f]{6}$/;
const COMMANDS = new Set(["play", "stop", "reset", "speed", "send_alerts"]);
const MAX_INGEST_ITEMS = 500;
const MAX_BODY_CHARS = 5_000_000;      // one ingest request; the brain sends at most 300 items (a few 100 KB)
const MAX_PARAM_CHARS = 400_000;       // one bound JSON parameter; larger groups are split over several statements
const MAX_PERSONAS = 24;

// ---------------------------------------------------------------- helpers
function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!origin || !allowed.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Headers": "authorization, content-type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(request, env, data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...corsHeaders(request, env) },
  });
}

async function digest(text) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

// Compare SHA-256 digests so the comparison does not leak the secret's length or prefix.
async function secretMatches(provided, expected) {
  if (!expected || !provided) return false;
  const [a, b] = await Promise.all([digest(provided), digest(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function bearer(request) {
  const h = request.headers.get("Authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7).trim() : "";
}

const parse = (text, fallback = null) => {
  if (text == null) return fallback;
  try { return JSON.parse(text); } catch { return fallback; }
};

const int = (value, fallback) => {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) ? n : fallback;
};

const isStr = (x) => typeof x === "string";
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const clip = (x, n) => (isStr(x) ? x.slice(0, n) : "");

// ---------------------------------------------------------------- personas
// The list of people the dashboard can show is sent by the brain (it grows when a real dataset is added),
// so no redeploy is needed. Until the first list arrives the three built-in people are valid.
function sanitizePersonas(d) {
  if (!isObj(d) || !Array.isArray(d.items)) return null;
  const seen = new Set();
  const items = [];
  for (const p of d.items.slice(0, MAX_PERSONAS)) {
    if (!isObj(p) || !isStr(p.id) || !PERSONA_ID.test(p.id) || seen.has(p.id)) continue;
    seen.add(p.id);
    items.push({
      id: p.id,
      name: clip(p.name, 40) || p.id,
      tag: clip(p.tag, 40),
      blurb: clip(p.blurb, 400),
      kind: p.kind === "real" ? "real" : "synthetic",
      source: clip(p.source, 200),
      license: clip(p.license, 120),
      citation: clip(p.citation, 400),
      channels: Array.isArray(p.channels) ? p.channels.filter(isStr).slice(0, 24).map((c) => c.slice(0, 40)) : [],
      hue: isStr(p.hue) && HUE.test(p.hue) ? p.hue : "#FF385C",
    });
  }
  if (!items.length) return null;
  return { items, linked: isStr(d.linked) && seen.has(d.linked) ? d.linked : null };
}

const idsOf = (cfg) => new Set(cfg && Array.isArray(cfg.items) && cfg.items.length ? cfg.items.map((p) => p.id) : BUILTIN_PERSONAS);

async function storedPersonas(db) {
  const row = await db.prepare("SELECT value FROM kv WHERE key='personas'").first();
  return row ? parse(row.value) : null;
}

// The brain's own words about the person: a snapshot per persona that replaces the previous one.
function sanitizeProfile(d) {
  const items = (Array.isArray(d.items) ? d.items : []).filter(isObj).slice(0, 60).map((i) => ({
    key: clip(i.key, 60), label: clip(i.label, 80), value: clip(i.value, 400), origin: clip(i.origin, 20), at: clip(i.at, 40),
  })).filter((i) => i.key || i.label);
  const flags = (Array.isArray(d.flags) ? d.flags : []).filter(isStr).slice(0, 24).map((f) => f.slice(0, 40));
  return { persona: d.persona, items, flags };
}

// ----------------------------------------------------------------- ingest
// Rows travel as one JSON array parameter per statement (json_each), which keeps every call far
// under D1's per-query bound-parameter and per-invocation query limits. INSERT OR REPLACE keeps it idempotent.
const RANK = (col) => `(CASE ${col} WHEN 'read' THEN 3 WHEN 'delivered' THEN 2 WHEN 'failed' THEN 1 ELSE 0 END)`;
const DELIVERY = { sent: 0, failed: 1, delivered: 2, read: 3 };
const BULK = {
  series: `INSERT OR REPLACE INTO series(persona,i,ts,state,quality,v)
    SELECT json_extract(value,'$.persona'), json_extract(value,'$.i'), json_extract(value,'$.ts'),
           json_extract(value,'$.state'), json_extract(value,'$.quality'), json_extract(value,'$.v') FROM json_each(?1)`,
  feed: `INSERT OR REPLACE INTO feed(id,persona,ts,actor,kind,text,data,created_at)
    SELECT json_extract(value,'$.id'), json_extract(value,'$.persona'), json_extract(value,'$.ts'),
           json_extract(value,'$.actor'), json_extract(value,'$.kind'), substr(json_extract(value,'$.text'),1,2000),
           json_extract(value,'$.data'), json_extract(value,'$.created_at') FROM json_each(?1)`,
  alert: `INSERT OR REPLACE INTO alerts(id,persona,kind,metric,family,ts,started,evidence,decision,status,text,sent_at,closed_at,created_at,event_id)
    SELECT json_extract(value,'$.id'), json_extract(value,'$.persona'), json_extract(value,'$.kind'),
           json_extract(value,'$.metric'), json_extract(value,'$.family'), json_extract(value,'$.ts'),
           json_extract(value,'$.started'), json_extract(value,'$.evidence'), json_extract(value,'$.decision'),
           substr(json_extract(value,'$.status'),1,24), substr(json_extract(value,'$.text'),1,4000), json_extract(value,'$.sent_at'),
           json_extract(value,'$.closed_at'), json_extract(value,'$.created_at'), json_extract(value,'$.event_id') FROM json_each(?1)`,
  annotation: `INSERT OR REPLACE INTO annotations(alert_id,persona,metric,start_ts,end_ts,summary,factors,computed,created_at,updated_at,
      event_id,status,opened_at,closes_at,qa,versions)
    SELECT json_extract(value,'$.alert_id'), json_extract(value,'$.persona'), json_extract(value,'$.metric'),
           json_extract(value,'$.start_ts'), json_extract(value,'$.end_ts'), substr(json_extract(value,'$.summary'),1,2000),
           json_extract(value,'$.factors'), json_extract(value,'$.computed'), json_extract(value,'$.created_at'),
           json_extract(value,'$.updated_at'), json_extract(value,'$.event_id'), substr(json_extract(value,'$.status'),1,12),
           json_extract(value,'$.opened_at'), json_extract(value,'$.closes_at'), json_extract(value,'$.qa'),
           json_extract(value,'$.versions') FROM json_each(?1)`,
  // delivery only ever moves forward (sent < failed < delivered < read), so re-sending a thread row cannot undo a receipt.
  thread: `INSERT INTO thread(alert_id,mid,direction,text,created_at,kind,options,delivery)
    SELECT json_extract(value,'$.alert_id'), json_extract(value,'$.mid'), json_extract(value,'$.direction'),
           substr(json_extract(value,'$.text'),1,4000), json_extract(value,'$.created_at'), substr(json_extract(value,'$.kind'),1,16),
           json_extract(value,'$.options'),
           COALESCE(json_extract(value,'$.delivery'), CASE WHEN json_extract(value,'$.direction')='out' THEN 'sent' END)
    FROM json_each(?1) WHERE true
    ON CONFLICT(alert_id,mid) DO UPDATE SET direction=excluded.direction, text=excluded.text, created_at=excluded.created_at,
      kind=excluded.kind, options=excluded.options,
      delivery=CASE WHEN ${RANK("excluded.delivery")} > ${RANK("thread.delivery")} THEN excluded.delivery ELSE thread.delivery END`,
};
// Alerts come before their status changes and their stories; nothing else depends on order within one segment.
const ORDER = ["series", "feed", "alert", "annotation", "thread"];

// What a row needs so that no single bad item can make D1 reject the whole batch (the brain retries a failed batch forever).
const VALID = {
  series: (d) => Number.isInteger(d.i) && isStr(d.ts) && isStr(d.state) && isStr(d.quality) && (isObj(d.v) || isStr(d.v)),
  feed: (d) => Number.isInteger(d.id),
  alert: (d) => Number.isInteger(d.id),
  annotation: (d) => Number.isInteger(d.alert_id),
  thread: (d) => Number.isInteger(d.alert_id) && isStr(d.mid) && d.mid.length > 0,
};
const KEEPS_PERSONA = new Set(["series", "feed", "alert", "annotation"]);

// Everything in a thread except the alert's own row (mid "alert-<id>"): the person's replies and whatever was said around them.
const STORY_THREAD = "mid NOT LIKE 'alert-%'";

function* chunked(rows) {
  let cur = [];
  let size = 2;
  for (const d of rows) {
    const s = JSON.stringify(d);
    if (cur.length && size + s.length + 1 > MAX_PARAM_CHARS) { yield `[${cur.join(",")}]`; cur = []; size = 2; }
    cur.push(s);
    size += s.length + 1;
  }
  if (cur.length) yield `[${cur.join(",")}]`;
}

const jlist = (values) => JSON.stringify([...values]);

// Which kind of barrier an item is: reset (wipe a person's replay), forget (erase what a person said), forget_story (one conversation).
function barrierOf(it, validPersona) {
  const d = it.d;
  if (it.k === "reset" && validPersona(d.persona)) return ["reset", d.persona];
  if (it.k === "forget" && validPersona(d.persona)) return ["forget", d.persona];
  if (it.k === "forget_story" && Number.isInteger(d.alert_id)) return ["story", d.alert_id];
  return null;
}

async function ingest(request, env) {
  const raw = await request.text();
  if (raw.length > MAX_BODY_CHARS) return json(request, env, { error: "too large" }, 413);
  let body;
  try { body = JSON.parse(raw); } catch { return json(request, env, { error: "invalid json" }, 400); }
  const items = (Array.isArray(body && body.items) ? body.items : []).slice(0, MAX_INGEST_ITEMS)
    .filter((it) => it && typeof it === "object" && isStr(it.k) && isObj(it.d));
  const now = Date.now();
  const nowIso = new Date(now).toISOString();
  const db = env.DB;
  const stmts = [];
  let revBump = false;
  let epochBump = false;
  const dropped = { invalid: 0, unknown_personas: new Set() };

  // The persona list in this request (if any) applies to the whole request, wherever in it the list sits.
  let sent = null;
  for (const it of items) if (it.k === "personas") sent = sanitizePersonas(it.d) || sent;
  const ids = idsOf(sent || (await storedPersonas(db)));
  const validPersona = (p) => isStr(p) && ids.has(p);
  const okPersona = (p) => { if (validPersona(p)) return true; if (isStr(p)) dropped.unknown_personas.add(p.slice(0, 48)); return false; };

  // Global, last one wins: no ordering concerns.
  const globals = {};
  for (const { k, d } of items) {
    if (k === "setting" && d.key === "send_alerts") globals.send_alerts = d.value === "1" || d.value === 1 || d.value === true ? "1" : "0";
    else if (k === "health") globals.health = JSON.stringify(d);
  }
  if (sent) stmts.push(db.prepare("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('personas',?1,?2)").bind(JSON.stringify(sent), now));
  if (globals.send_alerts !== undefined) stmts.push(db.prepare("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('send_alerts',?1,?2)").bind(globals.send_alerts, now));
  if (globals.health !== undefined) stmts.push(db.prepare("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('health',?1,?2)").bind(globals.health, now));

  // A reset or forget is a barrier: what comes before it is wiped with it, what follows is kept.
  const segments = [{ items: [], barrier: null }];
  let cur = segments[0];
  for (const it of items) {
    const b = barrierOf(it, validPersona);
    if (b) {
      cur.barrier ||= { reset: new Set(), forget: new Set(), story: new Set() };
      cur.barrier[b[0]].add(b[1]);
    } else if (it.k === "reset" || it.k === "forget") {
      okPersona(it.d.persona);                       // unknown person: nothing to wipe, but remember the name for the reply
    } else if (!["personas", "setting", "health", "forget_story"].includes(it.k)) {
      if (cur.barrier) { cur = { items: [], barrier: null }; segments.push(cur); }
      cur.items.push(it);
    }
  }

  for (const seg of segments) {
    const groups = {};
    const statuses = new Map();
    const profiles = new Map();
    const receipts = new Map();
    const latest = { state: new Map(), monitor: new Map() };
    for (const { k, d } of seg.items) {
      // Rows of a person who is reset right after them would be written and deleted again: skip the write.
      if (seg.barrier && seg.barrier.reset.has(d.persona) && (KEEPS_PERSONA.has(k) || k === "state" || k === "monitor")) continue;
      if (k === "state" || k === "monitor") {
        if (okPersona(d.persona)) latest[k].set(d.persona, d);
      } else if (k === "alert_status") {
        if (Number.isInteger(d.id) && isStr(d.status)) statuses.set(d.id, d.status.slice(0, 24));
      } else if (k === "receipt") {
        if (Number.isInteger(d.alert_id) && isStr(d.mid) && d.mid && Object.hasOwn(DELIVERY, d.state) && d.state !== "sent") {
          const key = `${d.alert_id}\u0000${d.mid}`;
          const had = receipts.get(key);
          if (!had || DELIVERY[d.state] > DELIVERY[had.state]) receipts.set(key, { alert_id: d.alert_id, mid: d.mid, state: d.state });
        } else dropped.invalid++;
      } else if (k === "profile") {
        if (okPersona(d.persona)) profiles.set(d.persona, sanitizeProfile(d));
      } else if (VALID[k]) {
        if (!VALID[k](d) || (KEEPS_PERSONA.has(k) && !okPersona(d.persona))) { dropped.invalid++; continue; }
        (groups[k] ||= []).push(d);
      }
    }
    for (const k of ORDER) {
      if (!groups[k]) continue;
      for (const param of chunked(groups[k])) stmts.push(db.prepare(BULK[k]).bind(param));
      if (k !== "series" && k !== "feed") revBump = true;
    }
    if (statuses.size) {
      const rows = [...statuses].map(([id, status]) => ({ id, status }));
      stmts.push(db.prepare(`UPDATE alerts SET status=j.status, closed_at=CASE WHEN j.status='resolved' THEN ?2 ELSE alerts.closed_at END
        FROM (SELECT json_extract(value,'$.id') AS id, json_extract(value,'$.status') AS status FROM json_each(?1)) AS j WHERE alerts.id=j.id`)
        .bind(JSON.stringify(rows), nowIso));
      revBump = true;
    }
    if (receipts.size) {
      stmts.push(db.prepare(`UPDATE thread SET delivery=j.state
        FROM (SELECT json_extract(value,'$.alert_id') AS a, json_extract(value,'$.mid') AS m, json_extract(value,'$.state') AS state FROM json_each(?1)) AS j
        WHERE thread.alert_id=j.a AND thread.mid=j.m AND ${RANK("j.state")} > ${RANK("thread.delivery")}`).bind(JSON.stringify([...receipts.values()])));
      revBump = true;
    }
    if (profiles.size) {
      const full = [...profiles.values()].filter((p) => p.items.length);
      const empty = [...profiles.values()].filter((p) => !p.items.length).map((p) => p.persona);
      if (full.length) {
        stmts.push(db.prepare(`INSERT OR REPLACE INTO profile(persona,items,flags,updated_at)
          SELECT json_extract(value,'$.persona'), json_extract(value,'$.items'), json_extract(value,'$.flags'), ?2 FROM json_each(?1)`)
          .bind(JSON.stringify(full), now));
      }
      if (empty.length) stmts.push(db.prepare("DELETE FROM profile WHERE persona IN (SELECT value FROM json_each(?1))").bind(jlist(empty)));
      revBump = true;
    }
    if (latest.state.size) {
      const rows = [...latest.state].map(([persona, d]) => ({ persona, data: JSON.stringify(d) }));
      stmts.push(db.prepare("INSERT OR REPLACE INTO state(persona,data,updated_at) SELECT json_extract(value,'$.persona'), json_extract(value,'$.data'), ?2 FROM json_each(?1)")
        .bind(JSON.stringify(rows), now));
    }
    if (latest.monitor.size) {
      const rows = [...latest.monitor].map(([persona, d]) => ({ persona, data: JSON.stringify(d) }));
      stmts.push(db.prepare("INSERT OR REPLACE INTO monitor(persona,data,updated_at) SELECT json_extract(value,'$.persona'), json_extract(value,'$.data'), ?2 FROM json_each(?1)")
        .bind(JSON.stringify(rows), now));
      revBump = true;
    }

    const b = seg.barrier;
    if (!b) continue;
    if (b.reset.size) {
      const p = jlist(b.reset);
      // Thread rows go first: they find their person through the alerts that are about to be removed.
      stmts.push(db.prepare("DELETE FROM thread WHERE alert_id IN (SELECT id FROM alerts WHERE persona IN (SELECT value FROM json_each(?1)))").bind(p));
      for (const t of ["series", "feed", "alerts", "annotations", "state", "monitor"]) {
        stmts.push(db.prepare(`DELETE FROM ${t} WHERE persona IN (SELECT value FROM json_each(?1))`).bind(p));
      }
      epochBump = true;
      revBump = true;
    }
    if (b.forget.size) {
      // What the person said is erased: the stories, their side of every chat and their health notes. The system's own
      // alert texts and the synthetic series stay.
      const p = jlist(b.forget);
      stmts.push(db.prepare("DELETE FROM annotations WHERE persona IN (SELECT value FROM json_each(?1))").bind(p));
      stmts.push(db.prepare(`DELETE FROM thread WHERE ${STORY_THREAD} AND alert_id IN (SELECT id FROM alerts WHERE persona IN (SELECT value FROM json_each(?1)))`).bind(p));
      stmts.push(db.prepare("DELETE FROM feed WHERE kind IN ('annotation','story') AND persona IN (SELECT value FROM json_each(?1))").bind(p));
      stmts.push(db.prepare("DELETE FROM profile WHERE persona IN (SELECT value FROM json_each(?1))").bind(p));
      revBump = true;
    }
    if (b.story.size) {
      const a = jlist(b.story);
      stmts.push(db.prepare("DELETE FROM annotations WHERE alert_id IN (SELECT value FROM json_each(?1))").bind(a));
      stmts.push(db.prepare(`DELETE FROM thread WHERE ${STORY_THREAD} AND alert_id IN (SELECT value FROM json_each(?1))`).bind(a));
      stmts.push(db.prepare(`DELETE FROM feed WHERE kind IN ('annotation','story')
        AND CASE WHEN json_valid(data) THEN json_extract(data,'$.alert_id') END IN (SELECT value FROM json_each(?1))`).bind(a));
      revBump = true;
    }
  }

  if (epochBump) {
    stmts.push(db.prepare("INSERT INTO kv(key,value,updated_at) VALUES('epoch','1',?1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1, updated_at=?1").bind(now));
  }
  if (revBump) {
    stmts.push(db.prepare("INSERT INTO kv(key,value,updated_at) VALUES('rev','1',?1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1, updated_at=?1").bind(now));
  }
  if (stmts.length) await db.batch(stmts);
  return json(request, env, {
    ok: true, items: items.length, statements: stmts.length,
    dropped: dropped.invalid, unknown_personas: [...dropped.unknown_personas].slice(0, 5),
  });
}

// ------------------------------------------------------------------- live
async function live(request, env, url) {
  const persona = url.searchParams.get("persona") || "maya";
  if (persona.length > 48) return json(request, env, { error: "unknown persona" }, 400);
  const afterI = int(url.searchParams.get("after_i"), -1);
  const afterFeed = int(url.searchParams.get("after_feed"), 0);
  const clientRev = int(url.searchParams.get("rev"), -1);
  const db = env.DB;

  const feedSql = afterFeed > 0
    ? db.prepare("SELECT * FROM feed WHERE persona=?1 AND id>?2 ORDER BY id LIMIT 250").bind(persona, afterFeed)
    : db.prepare("SELECT * FROM (SELECT * FROM feed WHERE persona=?1 ORDER BY id DESC LIMIT 250) ORDER BY id").bind(persona);
  const [states, kv, series, feed, commands, maxRow] = await db.batch([
    db.prepare("SELECT persona,data FROM state"),
    db.prepare("SELECT key,value,updated_at FROM kv"),
    db.prepare("SELECT i,ts,state,quality,v FROM series WHERE persona=?1 AND i>?2 ORDER BY i LIMIT 3000").bind(persona, afterI),
    feedSql,
    db.prepare("SELECT id,type,persona,status,result,created_at FROM commands ORDER BY id DESC LIMIT 6"),
    db.prepare("SELECT MAX(i) AS m FROM series WHERE persona=?1").bind(persona),
  ]);

  const kvMap = Object.fromEntries(kv.results.map((r) => [r.key, r]));
  const cfg = parse(kvMap.personas && kvMap.personas.value);
  // Checked after the reads (not before) so the whole poll stays one round trip to D1.
  if (!idsOf(cfg).has(persona)) {
    // The answer names the people that do exist, so a dashboard asking for a stale one can pick another by itself.
    return json(request, env, { error: "unknown persona", personas: cfg && Array.isArray(cfg.items) ? cfg.items : [], linked: cfg && cfg.linked ? cfg.linked : null }, 400);
  }
  const rev = int(kvMap.rev && kvMap.rev.value, 0);
  const health = parse(kvMap.health && kvMap.health.value);
  const out = {
    now: Date.now(),
    rev,
    epoch: int(kvMap.epoch && kvMap.epoch.value, 0),
    max_i: maxRow.results[0] ? maxRow.results[0].m : null,
    brain: { online: !!(kvMap.health && Date.now() - kvMap.health.updated_at < 40000), seen_ms_ago: kvMap.health ? Date.now() - kvMap.health.updated_at : null, health },
    send_alerts: kvMap.send_alerts ? kvMap.send_alerts.value === "1" : false,
    personas: cfg && Array.isArray(cfg.items) ? cfg.items : [],
    linked: cfg && cfg.linked ? cfg.linked : null,
    states: Object.fromEntries(states.results.map((r) => [r.persona, parse(r.data)])),
    series: series.results.map((r) => ({ i: r.i, ts: r.ts, state: r.state, quality: r.quality, v: parse(r.v, {}) })),
    feed: feed.results.map((r) => ({ ...r, data: parse(r.data, {}) })),
    commands: commands.results,
  };
  if (rev !== clientRev) {
    const [alerts, notes, thread, monitor, profile] = await db.batch([
      db.prepare("SELECT * FROM alerts WHERE persona=?1 ORDER BY id").bind(persona),
      db.prepare("SELECT * FROM annotations WHERE persona=?1 ORDER BY alert_id").bind(persona),
      db.prepare("SELECT t.* FROM thread t JOIN alerts a ON a.id=t.alert_id WHERE a.persona=?1 ORDER BY t.created_at, t.mid").bind(persona),
      db.prepare("SELECT data FROM monitor WHERE persona=?1").bind(persona),
      db.prepare("SELECT items,flags,updated_at FROM profile WHERE persona=?1").bind(persona),
    ]);
    out.alerts = alerts.results.map((r) => ({ ...r, evidence: parse(r.evidence, {}), decision: parse(r.decision, {}) }));
    out.annotations = notes.results.map((r) => ({
      ...r, factors: parse(r.factors, []), computed: parse(r.computed, []), qa: parse(r.qa, []), versions: parse(r.versions, []),
    }));
    out.thread = thread.results.map((r) => ({ ...r, options: parse(r.options, null) }));
    out.monitor = monitor.results.length ? parse(monitor.results[0].data) : null;
    const p = profile.results[0];
    out.profile = p ? { items: parse(p.items, []), flags: parse(p.flags, []), updated_at: new Date(p.updated_at).toISOString() } : null;
  }
  return json(request, env, out);
}

// --------------------------------------------------------------- commands
async function postCommand(request, env) {
  let body;
  try { body = await request.json(); } catch { return json(request, env, { error: "invalid json" }, 400); }
  if (!body || !COMMANDS.has(body.type)) return json(request, env, { error: "unknown command" }, 400);
  if (body.type !== "send_alerts" && !(isStr(body.persona) && idsOf(await storedPersonas(env.DB)).has(body.persona))) {
    return json(request, env, { error: "unknown persona" }, 400);
  }
  const args = {};
  if (body.type === "play" && body.args) {
    if (Number.isInteger(body.args.start_day)) args.start_day = Math.max(0, Math.min(27, body.args.start_day));
    if (Number(body.args.speed) > 0) args.speed = Math.min(60, Number(body.args.speed));
  }
  if (body.type === "speed") args.speed = Math.max(0.2, Math.min(60, Number(body.args && body.args.speed) || 2));
  if (body.type === "send_alerts") args.value = !!(body.args && body.args.value);
  const now = Date.now();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM commands WHERE created_at < ?1").bind(now - 86400000),
    env.DB.prepare("INSERT INTO commands(type,persona,args,created_at) VALUES(?1,?2,?3,?4)").bind(body.type, body.persona || null, JSON.stringify(args), now),
  ]);
  return json(request, env, { ok: true });
}

async function claimCommands(request, env) {
  const now = Date.now();
  // Anything left "running" for over a minute belonged to a brain that died; let it go.
  await env.DB.prepare("UPDATE commands SET status='failed', result='brain did not finish', done_at=?1 WHERE status='running' AND created_at<?2").bind(now, now - 60000).run();
  const rows = await env.DB.prepare("SELECT id,type,persona,args FROM commands WHERE status='pending' ORDER BY id LIMIT 10").all();
  if (rows.results.length) {
    await env.DB.batch(rows.results.map((r) => env.DB.prepare("UPDATE commands SET status='running' WHERE id=?1 AND status='pending'").bind(r.id)));
  }
  return json(request, env, { commands: rows.results });
}

async function ackCommand(request, env) {
  let body;
  try { body = await request.json(); } catch { return json(request, env, { error: "invalid json" }, 400); }
  if (!body || !Number.isInteger(body.id)) return json(request, env, { error: "id required" }, 400);
  const status = body.status === "done" ? "done" : "failed";
  await env.DB.prepare("UPDATE commands SET status=?1, result=?2, done_at=?3 WHERE id=?4")
    .bind(status, String(body.result ?? "").slice(0, 200), Date.now(), body.id).run();
  return json(request, env, { ok: true });
}

// ----------------------------------------------------------------- router
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    try {
      if (url.pathname === "/api/health") return json(request, env, { ok: true });

      const token = bearer(request);
      const isIngest = ["/api/ingest", "/api/commands", "/api/commands/ack"].includes(url.pathname);
      const isDash = ["/api/live", "/api/command"].includes(url.pathname);
      if (!isIngest && !isDash) return json(request, env, { error: "not found" }, 404);
      const ok = await secretMatches(token, isIngest ? env.INGEST_TOKEN : env.DASH_TOKEN);
      if (!ok) return json(request, env, { error: "unauthorized" }, 401);

      if (url.pathname === "/api/ingest" && request.method === "POST") return await ingest(request, env);
      if (url.pathname === "/api/commands" && request.method === "GET") return await claimCommands(request, env);
      if (url.pathname === "/api/commands/ack" && request.method === "POST") return await ackCommand(request, env);
      if (url.pathname === "/api/live" && request.method === "GET") return await live(request, env, url);
      if (url.pathname === "/api/command" && request.method === "POST") return await postCommand(request, env);
      return json(request, env, { error: "method not allowed" }, 405);
    } catch (err) {
      console.error("request failed", err && err.message);
      return json(request, env, { error: "server error" }, 500);
    }
  },
};
