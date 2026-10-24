---
inclusion: manual
---
# Session 3 plan: iMessage depth, story sessions, real data (started 2026-10-24)

User asked for (all end to end, evidence-only, elder-friendly): richer iMessage (reply threads, tapbacks, emoji, effects, polls,
cards as images), a 10-minute conversation window per alert whose Q&A is summarized and attached to that spike ("story"),
onboarding via `demo link` (consent + short health profile, one question at a time), web evidence search that never invents,
Jev + LLM working as a loop (not just a gate), real datasets normalized + better synthetic data, dashboard touch-up,
rubric items (consent, quiet hours, one question at a time, correction messages, forget/correct memory, image confirmation,
fallbacks, event ids and evidence receipts), Cloudflare + GitHub Pages in sync.

## Verified Linq facts (docs.linqapp.com, OpenAPI at cdn.linqapp.com/openapi/linq-api-v3.yaml)
- Send to chat: POST /v3/chats/{chatId}/messages {message:{parts[], reply_to{message_id,part_index}, effect{type,name}, idempotency_key}}
  (idempotency_key is INSIDE message). parts: text(+text_decorations bold/italic/underline/strikethrough + animations), media(url|attachment_id), link (must be alone).
- Reactions: POST /v3/messages/{id}/reactions {operation add|remove, type love|like|dislike|laugh|emphasize|question|custom(+custom_emoji)}.
- Polls: POST /v3/chats/{chatId}/polls {poll:{options:[{text}],idempotency_key}} (chat must exist, >=2 options, no title, send question as text first). Votes arrive as poll.vote.added {message_id, option_id}.
- Typing: POST/DELETE /v3/chats/{id}/typing. Mark read: POST /v3/chats/{id}/read. Attachments: POST /v3/attachments {filename,content_type,size_bytes} -> upload_url+required_headers+attachment_id, then PUT bytes. URL media <=10MB.
- Edit text part: up to 5 times within 15 min. Voice memo: POST /v3/chats/{id}/voicememo. Contact card endpoints exist. Rate limit 30 msgs/60s per pair (429 + Retry-After). 7000/day/line advice.
- Webhooks to subscribe: message.received, message.delivered, message.read, message.failed, reaction.added/removed, poll.vote.added/removed. Our listen.sh only had message.received.
- iMessage-only: effects, decorations, polls, typing, threading. SMS/RCS degrade.

## Datasets (raw zips in data/raw/, gitignored)
- wearable_stress_exercise.zip (PhysioNet wearable-device-dataset 1.0.1, 73,121,143 B, verified intact) - Calderon et al., ODC-By
- qol_stress.zip (PhysioNet qol-stress 1.0.0, 10.6 MB unzipped, CC BY 4.0) - published list confirmed
- mmash.zip (PhysioNet mmash 1.0.0, ODbL) ; lifesnaps_v4.zip (Zenodo 7229547, 615 MB, Zenodo metadata says CC-BY-4.0)
- User's research notes claimed Auricle/BIDMC/CGMacros/DREAMT; Auricle 16.5 GB and DREAMT 100+ GB exist on PhysioNet, not pulled.

## Build order
0 plan+Pillow | 1 linq.py features + event webhooks | 2 schema, onboarding, consent, pause, quiet hours, limits
3 stories (10-min window), interpretations/corrections, Jev<->LLM loop, evidence receipts, event ids
3.5 monitor/replay handle missing channels + real-data personas hook | 4 cards (Pillow) | 5 evidence tool + grounding guard, elder style,
memory forget/correct, image confirmation | 6 spawn sub-agents: A (data normalization, signal2care/sim + realdata.py + docs/data*.md),
B (cloudflare/** + dashboard/**) | 7 integrate, regenerate sample, verify, deploy Worker, push gh-pages, commit.

## File ownership while sub-agents run
Me: signal2care/*.py except sim/ and realdata.py, scripts/make_sample.py. A: signal2care/sim/**, signal2care/realdata.py, scripts/data_*.py, docs/data*.md.
B: cloudflare/**, dashboard/** (not sample.json).

## Status log (append below)
- [done] linq.py (reply threads, tapbacks, polls, effects, bold, upload+image, read, idempotency, typed webhook events), messenger.py (outbox, throttle, receipts),
  voice.py (plain wording, reading level, approved questions), cards.py (Pillow cards), onboarding.py, memctl.py (consent/pause/forget/correct with verify),
  stories.py (10-min window, claims, checks, versions, Jev next step, closing card), alerts.py rewrite (event ids, receipts, gates, held alerts, Jev question pick),
  research.py (PubMed with study type + relevance), guard.py (evidence grounding), stack.py, scripts/sim_chat.py => 36/36 checks pass with real models + fake Linq.
- [todo] my-day + doctor-summary cards; fakelinq module; make_sample update; scripts/eval_monitor.py; spawn sub-agents A (data) + B (worker+dashboard);
  live Linq smoke test; restart brain+listener (new --events list in scripts/listen.sh); deploy Worker (wrangler, account approved by user) + gh-pages; docs; commit.
- Ingest contract for B: kinds annotation (+event_id,status,opened_at,closes_at,qa,versions), profile{persona,items,flags}, personas{items,linked}, forget{persona},
  forget_story{alert_id}; alert statuses add held_consent|held_paused|held_quiet|held_open_story|held_rate|digested|cancelled|expired|sending; evidence JSON has receipt+event_id.
