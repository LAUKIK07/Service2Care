---
inclusion: manual
---

# Signal2Care: architecture and plan (normalized)

Status: plan, 2026-10-24. Replaces section 6 of `signal2care-analysis.md`. Tags are the same as in that file: [VERIFIED], [USER], [INFERRED], [UNVERIFIED].

## 1. The product as one flow
Each requirement appears once, in the order the data moves.

1. Ingest. A script replays time-series metrics the way WHOOP's API delivers them. [USER]
2. Watch. An always-on monitor tracks every metric, learns each person's baseline, and looks for patterns and irregularities. [USER]
3. Decide. Deterministic math sizes each change. Jev rates how big it is and whether to alert. A policy gate has the last word. [USER]
4. Ask. An iMessage asks what is going on, with a question that fits the metric. [USER]
5. Talk. The person replies and chats. The agent has memory, reads images and documents, researches, and checks claimed causes with math. [USER]
6. Record. The chat is summarized and attached to the time series together with the computed impact. [USER]
7. Show. A dashboard on GitHub Pages plays or stops the replay and shows series, alerts and annotations beside the live iMessage thread. [USER]
8. Later. Emergency-contact escalation after 30 to 40 minutes without a reply. [USER]

## 2. Platform split
- GitHub Pages: static dashboard only. It holds no secrets, because the repo is public. [USER]
- Cloudflare free plan: Worker API, D1 database, one Durable Object for the replay clock, cron heartbeat. [USER]
- DeepInfra: text model `DeepSeek-V4-Flash-0731`, vision model `DeepSeek-V4.1-Flash-1017`, embeddings `BAAI/bge-m3`. DeepInfra is pay per token, not free.
- Linq: iMessage in and out. Sandbox, 7 days, ends about Oct 31.
- Jev: decision layer through OpenRouter. The key works. Client is `signal2care/jev.py`, not wired into the chat path yet.
- OCR.space (free plan): scanned PDFs and text-heavy photos.

