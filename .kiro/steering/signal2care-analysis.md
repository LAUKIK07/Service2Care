---
inclusion: manual
---

# Signal2Care: analysis, evidence ledger and track record

Status: DRAFT v0.1, 2026-10-24. Analysis only. No code written.
Authored on Sonnet 5.5. Under the routing policy in `orchestration.md` this is an Opus-tier artifact (it sets contracts and safety posture), so run an Opus 5.5 review pass before locking any decision below.

Claim tags
- [VERIFIED] checked today against a file, command output or a primary source (link given)
- [PDF] stated in the blueprint, not independently checked
- [USER] stated by the user, not independently checked
- [INFERRED] my reasoning from verified facts
- [UNVERIFIED] not confirmed; needs a spike or an answer

Web facts are paraphrased from the linked pages. Links are in section 5.

---

## 1. What exists today

### Repo [VERIFIED: git, ls]
- `/Users/aryamahadik/Documents/Service2Care`, remote `git@github.com:LAUKIK07/Service2Care.git`, branch `main`, level with `origin/main`.
- 4 commits: Initial commit, first commit, a merge, "second test commit".
- Files: `LICENSE` (MIT, copyright 2026 Laukik Rahul Khade) and `README.md` (two lines: "first commit", "second test commit"). No code, no data, no `.gitignore`, no `.env.example`.
- Repo visibility not checked. Treat it as public: no secrets, no real phone numbers, no real health data in any commit.

### Machine [VERIFIED]
- Python 3.12.10 (blueprint asks for 3.11+), Node v22.23.1, uv 0.11.7, macOS arm64.
- `kiro-cli` is not installed.

### Blueprint PDFs [VERIFIED: pdftotext, mdls, sha256]
| File | Version | Pages | Note |
|---|---|---|---|
| `~/Documents/Signal2Care.pdf` | 1.1, multi-metric expansion | 32 | Canonical. Matches the copy attached in chat by version line and page count. |
| `~/Downloads/Signal2Care.pdf` | 1.0 | 28 | Older draft, no multi-metric content. |
| `~/Downloads/Signal2Care_PDF.zip` | contains the 1.0 file | | Stale. |

Do not hand the Downloads copies to another AI or developer.

### "The synthetic data" [VERIFIED, partial read]
- Closest match on disk: `~/Downloads/whoop_atmos_data` (and its `.zip`): 85 CSVs plus `_generator.py`.
- It is a synthetic product-intelligence workspace where WHOOP is the company: members, roles, products, integrations, scraped customer feedback ("signals", inferred from column names), clusters, initiatives, hypotheses, billing, API keys, webhooks, audit logs. Business metrics such as "Daily Active Members" and "Recovery score logged" come from sources like stripe and amplitude.
- It has no per-person sleep, heart rate, HRV, respiratory rate, SpO2 or skin temperature records. Sleep and HRV words appear only inside text fields of feedback and derived facts.
- What I checked: file names, headers of five files, two rows of the metric files, keyword counts across all files. I did not read every file.
- Conclusion: no wearable time series exists yet. It has to be generated (blueprint Appendix A, p.31). The folder could at most seed scenario ideas. Its scraped-text provenance and licence are unknown, so do not ship it.

### Other [VERIFIED by directory listing only]
- `~/Documents/final_ai_brain` is a separate, large project (names suggest Atmos/Vernais) with env and credential-style files. Contents not opened. Not part of this repo. Do not copy anything from it here.

---

## 2. The brief, restated

1. Source data [USER]: WHOOP-style continuous time series. There is no WHOOP API access, so replay synthetic data over time to behave like the real API.
2. Decision stack [USER]: an LLM plus "Jeff" (read as Jev, a System One decision model) as a fallback audit, for exact and precise decisions, aiming to beat WHOOP's own AI on accuracy.
3. Trigger [USER]: irregularities in the series push a message on iMessage instead of a dashboard or app.
4. Conversation [USER]: the message asks about sleep and the day. The reply adds context to the series so the story is complete. Later the AI suggests what to do.
5. Stack for now [USER]: Mistral free API for the LLM, Linq Hobby for messaging. Users register a number first. "20 numbers."
6. Orchestration [USER]: Opus 5.5 where needed, Sonnet 5.5 where needed, Haiku 5.5 for small decisions, to limit spend without losing quality.
7. Working style [USER]: assume nothing, verify, be skeptical.
8. This turn [USER]: analyse and save the thinking. No building.

Readings I chose
- "now we have to log in": read as "logic" (the decision logic). [UNVERIFIED] If you meant a login or auth flow, say so. The closest thing in scope is phone-number registration.
- "Jeff": read as Jev. [VERIFIED] The blueprint spells it Jev, and TypeSafe AI's Jev is a public System One model (section 5).
- "20 numbers": read as Linq Hobby's 20-contact limit, so 20 recipients and not 20 phone lines. [VERIFIED on the pricing page; the exact definition of a contact is UNVERIFIED]

---

## 3. What the blueprint specifies [PDF v1.1]

Product (p.2): a conversation-first, multi-metric wearable evidence assistant. Non-diagnostic. Synthetic patients and data only. Pitch: wearables say what changed, Signal2Care checks whether the conclusion is justified, asks only what matters, and corrects itself when evidence changes.

Components (p.2, 11, 12, 15, 18)
- VERIFY: deterministic Python. Baselines, shifts, sufficiency, provenance, quality flags. No diagnoses.
- Safety gate: deterministic, runs before any model. Handles STOP and consent, and urgent-symptom language with fixed guidance.
- ASK SMART: picks one of 7 action IDs (ASK_ONE_CONTEXT_QUESTION, EXPLAIN_OBSERVATION, ABSTAIN_INSUFFICIENT_EVIDENCE, WAIT_FOR_MORE_DATA, REVISE_INTERPRETATION, OFFER_SHAREABLE_SUMMARY, STOP_MESSAGES) and an existing question ID from a registry of 6. A Jev-style scorer if it works, otherwise a rules policy.
- LLM communicator: rewrites approved evidence into 1 to 3 short sentences. A validator then checks numbers, banned claims, PII and length.
- RECHECK: immutable claims with supersession. States: SUPPORTED_BY_DATA, UNCERTAIN, REVISED, WITHDRAWN, EXPIRED. Materiality rule against correction spam.
- Messages: web simulator by default, real SMS optional.
- Optional: CDC PLACES county context (never an individual risk), one Synthea patient, PhysioNet as a separate stress and exercise dataset.

