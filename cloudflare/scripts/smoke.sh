#!/usr/bin/env bash
# Smoke test for the Signal2Care Worker (schema version 2). LOCAL ONLY: it reads the local test tokens from .dev.vars
# and talks to a `wrangler dev --local` server. It refuses to run against anything that is not localhost.
#
#   cd cloudflare
#   npx wrangler d1 execute signal2care --local --persist-to /tmp/s2c-dev --file=schema.sql      # once, fresh local database
#   npx wrangler dev --local --port 8788 --persist-to /tmp/s2c-dev                               # in another terminal
#   bash scripts/smoke.sh                                                                         # prints PASS / FAIL lines
#
# Environment: BASE (default http://127.0.0.1:8788), DEV_VARS (default ../.dev.vars next to this script).
# The test uses its own person ("smoke-test") and removes what it wrote when it is done; it puts the persona list back as it found it.
# Token values are read by variable expansion only and are never printed.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${BASE:-http://127.0.0.1:8788}"
VARS="${DEV_VARS:-$HERE/../.dev.vars}"
ORIGIN_OK="http://127.0.0.1:8080"
ORIGIN_BAD="https://evil.example"
P="smoke-test"
A1=900001; A2=900002; A3=900003

case "$BASE" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "FAIL refusing to run against $BASE (local only)"; exit 2 ;;
esac