## 3. Verified facts that shape the design
- DeepInfra [VERIFIED by calls]: `DeepSeek-V4-Flash-0731` is text only ("does not accept image input"). `DeepSeek-V4.1-Flash-1017` and `DeepSeek-V4.1-Flash` accept images. `DeepSeek-V4-Flash-Vision-Exp` accepts images but spends many tokens reasoning. DeepInfra's model list has no OCR model and no Jev.
- OCR.space free plan [VERIFIED, [docs](https://ocr.space/OCRAPI)]: 1 MB per file, 3 pages per PDF, 25,000 requests per month, 500 per day per IP. Engine 2 is the default; Engine 3 handles handwriting and tables (1,000 per month). Live test: a receipt image, a tilted JPEG and a scanned PDF were read correctly in 1 to 2 seconds. One result had a Cyrillic look-alike letter inside a Latin word; `fix_mixed_script` repairs that.
- Jev through OpenRouter [VERIFIED by calls]: about 0.1 to 0.2 s and $0.000026 per call with `typesafe/jev-1.14`. On five hand-written alert cases it alerted on a large persistent change with good data (0.74), ignored a tiny wobble (0.96), and called a huge reading from a sensor dropout a data issue (0.98). It still said alert (0.58) for a change the person had already explained 2 hours earlier, so repeat suppression must be a deterministic cooldown. On a borderline case, reversing the option order moved a probability by 0.12 and flipped the top pick. Treat borderline outputs (confidence under about 0.5) as watch, and average over orderings if it matters. This is a smoke test, not a benchmark.
- Cloudflare Workers Free [VERIFIED, [docs](https://developers.cloudflare.com/workers/platform/limits/)]: 100,000 requests per day, 10 ms CPU per request or cron run (waiting on network does not count), 50 subrequests per request, 5 cron triggers per account.
- D1 Free [VERIFIED, [limits](https://developers.cloudflare.com/d1/platform/limits/), [pricing](https://developers.cloudflare.com/d1/platform/pricing/)]: 5 million rows read and 100,000 rows written per day, 5 GB total, 50 queries per Worker invocation. An index adds a row written per write. Over the limit, queries fail until 00:00 UTC. D1 supports FTS5 and JSON functions ([docs](https://developers.cloudflare.com/d1/sql-api/sql-statements/)), so the memory design ports.
- Durable Objects Free [VERIFIED, [pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)]: SQLite-backed only, 100,000 requests per day including alarm invocations, 13,000 GB-s per day. Each `setAlarm` is one row written.
- WHOOP's API has no continuous heart rate and no sandbox. Data are records with a score state, updated later, announced by webhooks that can duplicate or drop. See section 5 of `signal2care-analysis.md`.
- `~/Downloads/whoop_atmos_data` is not wearable data, so a generator is needed.

## 4. Target architecture
```
GitHub Pages dashboard --HTTPS + access token--> Cloudflare Worker (API)
                                                   |- D1: messages, memory, series, alerts, annotations
                                                   |- Durable Object "replay": play, stop, tick by alarm
                                                   |- Cron: 1-minute heartbeat
Linq (iMessage) --signed webhook--> Worker --> Agent
Agent --> DeepInfra: text, vision, embeddings
Agent --> Document reader: PDF text layer, OCR provider for scans
Agent tools: read_memory, query_series, compute_impact, search_web (allowlist), list_alerts
Monitor (deterministic stats) --> candidate alert --> Jev --> policy gate --> Linq message
```
Today the agent runs locally in Python (FastAPI, SQLite, Linq CLI relay). The Worker is a TypeScript port of the same modules. The Python version stays as the reference for behaviour.

## 5. Alert pipeline, in order
1. A tick arrives from the replay (later, from a real source). Validate it and tag its source, unit and method.
2. Update the baseline. Use the person's own history by time of day. Require a warm-up of 7 to 14 days before alerting.
3. Detect. Robust z-score (median and MAD), drift detectors (CUSUM or EWMA), and cross-metric co-occurrence rules.
4. Size the impact. Effect size, percent change, duration, data quality. All of this is code. Neither the LLM nor Jev computes numbers.
5. Jev gets a typed state (metric, magnitude, persistence, quality, recent alerts, user context) and returns probabilities for alert now, watch, or ignore. Thresholds are calibrated on held-out labelled cases, not in-sample.
6. Policy gate. Cooldowns, daily cap, quiet hours, consent, STOP, and a severity floor that does not depend on any model.
7. Message. The LLM writes the question for that metric. Numbers are inserted by code from the evidence record.
8. Conversation. The agent uses memory, the series and the impact math. Research results are shown as context and sources, never as proof of cause.
9. Record. Summarize the chat and attach it to the series with the computed impact and the claim history.
10. Later, escalation: no reply in 30 to 40 minutes goes to a pre-approved contact.

## 6. Phases
- Phase A, live chat. DeepSeek text and vision router, document reader, layered memory, safety gate, iMessage through the Linq sandbox. Built and tested offline and against DeepInfra. Waiting for a first real text. Exit: a real conversation with a photo and a PDF, with recall across turns.
- Phase B, cloud foundation. Worker, D1, Linq webhook straight to the Worker with signature checks, memory ported to D1. Needs a Cloudflare account and API token. Exit: the same chat works with the laptop off.
- Phase C, data. A seeded physiology simulator with injected anomalies, a replay engine that emits WHOOP-shaped records, a series schema in D1. The cheap LLM writes persona backstories and event scripts, not numbers. Exit: a repeatable 28-day, 3-persona dataset.
- Phase D, monitor. Baselines, detectors, impact math. Exit: known injected anomalies are found, and quiet data raises no alerts.
- Phase E, decision and outreach. Jev layer, policy gate, metric-specific questions. Exit: alerts go out on iMessage during a replay, with a measured false-alert rate.
- Phase F, dashboard. GitHub Pages, access gate, play and stop, series charts, alert markers and annotations, thread beside it. Exit: a demo run from the dashboard.
- Phase G, analysis in chat. Web research tool with a source allowlist, impact checks of claimed causes, wellness-only suggestions, annotation summaries.
- Phase H, evaluation and hardening. Benchmark arms from the blueprint plus an audit arm, red-team cases, rate limits, kill switch. Emergency contact only after explicit double opt-in design.

## 7. What I need from you
1. OCR: done. OCR.space free key is set and tested.
2. Jev: done. The OpenRouter key works.
3. Cloudflare: account, plus an API token or a `wrangler login` run by you.
4. Dashboard access. Recommended: a passcode gate. A public page must not show chat summaries from real conversations.
5. Data. Recommended: deterministic simulator for numbers, LLM only for stories. Confirm.
6. Metrics. WHOOP-like set (sleep, resting HR, HRV, respiratory rate, SpO2, skin temperature, strain, steps) plus extras (blood pressure, sweat or EDA, stress) labelled as extra synthetic channels, since WHOOP's API does not provide them.
7. Emergency contact: defer. If built, it needs consent from the person and the contact.
8. A DeepInfra spend cap.
9. One model for text and images (simpler, consistent voice) or the two-model router (current).

## 8. Gaps I would close
Dashboard authentication, consent and data deletion, alert feedback ("was this useful"), snooze and mute, time zones and quiet hours, baseline warm-up, alert cooldowns and de-duplication, an audit log, a kill switch, a visible synthetic-data watermark, source and method on every metric, data-quality flags, an evaluation harness, and basic observability.
