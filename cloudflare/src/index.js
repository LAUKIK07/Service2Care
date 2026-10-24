// Signal2Care API on Cloudflare Workers + D1.
//
//   brain (this machine) --POST /api/ingest--> D1 <--GET /api/live-- dashboard (GitHub Pages)
//   dashboard --POST /api/command--> D1 --GET /api/commands--> brain
//
// Two secrets: INGEST_TOKEN (the brain) and DASH_TOKEN (the dashboard). No accounts, no sessions.

const PERSONAS = new Set(["maya", "daniel", "priya"]);
const COMMANDS = new Set(["play", "stop", "reset", "speed", "send_alerts"]);
const MAX_INGEST_ITEMS = 500;

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

// ----------------------------------------------------------------- ingest
// Rows travel as one JSON array parameter per statement (json_each), which keeps every call far
// under D1's per-query bound-parameter and per-invocation query limits.
const BULK = {
  series: `INSERT OR REPLACE INTO series(persona,i,ts,state,quality,v)
    SELECT json_extract(value,'$.persona'), json_extract(value,'$.i'), json_extract(value,'$.ts'),
           json_extract(value,'$.state'), json_extract(value,'$.quality'), json_extract(value,'$.v') FROM json_each(?1)`,
  feed: `INSERT OR REPLACE INTO feed(id,persona,ts,actor,kind,text,data,created_at)
    SELECT json_extract(value,'$.id'), json_extract(value,'$.persona'), json_extract(value,'$.ts'),
           json_extract(value,'$.actor'), json_extract(value,'$.kind'), json_extract(value,'$.text'),
           json_extract(value,'$.data'), json_extract(value,'$.created_at') FROM json_each(?1)`,
  alert: `INSERT OR REPLACE INTO alerts(id,persona,kind,metric,family,ts,started,evidence,decision,status,text,sent_at,closed_at,created_at)
    SELECT json_extract(value,'$.id'), json_extract(value,'$.persona'), json_extract(value,'$.kind'),
           json_extract(value,'$.metric'), json_extract(value,'$.family'), json_extract(value,'$.ts'),
           json_extract(value,'$.started'), json_extract(value,'$.evidence'), json_extract(value,'$.decision'),
           json_extract(value,'$.status'), json_extract(value,'$.text'), json_extract(value,'$.sent_at'),
           json_extract(value,'$.closed_at'), json_extract(value,'$.created_at') FROM json_each(?1)`,
  annotation: `INSERT OR REPLACE INTO annotations(alert_id,persona,metric,start_ts,end_ts,summary,factors,computed,created_at,updated_at)
    SELECT json_extract(value,'$.alert_id'), json_extract(value,'$.persona'), json_extract(value,'$.metric'),
           json_extract(value,'$.start_ts'), json_extract(value,'$.end_ts'), json_extract(value,'$.summary'),
           json_extract(value,'$.factors'), json_extract(value,'$.computed'), json_extract(value,'$.created_at'),
           json_extract(value,'$.updated_at') FROM json_each(?1)`,
  thread: `INSERT OR REPLACE INTO thread(alert_id,mid,direction,text,created_at)
    SELECT json_extract(value,'$.alert_id'), json_extract(value,'$.mid'), json_extract(value,'$.direction'),
           json_extract(value,'$.text'), json_extract(value,'$.created_at') FROM json_each(?1)`,
};
// Alerts come before their status changes; nothing else depends on order within one segment.
const ORDER = ["series", "feed", "alert", "alert_status", "annotation", "thread"];

function personaOf(d) {
  return d && PERSONAS.has(d.persona) ? d.persona : null;
}