Non-negotiables (p.4, 19, 32): evidence before explanation; measure versus meaning (recorded sleep is not actual sleep); corrections are first-class; one question at a time; any serious symptom overrides wearable reassurance; no diagnosis and no medication advice; never claim live WHOOP or real iMessage without integration; patient replies are data, not instructions.

Build order (p.25): data and UI, VERIFY, RECHECK, decision, messaging, evaluation, then one optional item (WHOOP OAuth, real SMS or PhysioNet). Real SMS is explicitly optional and last.

Evaluation (p.22, 23): 60 to 100 locked synthetic cases. Four arms: A rules, B standard LLM, C LLM with tools, D Jev plus LLM. Metrics: allowed-action accuracy, question burden, unsupported-claim rate, revision precision and recall, false-correction rate, safety failures (zero observed), latency. Ablations. Red-team matrix of 16 cases (p.24).

GO/NO-GO (p.24): do not use actual health data or real users for health advice at the hackathon. If safety or citation tests fail, narrow the demo to visualisation plus uncertainty reporting.

---

## 4. Brief versus blueprint versus evidence

C1. Real iMessage now versus simulator first
- Brief: users register numbers and get iMessage pushes. [USER]
- Blueprint: simulator by default; real SMS is the last, optional step; no real users for health advice (p.24, 25). [PDF]
- Proposal: real channel, synthetic data, consenting testers, no advice. The simulator stays the main test channel. Both go through one message pipeline behind a channel adapter. Spike Linq early but keep it off the critical path.

C2. "Continuous time series from the WHOOP API"
- Evidence: WHOOP's own FAQ says continuous heart rate is not available through the API. Data arrive as cycle, sleep, recovery and workout records with a score_state and an updated_at. Webhooks only announce changes, so a follow-up GET is needed. [VERIFIED]
- Proposal: the replay emits those records and webhook behaviours, not a raw stream. Any high-rate stream (PhysioNet-like) stays a separate source_type. Otherwise we develop against a capability the real API will not give us.

C3. Jev as a "fallback security audit"
- Blueprint: a Jev-style scorer chooses among allowed actions, rules are the fallback, and the safety gate is independent of any model (p.11, 15, 19). [PDF]
- Evidence: Jev returns probabilities over options we define and no text. [VERIFIED] The vendor claim that it cannot hallucinate means outputs are valid options, not that they are correct. A paired evaluation found Jev ahead of an open-weight model on 9 of 11 decision points, no better than chance on zero-shot model routing, and found that in-sample thresholds missed more on held-out data than targeted. [VERIFIED, arXiv 2610.02267 abstract] A second study found decision models weight an agent's own records over observations ("claim laundering"). I did not confirm Jev is one of its four models. [VERIFIED, arXiv 2610.25947 abstract and intro only]
- Proposal: Jev's role is an experimental question, not a given. Candidates: selector (arm D) and auditor (arm E: rules or LLM proposes, Jev checks, disagreement falls back to the conservative action). Jev sees only typed evidence flags, never raw user text. Conflicts between a user report and the data are flagged by VERIFY before Jev runs. Safety gate, consent and output validation stay model-free. Calibrate thresholds on held-out cases and test option-order permutations.

C4. "Perfect decisions" and "more accurate than WHOOP's AI"
- Blueprint: no claim of clinical safety or superiority. Safety means zero observed failures, not proof. Synthetic cases test program behaviour (p.23, 32). [PDF]
- [INFERRED] We cannot run WHOOP's AI on our data, and we author both the anomalies and the labels, so a detection score is partly circular.
- Proposal: define "better" as measurable on a locked test set against rules and LLM-with-tools: allowed-action accuracy, unsupported-claim rate, question burden, false-correction rate, safety failures (zero observed), latency. Make no claim about WHOOP.

C5. Open-ended check-in
- Brief: "how is your day going, when did you sleep?" [USER]
- Blueprint: exactly one question chosen from an approved registry (p.15, 16). [PDF]
- Proposal: add a small set of approved open prompts to the locked question bank. Treat replies as untrusted data.

C6. "AI suggests what to do"
- Blueprint: treatment advice is out of scope, wellness wording is not a blanket exemption from FDA rules, and no action ID exists for suggestions (p.2, 15, 20). [PDF]
- Proposal: out of the MVP. If added later: a static, pre-approved tip library plus "talk to a professional" routing, as a new locked action ID. Needs your decision.

C7. "Perfect story" from replies
- Blueprint: no causal claims from correlation; RECHECK must not present a confirmed physiological cause (p.11, 16). [PDF]
- Proposal: store replies as user-reported context events (provenance class HUMAN_REPLY) beside measured data. Phrase it as "You told me X; the tracker recorded Y." Never "X caused Y".

C8. Real phone numbers and free-text replies
- Blueprint: privacy checklist wants pseudonymous IDs, short retention and no real identifiers in prompts or logs (p.20). [PDF]
- Proposal: phone numbers live only in the messaging layer and are hashed elsewhere. Never in a prompt. Short retention for raw replies, delete on request, explicit consent text.

C9. Mistral Free mode and real replies
- Evidence: Mistral's help center says that in Free mode input and output may be used to train its models. Opt-out is described for pay-as-you-go. Zero data retention is pay-as-you-go only. [VERIFIED] Whether a Free-mode organisation can opt out is unclear. [UNVERIFIED]
- Proposal: Free mode only for synthetic content. Real replies never reach it. For the demo, parse replies deterministically. Revisit with pay-as-you-go, opt-out and ZDR if LLM understanding of real replies is needed.

C10. Linq Hobby
- Evidence [VERIFIED]: free; up to 20 contacts; iMessage with RCS and SMS fallback; API access; message status webhooks listed; described as for hobbyist and personal projects. Zero data retention is listed only under Pro (from $260 per month). The quickstart says the bearer token comes from a Linq representative and a number is provisioned. The Hobby plan says "via our CLI".
- Resolved for the sandbox (section 5, Linq sandbox and CLI): the token and line come from `linq signup`, the sandbox supports inbound events, and a contact is a number allowed to text a shared line (max 20). [UNVERIFIED]: what happens after the 7-day session (the CLI shows Tier Free, shared line, 20 contacts, which matches Hobby); how long Linq keeps message content. The homepage says data is inaccessible to Linq, while zero retention is sold as a Pro feature. Those two do not obviously agree. Linq's acceptable-use page returned no readable text to my fetch, so its health-message policy is unchecked.
- Deliverability rules from Linq's docs [VERIFIED]: start inbound-first; keep roughly 1 inbound per 2 outbound; do not bombard non-responders; no links or media in the first message; honour opt-outs (Linq blocks sends to opted-out recipients).
- Proposal: registration is inbound-first (the user texts START to the line, which also proves control of the number). At most one proactive message per irregularity episode. Stop after two unanswered. Quiet hours in the user's time zone. Nothing beyond approved wording in message text.