getvar() { grep -E "^$1=" "$VARS" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"; }
INGEST_TOKEN="$(getvar INGEST_TOKEN)"
DASH_TOKEN="$(getvar DASH_TOKEN)"
if [ -z "$INGEST_TOKEN" ] || [ -z "$DASH_TOKEN" ]; then echo "FAIL could not read INGEST_TOKEN / DASH_TOKEN from $VARS"; exit 2; fi
if [ "$INGEST_TOKEN" = "$DASH_TOKEN" ]; then echo "FAIL the two tokens must differ"; exit 2; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASSES=0; FAILS=0

pass() { PASSES=$((PASSES + 1)); echo "PASS $1"; }
fail() { FAILS=$((FAILS + 1)); echo "FAIL $1${2:+  ($2)}"; }
skip() { echo "SKIP $1"; }
section() { echo; echo "== $1"; }

# req METHOD PATH TOKEN BODYFILE [ORIGIN]   -> CODE, $TMP/body, $TMP/hdr. The token goes to curl through a config pipe, not argv.
req() {
  local method="$1" path="$2" tok="$3" bodyfile="$4" origin="$5"
  local args=(-sS -m 30 -X "$method" -o "$TMP/body" -D "$TMP/hdr" -w '%{http_code}')
  [ -n "$origin" ] && args+=(-H "Origin: $origin")
  [ -n "$bodyfile" ] && args+=(-H "content-type: application/json" --data-binary "@$bodyfile")
  if [ -n "$tok" ]; then
    CODE=$(curl "${args[@]}" -K <(printf 'header = "Authorization: Bearer %s"\n' "$tok") "$BASE$path" 2>"$TMP/err")
  else
    CODE=$(curl "${args[@]}" "$BASE$path" 2>"$TMP/err")
  fi
  [ -n "$CODE" ] || CODE=000
}

# js 'expr' -> true/false : evaluates expr with r = the last JSON response body
js() {
  node -e 'let r;try{r=JSON.parse(require("fs").readFileSync(0,"utf8"))}catch(e){console.log("false");process.exit(0)}
try{console.log(String(!!eval(process.argv[1])))}catch(e){console.log("false")}' "$1" < "$TMP/body"
}
# val 'expr' -> prints the value (JSON) of expr
val() {
  node -e 'let r;try{r=JSON.parse(require("fs").readFileSync(0,"utf8"))}catch(e){console.log("null");process.exit(0)}
try{const v=eval(process.argv[1]);console.log(typeof v==="string"?v:JSON.stringify(v))}catch(e){console.log("null")}' "$1" < "$TMP/body"
}
expect_code() { # name expected
  if [ "$CODE" = "$2" ]; then pass "$1 -> HTTP $2"; else fail "$1 -> HTTP $2" "got $CODE"; fi
}
expect_js() { # name expr
  if [ "$(js "$2")" = "true" ]; then pass "$1"; else fail "$1" "$2"; fi
}
header_has() { grep -qi "^$1:" "$TMP/hdr"; }
header_val() { grep -i "^$1:" "$TMP/hdr" | head -1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//'; }

ingest() { # JSON-file -> CODE and body
  req POST /api/ingest "$INGEST_TOKEN" "$1"
}
live() { # query string (without ?) -> body
  req GET "/api/live?$1" "$DASH_TOKEN"
}
put() { cat > "$TMP/$1.json"; }       # put name <<'JSON' ... JSON

# ---------------------------------------------------------------- 1. health
section "health"
req GET /api/health ""
expect_code "health without a token" 200
expect_js "health body is {ok:true}" 'r.ok === true'
req GET /api/nothing ""
expect_code "unknown path" 404

# ---------------------------------------------------------------- 2. auth
section "auth separation"
req GET "/api/live?persona=maya" "$INGEST_TOKEN";   expect_code "ingest token on /api/live is rejected" 401
req GET "/api/live?persona=maya" "";                expect_code "no token on /api/live" 401
req GET "/api/live?persona=maya" "wrong-token";     expect_code "bad token on /api/live" 401
req POST /api/command "$INGEST_TOKEN" /dev/null;    expect_code "ingest token on /api/command is rejected" 401
echo '{"items":[]}' | put empty
req POST /api/ingest "$DASH_TOKEN" "$TMP/empty.json"; expect_code "dashboard token on /api/ingest is rejected" 401
req POST /api/ingest "" "$TMP/empty.json";            expect_code "no token on /api/ingest" 401
req POST /api/ingest "wrong-token" "$TMP/empty.json"; expect_code "bad token on /api/ingest" 401
req GET /api/commands "$DASH_TOKEN";                  expect_code "dashboard token on /api/commands is rejected" 401
req POST /api/commands/ack "$DASH_TOKEN" "$TMP/empty.json"; expect_code "dashboard token on /api/commands/ack is rejected" 401
req GET "/api/live?persona=maya" "$DASH_TOKEN";       expect_code "dashboard token on /api/live" 200
req POST /api/ingest "$INGEST_TOKEN" "$TMP/empty.json"; expect_code "ingest token on /api/ingest" 200
# a token that merely starts like the real one must not pass
req GET "/api/live?persona=maya" "${DASH_TOKEN}x";    expect_code "token with extra suffix is rejected" 401
req GET "/api/live?persona=maya" "${DASH_TOKEN%?}";   expect_code "token with last character cut off is rejected" 401

# ---------------------------------------------------------------- 3. CORS
section "CORS"
req OPTIONS /api/live "" "" "$ORIGIN_OK"
[ "$CODE" = "204" ] && [ "$(header_val access-control-allow-origin)" = "$ORIGIN_OK" ] && pass "preflight from an allowed origin is answered" || fail "preflight from an allowed origin" "code $CODE"
req OPTIONS /api/live "" "" "$ORIGIN_BAD"
header_has access-control-allow-origin && fail "preflight from a foreign origin must not be allowed" || pass "preflight from a foreign origin gets no CORS headers"
req GET "/api/live?persona=maya" "$DASH_TOKEN" "" "$ORIGIN_OK"
[ "$(header_val access-control-allow-origin)" = "$ORIGIN_OK" ] && pass "GET with an allowed origin echoes that origin" || fail "GET with an allowed origin"
req GET "/api/live?persona=maya" "$DASH_TOKEN" "" "$ORIGIN_BAD"
header_has access-control-allow-origin && fail "GET with a foreign origin must not echo it" || pass "GET with a foreign origin gets no CORS headers"
req GET "/api/live?persona=maya" "wrong" "" "$ORIGIN_BAD"
header_has access-control-allow-origin && fail "401 for a foreign origin must not carry CORS headers" || pass "401 for a foreign origin carries no CORS headers"

# ---------------------------------------------------------------- 4. personas
section "personas list"
live "persona=maya&rev=-1"
expect_code "live for a built-in person" 200
BEFORE_LIST="$(val 'r.personas || []')"
BEFORE_LINKED="$(val 'r.linked')"
live "persona=zed&rev=-1"; expect_code "unknown person is rejected before it is in the list" 400
live "persona=$P&rev=-1";  expect_code "the test person is unknown before it is registered" 400
live "persona=";           expect_code "empty persona falls back to the default person" 200
live "persona=$(printf 'x%.0s' $(seq 1 60))"; expect_code "overlong persona is rejected" 400
live "persona=maya'%20OR%201=1"; expect_code "persona with SQL characters is rejected" 400

# items for a person nobody registered are counted and dropped, the request itself still succeeds
node -e '
const P="ghost-x";
console.log(JSON.stringify({items:[
 {k:"series",d:{persona:P,i:0,ts:"2026-10-01T00:00",state:"sleep",quality:"good",v:{heart_rate:50}}},
 {k:"state",d:{persona:P,status:"idle"}},
 {k:"alert",d:{id:899999,persona:P,metric:"skin_temp",status:"sent",ts:"2026-10-02T00:00"}}
]}))' | put ghost
ingest "$TMP/ghost.json"
expect_code "ingest for an unregistered person still answers 200" 200
expect_js "…but reports the items as dropped with the unknown name" 'r.unknown_personas.includes("ghost-x")'
live "persona=ghost-x&rev=-1"; expect_code "the unregistered person never becomes readable" 400

if [ "$(echo "$BEFORE_LIST" | node -e 'console.log(JSON.parse(require("fs").readFileSync(0,"utf8")).length)')" = "0" ]; then
  # Fresh database: only the three built-in people are valid until the brain sends its list.
  echo '{"items":[{"k":"series","d":{"persona":"priya","i":0,"ts":"2026-10-01T00:00","state":"sleep","quality":"good","v":{"heart_rate":50}}}]}' | put fallback
  ingest "$TMP/fallback.json"
  expect_js "built-in people are accepted while no list was sent" 'r.dropped === 0 && r.unknown_personas.length === 0'
else
  skip "built-in fallback check (a persona list is already stored in this database)"
fi

# ---------------------------------------------------------------- 5. a full story, every kind
section "ingest of every kind"
node - > "$TMP/full.json" <<'JS'
const P = "smoke-test", A1 = 900001, A2 = 900002, A3 = 900003;
const base = (id, name, tag, kind, hue) => ({ id, name, tag, blurb: `${name} (test)`, kind, source: kind === "real" ? "Example public dataset" : "Seeded physiology simulator",
  license: kind === "real" ? "CC BY 4.0" : "Invented", citation: kind === "real" ? "Doe et al. 2022" : "", channels: kind === "real" ? ["heart_rate", "skin_temp"] : [], hue });
const personas = { items: [base("maya", "Arya", "Runner", "synthetic", "#FF385C"), base("daniel", "Daniel", "Desk job", "synthetic", "#7C5CFA"),
  base("priya", "Priya", "Control", "synthetic", "#0EA5E9"), base(P, "Smoke", "Real data", "real", "#14B8A6")], linked: P };
const receipt = (eid) => ({ event_id: eid, metric: "skin_temp", kind: "hourly", context: "sleep",
  window: { started: "2026-10-20T08:00", detected: "2026-10-20T10:00", hours: 3, quality: "good" },
  readings: [{ hour: "2026-10-20T08", median: 34.0, samples: 4 }, { hour: "2026-10-20T09", median: 34.1, samples: 4 }, { hour: "2026-10-20T10", median: 34.2, samples: 4 }],
  baseline: { definition: "median of this person's earlier readings in the same context", median: 33.3, scale: 0.13, samples: 142 },
  calculation: { formula: "z = (reading - median) / scale", z: 6.9, percent_change: 2.4 }, rule: "a robust z of at least 4.5 for 3 hours",
  source: "Example public dataset", persona: P, synthetic: false });
const alert = (id, eid, status, extra = {}) => ({ id, persona: P, kind: "hourly", metric: "skin_temp", family: "temperature", ts: "2026-10-20T10:00", started: "2026-10-20T08:00",
  evidence: JSON.stringify({ persona: P, kind: "hourly", metric: "skin_temp", value: 34.2, baseline: 33.3, scale: 0.13, z: 6.9, pct: 2.4, persistence_h: 3, event_id: eid, receipt: receipt(eid) }),
  decision: JSON.stringify({ source: "jev", action: "alert_now", send: true, p_alert: 0.91, confidence: 0.8, question: { question_kind: "feeling_warm", source: "jev", confidence: 0.7, options: ["Yes", "No", "Not sure"] } }),
  status, text: "Signal2Care (demo data): Your skin temperature is above your usual.", sent_at: "2026-10-24T20:30:35+00:00", closed_at: null, created_at: "2026-10-24T20:30:35+00:00", event_id: eid, ...extra });
const E1 = "evt-smoke-test-202610200800-skin_temp", E2 = E1 + "-2", E3 = E1 + "-3";
const note = (alert_id, eid, over = {}) => ({ alert_id, persona: P, metric: "skin_temp", start_ts: "2026-10-20T08:00", end_ts: "2026-10-20T10:00", summary: "", factors: JSON.stringify(["workout"]),
  computed: JSON.stringify([{ factor: "workout", verdict: "within_typical_range", typical_shift: 0.3, observed_shift: 0.4 }]), created_at: "2026-10-24T20:30:37+00:00",
  updated_at: "2026-10-24T20:31:37+00:00", event_id: eid, status: "open", opened_at: "2026-10-24T20:30:37+00:00", closes_at: "2026-10-24T20:40:37+00:00",
  qa: JSON.stringify([{ q: "Are you feeling warmer than usual?", a: "Yes, I ran this morning", at: "2026-10-24T20:31:00+00:00" }, { q: "Anything else?", a: "No", at: "2026-10-24T20:31:30+00:00" }]),
  versions: JSON.stringify([{ v: 1, kind: "from_data", text: "Skin temperature is 0.9 degrees above usual for 3 hours.", at: "2026-10-24T20:30:37+00:00", changed: false, basis: { signature: [] } },
    { v: 2, kind: "recheck", text: "The rise is within what your runs usually produce.", at: "2026-10-24T20:31:37+00:00", changed: true, basis: { signature: ["workout"], note: "you mentioned a run" }, before: "Skin temperature is 0.9 degrees above usual for 3 hours." }]), ...over });
const th = (alert_id, mid, direction, text, kind, created_at, options = null) => ({ alert_id, mid, direction, text, kind, created_at, options });
const items = [
  { k: "personas", d: personas },
  ...[0, 1, 2, 3, 4, 5].map((i) => ({ k: "series", d: { persona: P, i, ts: `2026-10-01T0${Math.floor(i / 4)}:${["00", "15", "30", "45"][i % 4]}`, state: "sleep", quality: "good", v: { heart_rate: 50 + i, skin_temp: 33.6 } } })),
  { k: "state", d: { persona: P, status: "running", cursor: 6, total: 2688, ts: "2026-10-01T01:15", day: 0, speed: 4, sim_state: "sleep" } },
  { k: "monitor", d: { persona: P, ts: "2026-10-01T01:15", review: { heart_rate: { value: 50, median: 51, scale: 2, z: -0.5, n: 100, ts: "2026-10-01T01:00", state: "sleep" } } } },
  { k: "feed", d: { id: 900101, persona: P, ts: "2026-10-01T01:00", actor: "jev", kind: "review", text: "Reviewed Heart rate", data: { metric: "heart_rate" }, created_at: "2026-10-24T20:30:00+00:00" } },
  { k: "feed", d: { id: 900102, persona: P, ts: "2026-10-20T10:00", actor: "llm", kind: "annotation", text: "Linked the conversation", data: { alert_id: A1 }, created_at: "2026-10-24T20:32:00+00:00" } },
  { k: "feed", d: { id: 900103, persona: P, ts: "2026-10-20T10:00", actor: "llm", kind: "story", text: "Story update for A3", data: { alert_id: A3 }, created_at: "2026-10-24T20:33:00+00:00" } },
  { k: "alert", d: alert(A1, E1, "sent") },
  { k: "alert", d: alert(A2, E2, "held_open_story", { text: "Draft waiting for the open chat" }) },
  { k: "alert", d: alert(A3, E3, "discussed") },
  { k: "alert_status", d: { id: A1, status: "discussed" } },
  { k: "alert_status", d: { id: A2, status: "held_rate" } },
  { k: "annotation", d: note(A1, E1) },
  { k: "annotation", d: note(A3, E3, { status: "closed", summary: "The person said they ran this morning.", closes_at: "2026-10-24T20:30:00+00:00" }) },
  { k: "thread", d: th(A1, `alert-${A1}`, "out", "Signal2Care (demo data): Your skin temperature is above your usual.", "alert", "2026-10-24T20:30:35+00:00") },
  { k: "thread", d: th(A1, "m-alertmsg", "out", "Picture card for the alert\n[card image]", "alert", "2026-10-24T20:30:36+00:00") },
  { k: "thread", d: th(A1, "m-q1", "out", "Are you feeling warmer than usual?", "question", "2026-10-24T20:30:40+00:00") },
  { k: "thread", d: th(A1, "m-poll", "out", "[poll] Yes | No | Not sure", "poll", "2026-10-24T20:30:41+00:00", ["Yes", "No", "Not sure"]) },
  { k: "thread", d: th(A1, "m-in1", "in", "Yes, I ran this morning", "reply_in", "2026-10-24T20:31:00+00:00") },
  { k: "thread", d: th(A1, "m-upd", "out", "Updated reading: within what your runs produce", "update", "2026-10-24T20:31:40+00:00") },
  { k: "thread", d: th(A1, "m-close", "out", "Saved to your record", "closing", "2026-10-24T20:40:40+00:00") },
  { k: "thread", d: th(A3, `alert-${A3}`, "out", "Alert text for the second event", "alert", "2026-10-24T20:30:35+00:00") },
  { k: "thread", d: th(A3, "m3-in", "in", "I was at the gym", "reply_in", "2026-10-24T20:31:00+00:00") },
  { k: "profile", d: { persona: P, items: [{ key: "lungs", label: "Lung note", value: "mild asthma", origin: "onboarding", at: "2026-10-24T20:00:00+00:00" },
    { key: "allergy", label: "Allergies", value: "pollen", origin: "correction", at: "2026-10-24T20:05:00+00:00" }], flags: ["lung_condition", "allergies"] } },
  { k: "setting", d: { key: "send_alerts", value: "1" } },
  { k: "health", d: { text_model: "test/model", jev: true, jev_model: "test/jev", ocr: "none", cloud: { pushed: 1, dropped: 0 } } },
];
console.log(JSON.stringify({ items }));
JS
ingest "$TMP/full.json"
expect_code "ingest of a full story kit" 200
expect_js "…nothing dropped, no unknown persons" 'r.ok === true && r.dropped === 0 && r.unknown_personas.length === 0'
expect_js "…uses few D1 statements (free tier allows 50 queries per invocation)" 'r.statements <= 30'
echo "     statements used: $(val 'r.statements')"

live "persona=$P&rev=-1"
expect_code "live for the registered test person" 200
expect_js "personas list is returned with the real-data entry" 'r.personas.length === 4 && r.personas.some(p => p.id === "smoke-test" && p.kind === "real" && p.license === "CC BY 4.0" && p.channels.length === 2 && /^#/.test(p.hue))'
expect_js "linked person is returned" 'r.linked === "smoke-test"'
expect_js "series, state and feed are returned" 'r.series.length === 6 && r.series[0].v.heart_rate === 50 && r.states["smoke-test"].status === "running" && r.feed.length === 3 && r.feed[0].data.metric === "heart_rate"'
expect_js "monitor, send_alerts and brain health" 'r.monitor.review.heart_rate.z === -0.5 && r.send_alerts === true && r.brain.online === true && r.brain.health.jev_model === "test/jev"'
expect_js "alerts carry event_id and parsed evidence with the receipt" 'r.alerts.length === 3 && r.alerts[0].event_id === "evt-smoke-test-202610200800-skin_temp" && r.alerts[0].evidence.receipt.calculation.z === 6.9 && r.alerts[0].evidence.receipt.readings.length === 3'
expect_js "decision is parsed incl. the question options" 'r.alerts[0].decision.question.options.length === 3'
expect_js "alert_status updated the status (long held_* names intact)" 'r.alerts[0].status === "discussed" && r.alerts[1].status === "held_rate"'
expect_js "annotation story fields are parsed (open, closes_at, qa, versions, factors, computed)" 'const n = r.annotations.find(a => a.alert_id === 900001); n.status === "open" && n.event_id.endsWith("skin_temp") && n.closes_at.startsWith("2026-10-24T20:40") && n.qa.length === 2 && n.qa[0].a === "Yes, I ran this morning" && n.versions.length === 2 && n.versions[1].changed === true && n.versions[1].before.length > 0 && n.versions[1].basis.note === "you mentioned a run" && n.factors[0] === "workout" && n.computed[0].verdict === "within_typical_range"'
expect_js "closed story keeps its summary" 'const n = r.annotations.find(a => a.alert_id === 900003); n.status === "closed" && n.summary.startsWith("The person said")'
expect_js "thread rows have kind and options (poll), replies are direction in" 'const t = r.thread.filter(x => x.alert_id === 900001); t.length === 7 && t.find(x => x.mid === "m-poll").kind === "poll" && JSON.stringify(t.find(x => x.mid === "m-poll").options) === JSON.stringify(["Yes","No","Not sure"]) && t.find(x => x.mid === "m-in1").direction === "in" && t.find(x => x.mid === "m-q1").options === null'
expect_js "profile (items, flags, updated_at) is delivered" 'r.profile.items.length === 2 && r.profile.items[0].origin === "onboarding" && r.profile.flags.includes("lung_condition") && !Number.isNaN(Date.parse(r.profile.updated_at))'
REV="$(val 'r.rev')"; EPOCH0="$(val 'r.epoch')"

section "rev gate and idempotence"
live "persona=$P&rev=$REV&after_i=5"
expect_js "same rev: alerts, annotations, thread, monitor and profile are left out" '!("alerts" in r) && !("annotations" in r) && !("thread" in r) && !("profile" in r) && !("monitor" in r)'
expect_js "…personas and linked still travel (cheap, and the tabs need them)" 'r.personas.length === 4 && r.linked === "smoke-test"'
expect_js "…and only series after after_i are returned" 'r.series.length === 0'
ingest "$TMP/full.json"
expect_code "the same ingest again" 200
live "persona=$P&rev=-1"
expect_js "…changes nothing (idempotent): same counts" 'r.series.length === 6 && r.alerts.length === 3 && r.annotations.length === 2 && r.thread.length === 9 && r.feed.length === 3 && r.personas.length === 4'

# ---------------------------------------------------------------- 5b. receipts (delivery ticks)
section "receipts upgrade delivery"
expect_js "outgoing thread rows start as sent, replies have no delivery" 'const t = r.thread; t.find(x => x.mid === "m-q1").delivery === "sent" && t.find(x => x.mid === "m-poll").delivery === "sent" && t.find(x => x.mid === "m-in1").delivery == null'
REV_R0="$(val 'r.rev')"
echo '{"items":[{"k":"receipt","d":{"alert_id":900001,"mid":"m-q1","state":"delivered","at":"2026-10-24T20:30:42+00:00"}}]}' | put rc1
ingest "$TMP/rc1.json"; expect_code "receipt: delivered" 200
live "persona=$P&rev=-1"
expect_js "…the thread row is now delivered" 'r.thread.find(x => x.alert_id === 900001 && x.mid === "m-q1").delivery === "delivered"'
expect_js "…and rev moved (so the dashboard refetches)" 'r.rev > '"$REV_R0"
echo '{"items":[{"k":"receipt","d":{"alert_id":900001,"mid":"m-q1","state":"read","at":"2026-10-24T20:30:50+00:00"}}]}' | put rc2
ingest "$TMP/rc2.json"
live "persona=$P&rev=-1"
expect_js "receipt: read upgrades delivered" 'r.thread.find(x => x.alert_id === 900001 && x.mid === "m-q1").delivery === "read"'
ingest "$TMP/rc1.json"
live "persona=$P&rev=-1"
expect_js "a late 'delivered' never downgrades read" 'r.thread.find(x => x.alert_id === 900001 && x.mid === "m-q1").delivery === "read"'
echo '{"items":[{"k":"receipt","d":{"alert_id":900001,"mid":"m-poll","state":"failed","at":"2026-10-24T20:30:50+00:00"}},{"k":"receipt","d":{"alert_id":900001,"mid":"m-nope","state":"read","at":"x"}},{"k":"receipt","d":{"alert_id":900001,"mid":"m-upd","state":"constructor"}}]}' | put rc3
ingest "$TMP/rc3.json"; expect_code "receipts for failed / unknown message / invalid state are tolerated" 200
live "persona=$P&rev=-1"
expect_js "…failed is recorded, a receipt for an unknown message adds nothing, an invalid state is ignored" 'const t = r.thread.filter(x => x.alert_id === 900001); t.find(x => x.mid === "m-poll").delivery === "failed" && !t.some(x => x.mid === "m-nope") && t.find(x => x.mid === "m-upd").delivery === "sent"'
ingest "$TMP/full.json"
live "persona=$P&rev=-1"
expect_js "re-sending the thread rows keeps the read receipt (idempotent, no downgrade)" 'r.thread.find(x => x.alert_id === 900001 && x.mid === "m-q1").delivery === "read" && r.thread.length === 9'

# ---------------------------------------------------------------- 6. command queue
section "command queue round trip"
echo "{\"type\":\"speed\",\"persona\":\"$P\",\"args\":{\"speed\":4}}" | put cmd
req POST /api/command "$DASH_TOKEN" "$TMP/cmd.json"
expect_code "dashboard queues a command for a registered person" 200
echo '{"type":"explode","persona":"smoke-test"}' | put cmdbad
req POST /api/command "$DASH_TOKEN" "$TMP/cmdbad.json"; expect_code "unknown command type is rejected" 400
echo '{"type":"play","persona":"nobody"}' | put cmdbad2
req POST /api/command "$DASH_TOKEN" "$TMP/cmdbad2.json"; expect_code "command for an unknown person is rejected" 400
echo '{"type":"send_alerts","args":{"value":false}}' | put cmdsa
req POST /api/command "$DASH_TOKEN" "$TMP/cmdsa.json"; expect_code "send_alerts needs no person" 200
req GET /api/commands "$INGEST_TOKEN"
expect_code "the brain claims commands" 200
expect_js "…and finds ours (speed for the test person, args kept)" 'const c = r.commands.find(c => c.type === "speed" && c.persona === "smoke-test"); !!c && JSON.parse(c.args).speed === 4'
CID="$(val 'r.commands.find(c => c.type === "speed" && c.persona === "smoke-test").id')"
req GET /api/commands "$INGEST_TOKEN"
expect_js "a claimed command is not handed out twice" '!r.commands.some(c => String(c.id) === "'"$CID"'")'
echo "{\"id\":$CID,\"status\":\"done\",\"result\":\"ok\"}" | put ack
req POST /api/commands/ack "$INGEST_TOKEN" "$TMP/ack.json"; expect_code "the brain acknowledges it" 200
live "persona=$P&rev=$REV"
expect_js "the dashboard sees the command as done" 'const c = r.commands.find(c => String(c.id) === "'"$CID"'"); !!c && c.status === "done" && c.result === "ok"'

# ---------------------------------------------------------------- 7. validation, robustness
section "validation"
echo 'not json' | put garbage
ingest "$TMP/garbage.json"; expect_code "invalid JSON is a 400" 400
node -e '
const P="smoke-test";
console.log(JSON.stringify({items:[
 null, 5, "x", {k:"series"}, {d:{}}, {k:"series",d:{persona:P,i:"x"}},
 {k:"series",d:{persona:P,i:50,ts:"2026-10-01T12:30",state:"awake",quality:"good",v:{heart_rate:70}}},
 {k:"thread",d:{alert_id:"abc",mid:"x"}}, {k:"thread",d:{alert_id:900001}},
 {k:"annotation",d:{persona:P}}, {k:"alert",d:{persona:P,status:"sent"}}, {k:"feed",d:{persona:P}},
 {k:"mystery",d:{persona:P}}, {k:"alert_status",d:{id:"nope",status:"sent"}}
]}))' | put junk
ingest "$TMP/junk.json"
expect_code "a batch with malformed items still answers 200 (the brain retries failures forever)" 200
expect_js "…malformed rows are counted as dropped" 'r.dropped >= 6'
live "persona=$P&rev=-1&after_i=40"
expect_js "…and the one valid row in that batch was stored" 'r.series.length === 1 && r.series[0].i === 50'
node -e 'console.log(JSON.stringify({items:Array.from({length:700},(_,i)=>({k:"series",d:{persona:"smoke-test",i:1000+i,ts:"2026-10-02T00:00",state:"awake",quality:"good",v:{heart_rate:60}}}))}))' | put many
ingest "$TMP/many.json"
expect_js "more than 500 items per request: the first 500 are taken" 'r.items === 500'
node -e 'process.stdout.write(JSON.stringify({items:[{k:"feed",d:{id:1,persona:"smoke-test",text:"x".repeat(5100000)}}]}))' > "$TMP/huge.json"
ingest "$TMP/huge.json"; expect_code "a body over 5 MB is a 413" 413

# ---------------------------------------------------------------- 8. forget_story and forget
section "forget_story and forget really delete"
node -e '
const P="smoke-test";
// order inside one request matters: what is sent before the forget is erased, what follows is kept
console.log(JSON.stringify({items:[
 {k:"forget_story",d:{alert_id:900001}},
 {k:"annotation",d:{alert_id:900001,persona:P,metric:"skin_temp",summary:"a new conversation after the old one was forgotten",status:"open",event_id:"evt-new",factors:"[]",computed:"[]",qa:"[]",versions:"[]"}},
 {k:"thread",d:{alert_id:900001,mid:"m-new-in",direction:"in",text:"fresh reply",kind:"reply_in",created_at:"2026-10-24T21:00:00+00:00"}}
]}))' | put fstory_order
echo "{\"items\":[{\"k\":\"forget_story\",\"d\":{\"alert_id\":900001}}]}" | put fstory
ingest "$TMP/fstory.json"; expect_code "forget_story for one alert" 200
live "persona=$P&rev=-1"
expect_js "…its annotation is gone, the other story stays" '!r.annotations.some(a => a.alert_id === 900001) && r.annotations.some(a => a.alert_id === 900003)'
expect_js "…its questions, replies and system messages are gone, only the alert's own alert-* row stays" 'const t = r.thread.filter(x => x.alert_id === 900001).map(x => x.mid).sort(); JSON.stringify(t) === JSON.stringify(["alert-900001"])'
expect_js "…the other alert keeps its conversation" 'r.thread.filter(x => x.alert_id === 900003).length === 2'
expect_js "…feed rows of kind annotation for that alert are gone, other feed rows stay" '!r.feed.some(f => f.id === 900102) && r.feed.some(f => f.id === 900101) && r.feed.some(f => f.id === 900103)'
expect_js "…the alert itself and its receipt stay" 'const a = r.alerts.find(a => a.id === 900001); !!a && a.evidence.receipt.rule.length > 0'
ingest "$TMP/fstory_order.json"
live "persona=$P&rev=-1"
expect_js "forget_story then new items in one request: the newer items survive" 'const n = r.annotations.find(a => a.alert_id === 900001); !!n && n.event_id === "evt-new" && r.thread.some(t => t.mid === "m-new-in")'
echo "{\"items\":[{\"k\":\"forget\",\"d\":{\"persona\":\"$P\"}}]}" | put forget
ingest "$TMP/forget.json"; expect_code "forget the person's words" 200
live "persona=$P&rev=-1"
expect_js "…all annotations are gone" 'r.annotations.length === 0'
expect_js "…every reply and system message is gone, the alert-* rows stay" 'r.thread.length > 0 && r.thread.every(t => t.mid.startsWith("alert-")) && r.thread.some(t => t.mid === "alert-900003")'
expect_js "…the health notes are gone" 'r.profile === null'
expect_js "…feed rows of kind annotation/story are gone, the series and alerts stay" '!r.feed.some(f => f.kind === "annotation" || f.kind === "story") && r.feed.some(f => f.kind === "review") && r.series.length > 0 && r.alerts.length === 3'

# ---------------------------------------------------------------- 9. reset
section "reset"
node -e '
const P="smoke-test";
console.log(JSON.stringify({items:[
 {k:"profile",d:{persona:P,items:[{key:"k",label:"Kept",value:"v",origin:"chat",at:"2026-10-24T20:00:00+00:00"}],flags:[]}},
 {k:"series",d:{persona:P,i:7001,ts:"2026-10-05T00:00",state:"awake",quality:"good",v:{heart_rate:61}}},
 {k:"reset",d:{persona:P}},
 {k:"series",d:{persona:P,i:7005,ts:"2026-10-05T01:15",state:"awake",quality:"good",v:{heart_rate:62}}}
]}))' | put reset
ingest "$TMP/reset.json"; expect_code "reset in the middle of a batch" 200
live "persona=$P&rev=-1"
expect_js "…what came before the reset is gone, what follows stays" 'r.series.length === 1 && r.series[0].i === 7005 && r.alerts.length === 0 && r.annotations.length === 0 && r.thread.length === 0 && r.monitor === null'
expect_js "…epoch moved so open dashboards start over" 'r.epoch > '"$EPOCH0"
expect_js "…the health notes are not part of the replay and stay" 'r.profile && r.profile.items[0].key === "k"'
echo '{"items":[{"k":"reset","d":{"persona":"nobody-here"}}]}' | put resetbad
ingest "$TMP/resetbad.json"; expect_code "reset of an unknown person is ignored, not an error" 200

# ---------------------------------------------------------------- 10. clean up
section "clean up"
echo "{\"items\":[{\"k\":\"reset\",\"d\":{\"persona\":\"$P\"}},{\"k\":\"forget\",\"d\":{\"persona\":\"$P\"}}]}" | put cleanup
ingest "$TMP/cleanup.json"; expect_code "remove the test person's rows" 200
# put the persona list back as it was (or the three built-in people when there was none), then the test person is unknown again
node -e '
const before = JSON.parse(process.argv[1] || "[]"), linked = process.argv[2] === "null" ? null : process.argv[2];
const base = (id, name, tag, hue) => ({ id, name, tag, blurb: "", kind: "synthetic", source: "Seeded physiology simulator", license: "Invented", citation: "", channels: [], hue });
const items = before.length ? before : [base("maya", "Arya", "Runner", "#FF385C"), base("daniel", "Daniel", "Desk job", "#7C5CFA"), base("priya", "Priya", "Control", "#0EA5E9")];
console.log(JSON.stringify({ items: [{ k: "personas", d: { items, linked } }, { k: "setting", d: { key: "send_alerts", value: "0" } }] }));
' "$BEFORE_LIST" "$BEFORE_LINKED" | put restore
ingest "$TMP/restore.json"; expect_code "restore the persona list" 200
live "persona=$P&rev=-1"; expect_code "the test person is unknown again after the list is restored" 400
live "persona=maya&rev=-1"; expect_code "built-in people are still readable" 200

# ---------------------------------------------------------------- 11. migration 0002 on a version 1 database
section "migration 0002 on a v1 database"
# Builds a database from the schema as committed (version 1, nine tables) in a scratch dir, puts a row in each changed table,
# applies migrations/0002_story.sql, and compares every table's columns with a database made from the final schema.sql.
ROOT="$(cd "$HERE/.." && pwd)"
SCR="$(mktemp -d /tmp/s2c-mig.XXXXXX)"
# the newest committed schema.sql that does not have the version 2 columns yet (HEAD today; an older commit once version 2 is committed)
for REV in $(cd "$ROOT/.." && git rev-list HEAD -- cloudflare/schema.sql 2>/dev/null | head -20); do
  (cd "$ROOT/.." && git show "$REV:cloudflare/schema.sql") > "$SCR/v1.sql" 2>/dev/null
  grep -q "event_id" "$SCR/v1.sql" || break
  : > "$SCR/v1.sql"
done
if [ -s "$SCR/v1.sql" ]; then
  d1x() { (cd "$ROOT" && npx wrangler d1 execute signal2care --local --persist-to "$1" "${@:2}") 2>"$TMP/d1err"; }
  cols() { d1x "$1" --json --command "SELECT m.name AS t, group_concat(p.name || ':' || p.type, ',') AS c FROM sqlite_master m, pragma_table_info(m.name) p WHERE m.type='table' AND m.name NOT LIKE '\\_cf%' ESCAPE '\\' AND m.name NOT LIKE 'sqlite%' GROUP BY m.name ORDER BY m.name" > "$2"; }
  if d1x "$SCR/old" --file="$SCR/v1.sql" >/dev/null && d1x "$SCR/old" --command "INSERT INTO alerts(id,persona,metric,status) VALUES(1,'maya','skin_temp','sent'); INSERT INTO annotations(alert_id,persona,summary) VALUES(1,'maya','old summary'); INSERT INTO thread(alert_id,mid,direction,text) VALUES(1,'alert-1','out','old alert text')" >/dev/null; then
    cols "$SCR/old" "$SCR/cols-v1.json"
    node -e 'const j=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8").slice(require("fs").readFileSync(process.argv[1],"utf8").indexOf("[")));console.log(j[0].results.length)' "$SCR/cols-v1.json" > "$SCR/n1"
    [ "$(cat "$SCR/n1")" = "9" ] && pass "the committed schema.sql (HEAD) builds a v1 database with 9 tables" || fail "v1 database should have 9 tables" "got $(cat "$SCR/n1")"
    if d1x "$SCR/old" --file="$ROOT/migrations/0002_story.sql" >/dev/null; then
      pass "migration 0002 applies to the v1 database"
      cols "$SCR/old" "$SCR/cols-migrated.json"
      d1x "$SCR/new" --file="$ROOT/schema.sql" >/dev/null && cols "$SCR/new" "$SCR/cols-fresh.json"
      node -e '
const fs=require("fs"); const rd=(f)=>{const t=fs.readFileSync(f,"utf8");return JSON.parse(t.slice(t.indexOf("[")))[0].results.map(r=>r.t+"("+r.c+")").join("\n")};
const a=rd(process.argv[1]), b=rd(process.argv[2]); if(a!==b){console.log("migrated:\n"+a+"\n\nfresh:\n"+b);process.exit(1)}' "$SCR/cols-migrated.json" "$SCR/cols-fresh.json" > "$TMP/coldiff" \
        && pass "migrated v1 database and a fresh schema.sql have the same tables, columns, order and types" || { fail "migrated and fresh schemas differ"; cat "$TMP/coldiff"; }
      d1x "$SCR/old" --json --command "SELECT (SELECT summary FROM annotations WHERE alert_id=1) AS s, (SELECT text FROM thread WHERE mid='alert-1') AS t, (SELECT event_id FROM alerts WHERE id=1) AS e, (SELECT delivery FROM thread WHERE mid='alert-1') AS d" > "$SCR/rows.json"
      node -e 'const t=require("fs").readFileSync(process.argv[1],"utf8");const r=JSON.parse(t.slice(t.indexOf("[")))[0].results[0];process.exit(r.s==="old summary"&&r.t==="old alert text"&&r.e===null&&r.d===null?0:1)' "$SCR/rows.json" \
        && pass "…old rows survive the migration; new columns are NULL" || fail "old rows after migration"
      if d1x "$SCR/old" --file="$ROOT/migrations/0002_story.sql" >/dev/null 2>&1; then
        fail "running the plain migration twice should fail (use scripts/migrate.mjs to repeat safely)"
      else
        pass "the plain migration refuses to run twice (duplicate column), as documented"
      fi
      (cd "$ROOT" && node scripts/migrate.mjs --local --persist-to "$SCR/old" >"$TMP/migout" 2>&1)
      grep -q "skip   thread.delivery" "$TMP/migout" && pass "scripts/migrate.mjs is safe to repeat (skips the columns that exist)" || fail "scripts/migrate.mjs repeat" "$(tail -2 "$TMP/migout")"
    else
      fail "migration 0002 applies to the v1 database" "$(tail -3 "$TMP/d1err")"
    fi
  else
    fail "could not build the v1 scratch database" "$(tail -3 "$TMP/d1err")"
  fi
else
  skip "migration test (no committed version 1 schema.sql found in git history)"
fi
rm -rf "$SCR"

echo
echo "$PASSES passed, $FAILS failed"
[ "$FAILS" -eq 0 ]