async function ingest(request, env) {
  let body;
  try { body = await request.json(); } catch { return json(request, env, { error: "invalid json" }, 400); }
  const items = Array.isArray(body.items) ? body.items.slice(0, MAX_INGEST_ITEMS) : [];
  const now = Date.now();
  const db = env.DB;
  const stmts = [];
  let revBump = false;

  // A reset is a barrier: what comes before it is wiped with it, what follows is kept.
  const segments = [[]];
  for (const item of items) {
    if (item && item.k === "reset") segments.push({ reset: item.d && item.d.persona }, []);
    else segments[segments.length - 1].push(item);
  }

  for (const seg of segments) {
    if (!Array.isArray(seg)) {
      if (!PERSONAS.has(seg.reset)) continue;
      for (const t of ["series", "feed", "alerts", "annotations", "state", "monitor"]) {
        stmts.push(db.prepare(`DELETE FROM ${t} WHERE persona=?1`).bind(seg.reset));
      }
      stmts.push(db.prepare("DELETE FROM thread WHERE alert_id IN (SELECT id FROM alerts WHERE persona=?1)").bind(seg.reset));
      stmts.push(db.prepare("INSERT INTO kv(key,value,updated_at) VALUES('epoch','1',?1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1, updated_at=?1").bind(now));
      revBump = true;
      continue;
    }
    const groups = {};
    const latest = { state: {}, monitor: {} };
    const statuses = {};
    for (const { k, d } of seg) {
      if (!d || typeof d !== "object") continue;
      if (k === "state" || k === "monitor") {
        const p = personaOf(d);
        if (p) latest[k][p] = d;
      } else if (k === "alert_status") {
        if (Number.isInteger(d.id)) statuses[d.id] = String(d.status).slice(0, 20);
      } else if (k === "setting") {
        if (d.key === "send_alerts") {
          stmts.push(db.prepare("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('send_alerts',?1,?2)").bind(d.value === "1" ? "1" : "0", now));
        }
      } else if (k === "health") {
        stmts.push(db.prepare("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('health',?1,?2)").bind(JSON.stringify(d), now));
      } else if (BULK[k]) {
        if (k === "thread" || k === "annotation" || k === "alert" || k === "feed" || k === "series") {
          if (k === "thread" ? Number.isInteger(d.alert_id) : personaOf(d)) (groups[k] ||= []).push(d);
        }
      }
    }
    for (const k of ORDER) {
      if (k === "alert_status") {
        for (const [id, status] of Object.entries(statuses)) {
          stmts.push(db.prepare("UPDATE alerts SET status=?1, closed_at=CASE WHEN ?1='resolved' THEN ?3 ELSE closed_at END WHERE id=?2")
            .bind(status, Number(id), new Date().toISOString()));
          revBump = true;
        }
      } else if (groups[k]) {
        stmts.push(db.prepare(BULK[k]).bind(JSON.stringify(groups[k])));
        if (k !== "series" && k !== "feed") revBump = true;
      }
    }
    for (const [p, d] of Object.entries(latest.state)) {
      stmts.push(db.prepare("INSERT OR REPLACE INTO state(persona,data,updated_at) VALUES(?1,?2,?3)").bind(p, JSON.stringify(d), now));
    }
    for (const [p, d] of Object.entries(latest.monitor)) {
      stmts.push(db.prepare("INSERT OR REPLACE INTO monitor(persona,data,updated_at) VALUES(?1,?2,?3)").bind(p, JSON.stringify(d), now));
      revBump = true;
    }
  }
  if (revBump) {
    stmts.push(db.prepare("INSERT INTO kv(key,value,updated_at) VALUES('rev','1',?1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1, updated_at=?1").bind(now));
  }
  if (stmts.length) await db.batch(stmts);
  return json(request, env, { ok: true, items: items.length, statements: stmts.length });
}

// ------------------------------------------------------------------- live
async function live(request, env, url) {
  const persona = url.searchParams.get("persona") || "maya";
  if (!PERSONAS.has(persona)) return json(request, env, { error: "unknown persona" }, 400);
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
  const rev = int(kvMap.rev && kvMap.rev.value, 0);
  const health = parse(kvMap.health && kvMap.health.value);
  const out = {
    now: Date.now(),
    rev,
    epoch: int(kvMap.epoch && kvMap.epoch.value, 0),
    max_i: maxRow.results[0] ? maxRow.results[0].m : null,
    brain: { online: !!(kvMap.health && Date.now() - kvMap.health.updated_at < 40000), seen_ms_ago: kvMap.health ? Date.now() - kvMap.health.updated_at : null, health },
    send_alerts: kvMap.send_alerts ? kvMap.send_alerts.value === "1" : false,
    states: Object.fromEntries(states.results.map((r) => [r.persona, parse(r.data)])),
    series: series.results.map((r) => ({ i: r.i, ts: r.ts, state: r.state, quality: r.quality, v: parse(r.v, {}) })),
    feed: feed.results.map((r) => ({ ...r, data: parse(r.data, {}) })),
    commands: commands.results,
  };
  if (rev !== clientRev) {
    const [alerts, notes, thread, monitor] = await db.batch([
      db.prepare("SELECT * FROM alerts WHERE persona=?1 ORDER BY id").bind(persona),
      db.prepare("SELECT * FROM annotations WHERE persona=?1 ORDER BY alert_id").bind(persona),
      db.prepare("SELECT t.* FROM thread t JOIN alerts a ON a.id=t.alert_id WHERE a.persona=?1 ORDER BY t.created_at, t.mid").bind(persona),
      db.prepare("SELECT data FROM monitor WHERE persona=?1").bind(persona),
    ]);
    out.alerts = alerts.results.map((r) => ({ ...r, evidence: parse(r.evidence, {}), decision: parse(r.decision, {}) }));
    out.annotations = notes.results.map((r) => ({ ...r, factors: parse(r.factors, []), computed: parse(r.computed, []) }));
    out.thread = thread.results;
    out.monitor = monitor.results.length ? parse(monitor.results[0].data) : null;
  }
  return json(request, env, out);
}

// --------------------------------------------------------------- commands
async function postCommand(request, env) {
  let body;
  try { body = await request.json(); } catch { return json(request, env, { error: "invalid json" }, 400); }
  if (!COMMANDS.has(body.type)) return json(request, env, { error: "unknown command" }, 400);
  if (body.type !== "send_alerts" && !PERSONAS.has(body.persona)) return json(request, env, { error: "unknown persona" }, 400);
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
  if (!Number.isInteger(body.id)) return json(request, env, { error: "id required" }, 400);
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