C11. Name
- Repo is Service2Care, PDF is Signal2Care. Decide. It affects package names, consent text and the Linq contact card.

C12. Safety gate coverage gap
- Blueprint's urgent list: chest pain, severe breathing trouble, stroke-like symptoms (p.4, 19). It has no self-harm or crisis language. [PDF]
- Open prompts like "why couldn't you sleep?" can draw such replies. [INFERRED]
- Proposal: add fixed crisis guidance (988 in the US) with deterministic detection and tests. This is an addition to the blueprint, so it needs your OK.

C13. Registration abuse
- Unverified number registration lets anyone enrol someone else's phone. [INFERRED] Use inbound-first START, STOP and HELP handling, and a visible "synthetic demo" label.

C14. "Irregularity" is undefined
- The blueprint avoids "abnormal" and uses within-person baselines, sufficiency flags and co-occurrence (p.13, 14). [PDF]
- Proposed taxonomy, thresholds configurable and labelled non-medical: DATA_GAP, LOW_COVERAGE, SHIFT_FROM_BASELINE, CO_OCCURRENCE, METHOD_MISMATCH, SOURCE_CONFLICT, UPDATED_RECORD.
- MVP triggers: sleep (recorded duration, no-data time) and the RHR up, HRV down, activity up pattern. These match the demo script (p.25).
- Real WHOOP sleep records carry no-data time that can serve as a wear-coverage proxy [VERIFIED]. The synthetic "wear coverage" is a synthetic field and must be labelled as such (p.5). [PDF]

C15. "Jev-style" is ambiguous
- It could be hosted TypeSafe Jev (closed weights, via OpenRouter, Cloudflare or the SDK), an open-weight Jev-style model, or something you trained. Cost, privacy and determinism differ. [UNVERIFIED which]

C16. Two PDF versions
- See section 1. Use `~/Documents/Signal2Care.pdf` (1.1) and copy it into `docs/` so every agent reads the same file.

---

## 5. External dependency ledger

### WHOOP API [VERIFIED against developer docs, 2026-10-24]
- You need a WHOOP device and membership to use the developer platform. There is no sandbox. ([support FAQ](https://developer.whoop.com/docs/developing/support/))
- Continuous heart rate is not available through the API. (same FAQ)
- Cycle: integer id, start and end, timezone_offset, score_state, score (strain, kilojoule, average and max heart rate), optional step_count that is null when the device was not worn for the full cycle. ([cycle](https://developer.whoop.com/docs/developing/user-data/cycle/))
- Sleep: UUID id, cycle_id, nap flag, score_state, stage summary including total_no_data_time_milli, respiratory_rate, performance, consistency and efficiency percentages. ([sleep](https://developer.whoop.com/docs/developing/user-data/sleep/))
- Recovery: cycle_id, sleep_id, score_state, recovery_score, resting_heart_rate, hrv_rmssd_milli, and spo2_percentage and skin_temp_celsius for 4.0 members. ([recovery](https://developer.whoop.com/docs/developing/user-data/recovery/))
- score_state is SCORED, PENDING_SCORE or UNSCORABLE. A cycle may have no recovery (for example, device not worn).
- Webhooks ([webhooks](https://developer.whoop.com/docs/developing/webhooks/)): sleep, workout and recovery events (updated, deleted). Body has only user_id, id, type, trace_id, so you must GET the data. Duplicates are possible (dedupe on trace_id). Delivery retried 5 times over about an hour; missed events are possible, so run a reconciliation fetch. HMAC-SHA256 signature over timestamp plus raw body, base64. No webhooks for cycles or body measurements.
- Blueprint spot-check: no continuous HR, RMSSD HRV, optional SpO2 and skin temperature, and cycle step_count all matched the docs. Not checked: OAuth scope list, rate limits, workout schema, Twilio, CDC, FDA, Synthea, PhysioNet, the hackathon portal.

### Linq [VERIFIED unless marked]
- Pricing ([pricing](https://linqapp.com/s/pricing)): Hobby $0, up to 20 contacts, iMessage plus RCS and SMS fallback, "full API access", message status webhooks, SOC 2 Type II (vendor claim). Pro from $260 per month adds group messaging, payments, iMessage apps, live location and zero data retention.
- API ([overview](https://docs.linqapp.com/channel/imessage/), [quickstart](https://docs.linqapp.com/channel/imessage/getting-started/quickstart/)): base `https://api.linqapp.com/api/partner/v3`. Needs a bearer token from a Linq representative and a provisioned number. A webhook subscription API lists `message.sent`, `message.received`, `message.delivered`, `message.read`, `message.failed`.
- Webhooks ([guide](https://docs.linqapp.com/channel/imessage/guides/webhooks/)): Standard Webhooks signing (webhook-id, webhook-timestamp, webhook-signature), 5-minute replay window, retries with backoff up to 30 minutes, webhook-id as idempotency key, date-versioned payloads (pin a version).
- Best practices ([docs](https://docs.linqapp.com/channel/imessage/getting-started/best-practices/)): inbound-first, about 1 inbound per 2 outbound, do not bombard non-responders, no links or media in the first message, honour opt-out, 7,000 messages per day per line.
- Service selection ([FAQ](https://help.linqapp.com/api-v3-faqs)): default chain iMessage, then RCS, then SMS. `preferred_service` can force one.
- Data ([DPA](https://linqapp.com/policies/data-processing-addendum)): Linq is the processor and the customer is the controller; subprocessors are listed in a Trust Center.
- Sandbox and CLI ([help center](https://help.linqapp.com/linq-sandbox), [CLI README](https://github.com/linq-team/linq-cli)) [VERIFIED 2026-10-24]: the free sandbox is a 7-day trial that starts when the phone line is assigned. After expiry the token returns 401; the account and history are kept; webhook subscriptions are removed shortly before expiry. It is inbound-first: you cannot start a conversation, only reply to a number that has already texted your line (otherwise HTTP 403, code 2008), and once a number has texted it qualifies permanently. Account-wide caps: 30 messages per minute and 7,000 per day. Messages are real iMessages to real devices. Going live keeps the token and lifts the limits.
- CLI [VERIFIED]: `@linqapp/cli` 2.7.0 (Apache-2.0, published by Linq's npm account, no install scripts), installed globally under `~/.local`. `linq signup` does email OTP, creates the account, mints a token and assigns a shared line (max 20 contacts per shared line). Non-interactive flags: `--email`, `--code`, `--name`. The email-only call printed "sent" but did not exit within 120 s here, so use a timeout. `linq webhooks listen` is documented to stream inbound events to the terminal; not yet observed. Credentials live in `~/.linq/config.json`. `linq signup` and `linq login` write to the `default` profile, so a second signup would overwrite it.
- Conflicting sources, resolved by observation on 2026-10-24: signup used email OTP and the CLI health check reported 7 days remaining right after, matching the help center. The third-party claim of 3 hours and GitHub auth did not apply.
- Pre-existing account on this machine [VERIFIED, secrets not read]: `~/.linq/config.json` holds a `default` profile for a different Gmail address, a shared line, a session expiring 2026-10-29 UTC, and one active webhook pointing at another project's endpoint. Do not modify it. The Signal2Care account uses an isolated config (`HOME=~/.linq-signal2care-home`) so the default profile stays intact.
- [UNVERIFIED]: see C10.

### Mistral [VERIFIED, official docs and help center]
- Free mode exists: it lets you create API keys and use included monthly usage within limits shown in the admin Limits page. ([usage limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)) Exact per-model rate limits were not read. Third-party claims (no card needed, about 1B tokens per month) are [UNVERIFIED].
- Free mode: Mistral may use input and output to train models; pay-as-you-go can opt out. ([training](https://help.mistral.ai/en/articles/347617-do-you-use-my-user-data-to-train-your-artificial-intelligence-models), [opt-out](https://help.mistral.ai/en/articles/455207-can-i-opt-out-of-my-input-or-output-data-being-used-for-training))
- Zero data retention: pay-as-you-go only, stateless calls only. (from a search snippet of the [ZDR article](https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr); page not opened)

### Jev (TypeSafe AI)
- [VERIFIED] Hosted; takes a state plus typed questions and returns typed answers with probabilities, no text. Question types Choice, Noul (yes/no probability), Score, and Box for images in 1.14. Reachable through OpenRouter (model `typesafe/jev-1.14`, alias `~typesafe/jev-latest`, `jev-1.13` still available), TypeSafe's SDK, and Cloudflare Workers AI (the last per the arXiv paper's intro and a search result). Billed per input token, output free. ([OpenRouter docs](https://openrouter.ai/docs/guides/community/jev))
- [Secondary] Released 2026-09-15 as TypeSafe's first "System One" model, in early access at launch. (DataCamp and the community notes below)
- [Secondary, vendor-sourced] about $0.042 per million input tokens and 70 to 500 ms latency. ([DataCamp](https://www.datacamp.com/blog/system-one-models-jev), [community notes tagging vendor versus reported claims](https://github.com/vamsikrishna2421/jev-usecases)) A community tip: always include an explicit "other" option so the model can say nothing fits.
- [VERIFIED, abstracts] [Paired evaluation](https://arxiv.org/abs/2610.02267v1) and [claim laundering study](https://arxiv.org/html/2610.25947v1). See C3.
- [UNVERIFIED] TypeSafe's and OpenRouter's data retention and training terms. Required before any real user data reaches Jev.

### Kiro models and steering [VERIFIED from Kiro docs via the introspect agent and the [models page](https://kiro.dev/docs/models/available-models.md)]
- Sonnet 5.5: generally available, 1M context, 1.3x credits, adaptive thinking on by default.
- Opus 5.5: experimental, 1M context, 2.0x credits.
- Haiku 5.5: experimental, launched 2026-10-23, 1M context, 0.06x credits up to 100K tokens and 0.3x above, paid plans, US and EU geographies.
- Experimental models are "Kiro Excluded Models": the output indemnity does not apply. The Auto router does not route to experimental models.
- Switching: the user switches in the IDE model dropdown. The IDE has no `/model` command. The agent cannot switch. `invoke_sub_agent` has no model parameter. Custom agents have a `model` field, but exact IDs for the 5.5 models are not documented (a wrong ID fails the first prompt) and it is not documented whether sub-agents honour it. `kiro-cli chat --list-models --format json` can list IDs, but the CLI is not installed here.
- Steering: `inclusion: always` files load on every interaction and cost context each time. Manual files load only when referenced. No size guidance is documented for the IDE.
- Cost ratios [INFERRED from the multipliers, per request]: Haiku about 22x cheaper than Sonnet and about 33x cheaper than Opus up to 100K tokens; Opus about 1.5x Sonnet.

---

## 5b. Chat agent and memory (built 2026-10-24)

Research used [VERIFIED, abstracts only]
- Event-centric memory ([arXiv 2511.17208](https://arxiv.org/abs/2511.17208)): store history as short self-contained event statements with entities, time cues and source turns, keep information non-compressed, recall by similarity plus an LLM filter. It matches or beats strong baselines with much shorter contexts.
- Multi-layer memory ([arXiv 2603.29194](https://arxiv.org/abs/2603.29194)): working, episodic and semantic layers with retrieval gating. Low-confidence source: it lists odd benchmark names and a Computer Vision category, so I used it only for the layering idea.
- Seen as search snippets only, not read: LiCoMemory (ACL 2026 Findings), Temporal Semantic Memory (ACL 2026 Findings), Human-Inspired Memory Architecture (arXiv 2605.08538).

What is in `signal2care/` [VERIFIED by an offline test with a fake model, 29 checks]
- Layer 1, raw log: every message verbatim in SQLite, never rewritten. Duplicate webhook deliveries are ignored.
- Layer 2, events: after each reply a model call turns new messages into self-contained statements that cite their source messages. Image OCR text is included.
- Layer 3, facts: stable profile slots. A changed fact supersedes the old one and the old row is kept.
- Layer 4, rolling summary: orientation only, never the sole source.
- Layer 5, working window: up to 40,000 characters of recent messages sent verbatim each turn.
- Recall: BM25 (FTS5) always, plus embedding similarity once 20 or more events exist, fused by reciprocal rank fusion. OCR text is searchable.
- Control: "what do you remember" and "forget everything" (with CONFIRM FORGET) are deterministic.
- Safety and consent: allowlist, STOP/START/HELP, emergency and crisis gate with fixed text, all without a model. Model output has markdown stripped. The model has no tools.
- Chat model chain: `mistral-large-2512`, then `mistral-medium-latest`, then `mistral-small-latest`. A model that returns 403 or a 0 per-minute limit is skipped for 10 minutes. If none works, the person gets one honest notice per 15 minutes, messages stay pending, and a sweeper retries.

Mistral key test [VERIFIED by calls, 2026-10-24]
- The key authenticates. `/v1/models` and embeddings work (1024 dimensions).
- `mistral-large-2512` and `mistral-large-latest` return 403 tier_not_allowed. `mistral-small-latest`, `mistral-medium-latest` and `mistral-ocr-latest` return 429 with `x-ratelimit-limit-req-minute: 0`.
- So chat, vision and OCR are blocked for this key or organisation right now. The Limits row the user pasted (large-2512, 250,000 tokens per minute, 1 request per second, vision, 262,144 context) does not match what the API enforces for this key.

Claims I do not make
- "Perfect memory". Storage is lossless, but what reaches the model each turn is chosen by window, profile, summary and recall, so a detail can still be missed. Real-model recall is untested until a chat model works.

---

## 6. Recommended path (superseded by `architecture.md`)

Order logic: Linq, Mistral and Jev are the only unknowns that can change the architecture. The deterministic core has none. Spike the unknowns early, keep them off the critical path.

### Phase 0: lock decisions (Opus)
- Answer section 8. Copy the v1.1 PDF into `docs/`. Lock the action vocabulary, the question bank (including any open prompts) and the irregularity taxonomy.
- Exit: decisions written in the track record; Opus review of this file done.

### Phase 1: deterministic core on a WHOOP-shaped replay (Sonnet; Haiku for fixture rows)
- Seeded generator: at least 3 personas by at least 28 days (p.5, 31), as WHOOP-shaped cycle, sleep, recovery and workout records, plus the p.6 scenarios: non-wear, travel and time zone, device swap RMSSD to SDNN, missing SpO2, HR plus HRV plus activity co-occurrence, conflicting sources, safety-message override.
- Mock WHOOP server with a virtual clock and a webhook emitter: PENDING_SCORE to SCORED, UNSCORABLE, edited records with a new updated_at, duplicate webhooks, a dropped webhook, a reconciliation fetch. The ingestion adapter is the same one a real WHOOP account would use later.
- VERIFY, safety gate, RECHECK ledger, rules-based ASK SMART, template messages, web Messages simulator, tests.
- Exit: runs offline with no LLM and no Linq; the blueprint acceptance checklist (p.29) items for data visibility, no fabrication, quality checks, decision gate, RECHECK persistence, STOP and clinical boundaries pass.

### Phase 1b: time-boxed spikes, in parallel (Sonnet). Each ends with a short finding in the track record.
- Linq: can a Hobby account get a token and a number; send one message to one consenting tester; receive one `message.received` webhook; verify the signature; confirm STOP behaviour; note what Hobby includes.
- Mistral Free: one structured-output call on synthetic text; measure latency, limits and error shape. No real user text.
- Jev: one Choice question over a typed evidence state with the 7 action IDs plus "other"; run every option-order permutation; record calibration on a handful of hand-labelled cases.

### Phase 2: LLM wording and validators (Sonnet; Opus reviews the validator threat model)
- Mistral wording from approved evidence text only. Deterministic validator (numbers match evidence IDs, banned claims, PII, length). Template fallback when the LLM is down or validation fails.
- Reply handling: deterministic parsing first. LLM extraction only if the data policy allows. The user confirms facts that would change a claim.
- Exit: red-team cases 5 and 14 and the injection guard pass.

### Phase 3: Linq channel (Sonnet; Opus reviews the consent flow)
- Inbound-first registration (START), STOP, HELP and re-consent, quiet hours by time zone, one proactive message per episode, stop after two unanswered, signature verification, idempotency on webhook-id, same pipeline as the simulator.
- Synthetic data only, at most 20 consenting testers, every message labelled as a synthetic demo.
- Exit: end-to-end on a real iPhone with recorded proof before any "real iMessage" claim.

### Phase 4: benchmark (Opus designs; Sonnet builds; Haiku runs and formats)
- Arms A to D plus E (propose then Jev audits). Thresholds calibrated on held-out cases. Option-order permutations. Report N, successes and failures with uncertainty.
- Exit: scoreboard. Keep only components that improve a relevant metric (p.23).

### Phase 5: optional, after 1 to 4
- WHOOP OAuth (needs a device and membership), CDC county layer, PhysioNet visualisation, shareable summary, wellness suggestions (needs an explicit decision).

---

## 7. Orchestration plan

### Development time
The agent cannot switch models, so the user flips the dropdown at tier boundaries and the agent states the tier before each step. Hard enforcement through custom agents with `model:` is blocked until the exact model IDs are known.

| Work item | Tier | Why |
|---|---|---|
| Lock action vocabulary, question bank, irregularity taxonomy, claim semantics, data contracts | Opus 5.5 | Cross-cutting and costly to reverse |
| Safety gate, crisis and urgent cases, consent flow, privacy posture | Opus 5.5 | Highest consequence |
| Benchmark and red-team design, labels locked before running | Opus 5.5 | Adversarial thinking; circularity risk |
| Review of safety-path code and the output validator | Opus 5.5 | Reviewer at or above author tier |
| Root cause after two failed attempts | Opus 5.5 | Working agreement |
| Adapters, mock server, VERIFY, RECHECK, API, UI, tests, Linq, Mistral and Jev clients | Sonnet 5.5 | Scoped, specified work |
| Spikes and their write-ups | Sonnet 5.5 | Needs judgement about what is signal |
| Fixture rows from a template, renames, formatting, doc lint, one-file summaries, checklists, log parsing, commit message drafts | Haiku 5.5 | Mechanical, reversible, checkable; context under 100K tokens |
| Safety gate execution, STOP, unit and method checks, numerics, output validation | No model | Deterministic code |

Rules: route by cost of being wrong and reversibility; Haiku to Sonnet after one failed check; Sonnet to Opus after two or on a design flaw; never Haiku on the safety path; verify experimental-model output with tests; use context-gatherer for exploration so the main context stays small.

### Runtime (the product)
| Function | Engine | Note |
|---|---|---|
| Safety gate, STOP, consent | Deterministic code | No LLM, no Jev |
| Evidence (VERIFY) and irregularity detection | Deterministic Python | Thresholds configurable, labelled non-medical |
| Action selection | Rules baseline first; Jev or LLM per benchmark | Arms A to E |
| Audit | Jev as shadow auditor (candidate) | Typed state only; fallback to the conservative action |
| Wording | Mistral plus validator plus templates | Free mode for synthetic content only |
| Reply parsing | Deterministic first | LLM extraction only if the data policy allows |

---

## 8. Open questions (default in brackets; reply "defaults" to accept all)

1. Which Jev: hosted TypeSafe via OpenRouter, an open-weight Jev-style model, or something you trained? Is there existing code or weights? [hosted Jev via OpenRouter for the spike; rules baseline first]
2. What does "fallback security audit" mean: a safety or consistency check on the chosen action, or something else? [shadow auditor, decided by the benchmark]
3. Name: Signal2Care or Service2Care? [Signal2Care]
4. Is "log in" a typo for "logic"? [yes]
5. Confirm that `whoop_atmos_data` is not what you meant, and generate the wearable data per Appendix A. [generate]
6. Who are the up to 20 Linq contacts, and will they opt in to synthetic-data messages? [team and judges, opt in via START]
7. When is the demo, and is real iMessage required for judging or a stretch goal? [simulator first, Linq as a demo-day add-on]
8. Wellness suggestions: out of the MVP? [yes]
9. A small set of approved open prompts in the question bank? [yes, two]
10. Add fixed crisis guidance (988) to the safety gate? [yes]
11. Replies in the demo: deterministic parsing, with Mistral Free kept to synthetic content? [yes]
12. First commit: `.gitignore` (with `.env`), `.env.example`, and the v1.1 PDF in `docs/`? [yes]
13. Run an Opus 5.5 review pass on this file before locking? [yes]
14. Legal review of consent for health-adjacent messages is not done (the blueprint also flags HIPAA and FTC review before production). Treat the pilot as a synthetic demo only. [accepted]

---

## 9. Track record (append-only; newest last)

### 2026-10-24, session 1: analysis only
Done
- Read the v1.1 blueprint (32 pages) as attached. Located and compared the two PDFs on disk.
- Verified repo state, git history, remote and toolchain.
- Searched the machine for the "synthetic data". Inspected `whoop_atmos_data` (names, headers, keyword counts). It holds no wearable time series.
- Checked WHOOP developer docs, Linq docs and pricing, Mistral help center and limits docs, Jev coverage and two arXiv abstracts, Kiro model docs.
- Asked the Kiro introspect sub-agent about model selection and steering.
- Wrote this file and `orchestration.md`.

Decisions locked: none.
Corrections to earlier assumptions: no synthetic wearable data exists yet; WHOOP has no continuous data through its API; "Jeff" is Jev; Mistral Free mode is not for real replies; the PDF exists in two versions; "20 numbers" means 20 contacts.
Open: section 8.
Next: answers to section 8, Opus review of this file, then Phase 0 and Phase 1.

Orchestration log
| Step | Tier used | Note |
|---|---|---|
| Whole session 1 | Sonnet 5.5 (session model) | The agent cannot switch. Sub-agent model unknown (not documented). No Opus or Haiku work was possible. |

### 2026-10-24, session 1 follow-up: iMessage interaction flow (default, not locked)
1. Register, inbound-first: the tester texts START to the Linq line. This proves control of the number, records consent and opens the thread. The bot replies with what this is (synthetic demo, not medical advice), message frequency, STOP and HELP. The tester is linked to a synthetic persona (demo_link).
2. Replay: the mock WHOOP feed delivers that persona's records on an accelerated clock.
3. Detect: VERIFY compares the latest night to the persona's own baseline. The model-free gate checks STOP, quiet hours and the message cap. The selector then picks one approved question.
4. Push: one short text, one question. Example: "Your tracker recorded less sleep than usual, and part of the night has no data. Did you wear it all night?" Cap: one proactive message per episode, stop after two unanswered.
5. Reply: the tester answers in plain language. Check order: webhook signature and dedupe, STOP and HELP, urgent language (fixed guidance, no model), then parsing. The reply is stored as user-reported context beside the data.
6. Correct: if the reply is material, RECHECK supersedes the earlier claim and says so. Example: "The device recorded 5.2 hours, but actual sleep is unknown because the recording was incomplete."
7. Close: optionally one more approved question, or a summary on request. No advice or suggestions in the MVP.
8. Always available: STOP, START, HELP. The web simulator mirrors the same thread.

Unverified: whether Linq Hobby can receive inbound messages and how it issues a token (first spike). Section 8 questions still apply.
Orchestration: Sonnet 5.5 (session model). The task was Haiku-eligible; not switched.

### 2026-10-24, session 1 follow-up: Linq install and signup (in progress)
- Installed `@linqapp/cli` 2.7.0 globally (npm prefix `~/.local`). Package metadata checked first: Apache-2.0, official Linq npm maintainer, no install scripts.
- Found an existing Linq setup on this machine (different email, session until 2026-10-29 UTC, one active webhook to another project). Left untouched.
- Created an isolated config dir `~/.linq-signal2care-home` and requested the signup code for the Signal2Care email ("sent"). The account is not created yet. It needs the 6-digit code and a display name from the user.
- Sandbox facts recorded in section 5. They resolve the C10 questions about token, line and inbound events. Inbound is still to be observed live.
- Risk noted: if the new shared line has the same number as the existing account's, and the tester's phone is a contact in both, inbound messages could route to the other project's webhook. Check the assigned number and contact list before adding any phone.
- Orchestration: Sonnet 5.5. No Haiku-eligible work in this step.

### 2026-10-24, session 1 follow-up: Linq account created [VERIFIED by CLI output]
- Signup completed non-interactively with the OTP. Account name used: "Signal2Care" (the user did not supply a name). Tier Free, Shared line, up to 20 contacts, 7 days remaining at creation (expires about 2026-10-31). Different line number from the pre-existing account, so no cross-routing risk.
- Token is stored only in `~/.linq-signal2care-home/.linq/config.json` (mode 600). It was redacted from all output and never shown. Run Linq commands for this account with `HOME=$HOME/.linq-signal2care-home linq ...`. Usage telemetry turned off for it.
- The original `~/.linq/config.json` is unchanged (same size and timestamp, default profile intact).
- `linq doctor`: token ok, number set, API connected, no webhook subscriptions yet.
- Blocked on: the tester's iPhone number (a shared line only accepts texts from contacts). Next: `linq contacts add`, `linq webhooks listen`, tester texts START, reply from the CLI to prove the round trip.
- Orchestration: Sonnet 5.5. No Haiku-eligible work in this step.

### 2026-10-24, session 1 follow-up: chat agent with layered memory built
- Scope change by the user: a free-flowing chat agent with memory, images and OCR on iMessage, ahead of the wearable evidence core. It departs from the blueprint's "one approved question from a registry" design. Guardrails kept: deterministic safety gate, no tools for the model, no invented wearable data, synthetic-demo disclosure, allowlist, delete on request.
- Built: Python service (FastAPI, httpx, SQLite), Linq client and webhook parser, Mistral client (chat chain, embeddings, OCR), memory layers, media handling (HEIC to JPEG via macOS sips), scripts `serve.sh` and `listen.sh`. Dependencies pinned in `pyproject.toml` and `uv.lock`.
- Secrets live only in `.env` (git-ignored, mode 600). The Mistral key and the tester's number came from chat messages, so the key is in this transcript. Rotate it after the demo.
- The Linq CLI listener printed its session signing secret and the localhost forwarding URL into the process output. The receiver listens on 127.0.0.1 only and rejects calls without the secret, but rotate `WEBHOOK_SECRET` before exposing anything.
- Blocked: Mistral chat, vision and OCR return 403 or a 0 per-minute limit for this key (see 5b). Until fixed, the service stores messages, runs the deterministic commands, and tells the person the model is unreachable.
- Open: first live inbound message not yet seen, so the real `--forward-to` payload shape is unverified. The Linq send body is accepted by the API: a send before the person has texted returned the expected 403 code 2008 (inbound-first), not a format error.
- Orchestration: Sonnet 5.5. Memory design and prompts are Opus-tier work and should get a review pass.

### 2026-10-24, session 1 follow-up: first commit, push and merge
- Committed the chat agent as `c9d4ba1` on `feat/imessage-memory-agent` after a secret scan (clean). The first push attempts failed because the SSH key needs a passphrase and no agent held it. The user pushed the branch from their terminal.
- On the user's request, fast-forwarded `main` from `3341f68` to `c9d4ba1` and pushed, using a throwaway ssh-agent so nothing persisted. The remote now has `main` and the feature branch at the same commit.
- The repo is public. The commit shows the user's git identity only. The user wants Kiro credited too, so later commits carry a `Co-authored-by: Kiro` trailer (see `orchestration.md`, section Git). `c9d4ba1` was not rewritten. Adding the trailer to it would need a force push to public `main`, which needs an explicit OK.
- The passphrase was shared in chat. It is weak, and the user was advised to change it. It is not stored anywhere in the repo.
- Orchestration: Sonnet 5.5. Haiku-eligible, not switched.

### 2026-10-24, session 1 follow-up: DeepInfra switch, attachment router, normalized plan
- The user moved the LLM to DeepInfra (DeepSeek V4 Flash 0731) and asked for a normalized plan (GitHub Pages dashboard, Cloudflare backend, always-on monitor, Jev decisions, WHOOP-style replay) before building it. The plan is in `architecture.md`. Only the chat path was built now.
- Correction to a user claim [VERIFIED by calls]: `DeepSeek-V4-Flash-0731` is text only on DeepInfra. Image turns use `DeepSeek-V4.1-Flash-1017`, which accepts images. DeepInfra has no OCR model and no Jev.
- Built: `llm.py` (OpenAI-compatible client with model fallback, network retries, lenient JSON parsing), `ocr.py` (document reader: PDF text layer first, optional OCR provider, one normalization step), model router in `agent.py` (images go to the vision model as they are, documents become normalized text, each image also gets a stored text note so it is searchable later). Embeddings moved to `BAAI/bge-m3` on DeepInfra. `mistral.py` removed.
- Tested against the live DeepInfra API with a fake iMessage line, nothing sent to a phone: natural text replies, fact and event extraction, recall of earlier details, image turn on the vision model ("solid red" on a 192 px test image), stored image note, PDF text-layer read and recall. Offline regression (12 checks) passed after a fix to a race that delayed image notes and one that let network errors escape. Test scripts were deleted.
- A 16 px test image was read as white. That is a too-small-image artifact, not a routing bug.
- Not built: Cloudflare, dashboard, simulator, monitor, Jev, web research, emergency contact.
- Still open: the OCR provider and key, a Jev key, Cloudflare access, and the first real inbound text (live payload shape still unseen).
- The DeepInfra key was pasted in chat, so it is in this transcript. Rotate it after the demo. `OCR_API_KEY` is empty.
- Orchestration: Sonnet 5.5. Architecture and the alert pipeline are Opus-tier decisions and need a review pass.

### 2026-10-24, session 1 follow-up: OCR.space router and Jev access
- Added OCR.space (free key) as the OCR provider. Router in `ocr.py`: plain text files are read directly, PDFs use the text layer first, scanned PDFs are rendered page by page (poppler) and read through OCR.space (Engine 2, then Engine 3 if almost nothing is found), photos go to the vision model and also to OCR when the person asks for the text. One normalization step (control characters, whitespace, length, Cyrillic look-alikes inside Latin words). Free-plan limits are handled: images are shrunk under 1 MB, at most 6 scanned pages per PDF.
- Bug found by the end-to-end test and fixed: a follow-up question about an earlier document made the model invent prices, because document text lived only in the database and the recent-message window showed just the "[attachment]" marker. Extracted text now rides along with its message in the prompt (newest first, 24,000 characters), recalled older messages show up to 1,500 characters of attachment text, and the prompt says to answer only from that text or admit the detail is missing. After the fix the same follow-up answered correctly ($14.50 sandwich, $4.50 per latte).
- Also fixed: OCR.space's "no text detected" placeholder was beating an empty result, and the file type was hard-coded as JPEG. The type is now read from the bytes. The first blank test image was a poppler font problem in my test file, not an OCR failure.
- Jev: OpenRouter key works. `jev.py` has typed builders for Noul, Choice and Score. Smoke-test results are in `architecture.md` section 3. Not wired into the chat path.
- The OCR.space key and the OpenRouter key were pasted in chat, so both are in this transcript. Rotate after the demo. The OpenRouter key was described as Jev-only.
- Test scripts and test files were deleted. Nothing is committed (working tree is on branch `docs/coauthor-and-log`).
- Orchestration: Sonnet 5.5.

### 2026-10-24, session 1 follow-up: first live iMessage round trip [VERIFIED on Linq and in the local database]
- At 2:24 PM the user texted "Hi". The Linq CLI relay forwarded it, the receiver parsed and stored it (sender, text, time correct), and two messages went out and were delivered: the first-contact disclosure and the "model isn't reachable" notice. That was the older server, when the Mistral key was blocked, so no model reply happened.
- This closes two open items: the live inbound payload parses correctly for text, and the Linq send path works end to end.
- Still unseen live: a DeepSeek reply, a photo, a PDF. The "Hi" has no reply recorded, so it will be part of the next pending batch. The sweeper skips messages older than 30 minutes by design.
### 2026-10-24, session 2: the full flow (simulator, monitor, Jev, alerts, replies on the chart, Worker, dashboard)
Built and run end to end on this machine. Nothing here was texted to a phone: the harnesses used a fake send, and `send_alerts` defaults to off.
- **Monitor tuning [MEASURED].** Before: 0.42 false candidates per clean 28-day run. After raising the onset rule from (4.0 z for 3 h) to (4.5 z for 3 h): 0.17, over 60 clean runs (20 new seeds for each of 3 baselines). Detection of the planned events did not change over 10 seeds: illness-like 10/10 (median 3.2 h after onset), low-oxygen night 10/10 (1.8 h), tracker dropout 10/10 (1.8 h), stress afternoon 10/10 (1.8 h). Short sleep 6/10, which is the weak spot. All of this is on the simulator the thresholds were tuned on.
- **Alert policy, found by running it.** First full run texted twice within an hour for one illness and printed "2.3% above" for a Celsius reading. Fixed: skin temperature and SpO2 are told as absolute differences, one text per three hours unless the change is extreme, no new text while an earlier one is open (12 h), and a "watch" decision re-arms the signal instead of swallowing it. The "safety floor" never fired before because it compared a quality string to "good"; fixed. Gap alerts use a fixed template because the model produced "Your tracker data shows 0, which is 100% below your usual of 1".
- **Result of the full flow (one seed):** Maya 4 texts (tracker gap, illness-like, a repeat 22 h later, low-oxygen night), Daniel 3 drafts (two short nights, stress afternoon), Priya 0.
- **Reply on the chart [VERIFIED with a real model, scripted reply].** The annotation attaches the person's words to the alert, lists only factors they mentioned, and runs arithmetic checks against their own history. Factors the tracker cannot measure are listed as "not measurable". Bugs found by the harness and fixed: an unrelated question was annotated as if it explained the alert and wrongly closed it (the annotator now has to say the content is relevant); the thread for an earlier alert included a later alert's text (now bounded at the next alert); the model asked permission instead of running a check (prompt now says to run it).
- **Guard for health replies.** Pattern-based, with refusals that mention the condition allowed. 7 unsafe and 12 safe test sentences classified correctly, which is a small sample. A live model declined "Should I take 400 mg of ibuprofen?" and "Do you think I have the flu?" and answered a hash-map question normally.
- **Privacy fix.** "Forget everything" left behind alert annotations, reply summaries and the dashboard copy. It now removes them locally and on the Worker (checked against the local Worker).
- **Cloudflare Worker and D1 [VERIFIED locally with `wrangler dev`, not deployed].** Auth separation holds (the ingest token cannot call dashboard routes and the reverse), unknown origins get no CORS header, the command queue round trip works, the dashboard's "play" command started the real replay through the queue. One bug: the first probe returned 404/301 because the dev server was not ready yet; not a code issue.
- **Dashboard [VERIFIED visually with Chrome headless, live data and the recorded preview].** Premium card layout, detail drawer with usual-range ribbon, sleep shading and alert pins, alert timeline with the thread and arithmetic checks, activity feed. Found and fixed while screenshotting: the "extra signal" tag truncated card titles, brief awakenings split one night into several (showed 4.0 h instead of 7.0 h), and the preview's first thread leaked a later alert.
- **Cloudflare login.** `wrangler` on this machine is already logged in, but to an account that was not obviously the user's. Nothing was created there. The user needs to confirm the account or log in themselves.
- **PhysioNet wearable dataset.** Download started in the background (73 MB, slow). Not normalized yet.
- **Exposure.** Reading the receiver's process output printed the webhook secret into this transcript. Rotate it, along with the other keys listed above.
- Orchestration: Sonnet 5.5 for the build. The alert policy and the guard are safety-sensitive and should get an Opus-tier review before real use.
### 2026-10-24, session 2 follow-up: Cloudflare deployed [VERIFIED against the live Worker]
- The user approved using the Cloudflare account that `wrangler` was already logged into. Before creating anything I listed the account's D1 databases (one unrelated one, `santiago`, left alone).
- Created D1 `signal2care` (id `e3216f03-3b64-4d0f-8db8-56242d05fd43`, ENAM), applied `schema.sql` (9 tables), set fresh `INGEST_TOKEN` and `DASH_TOKEN` as Worker secrets (not the local dev ones; saved to `.env` without printing), deployed `signal2care-api` to `https://signal2care-api.sentaigo.workers.dev`, then turned off the auto-enabled preview URLs because they would share the same secrets.
- Checks on the deployed Worker: health OK; no token, wrong-route token and the old local dev token are all 401; CORS allows `https://laukik07.github.io` and nothing else; ingest then live round trip works.
- Full production path: a "play" command queued through the deployed Worker was picked up by the local brain, the replay ran (Maya from day 19, 8 alert records, 2 drafted texts for the illness-like pattern and the low-oxygen night, nothing texted), the dashboard read it back from the Worker and rendered it, and a "reset" command cleaned it up. `.env` now points `CLOUD_URL` at the Worker and `DASHBOARD_URL` at the planned Pages address.
- Still not done: GitHub Pages (needs repo admin and a `gh-pages` branch), committing this session's later changes, PhysioNet normalization.
