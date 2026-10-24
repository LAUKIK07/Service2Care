# Gaps and system-design review

Written after the first complete run of the flow: replay → monitor → Jev → alert text → reply → annotation → dashboard.
"Measured" means I ran it and counted. "Not verified" means I did not.

## What was measured

| Check | Result |
|---|---|
| False alert candidates from the monitor on clean data | 0.17 per 28-day run, over 60 runs (20 new seeds × 3 baselines), after raising the onset threshold. It was 0.42 before. |
| Planned events found (10 seeds each) | Illness-like pattern 10/10 (median 3.2 h after onset). Low-oxygen night 10/10 (1.8 h). Tracker dropout 10/10 (1.8 h). Stress afternoon 10/10 (1.8 h). Short sleep 6/10. |
| Texts actually sent in the full flow (one seed) | Maya 4, Daniel 3 (drafted), Priya 0. |
| Tool calling on both DeepSeek models | Works. |
| Worker auth | Ingest token is rejected on dashboard routes and the other way round. Unknown origins get no CORS header. |

These numbers come from the simulator the thresholds were tuned on. The 60 false-alert runs use new seeds, but the same generator, so real wearable data will behave differently. The sleep detector is the weakest: 4 of 10 short-sleep runs were missed, because the nightly spread for an irregular sleeper is wide.

## Agent

| Gap | Impact | Next step |
|---|---|---|
| The health guard is pattern-based. It misses paraphrases ("maybe bump your dose up a bit") and non-English text. The emergency/self-harm gate is English-only too. | A safe-sounding unsafe reply can slip through. | A second-opinion classifier on drafted replies, and gate wording in more languages. |
| Photos are described by a vision model. Only the diagnosis and medicine patterns are caught afterwards. | "That rash looks like X" depends on the wording. | Add image-specific rules to the guard, or refuse to interpret medical photos. |
| Tool loop is capped at 3 rounds, and PubMed text is passed to the model as untrusted data by prompt only. | A hostile abstract could try to steer the reply. | Strip instructions from literature text, or summarize it with a separate call. |
| No daily spend cap for model calls. | A runaway loop costs money. | Per-day budget counter that pauses the monitor's model calls. |
| Hourly Jev reviews are sampled by wall clock (one per 3 s) so fast replays do not pay for every simulated hour. | Reviews in the activity log are not one per hour at high speed. | Fine for a demo. Say so on the page if it matters. |
| Reactions, voice memos and group chats are ignored. | Some replies are lost. | Handle tapbacks as short replies. |
| Uploading your own data over iMessage (a CSV of readings) is not built. | You cannot bring your own series yet. | Parse an attached CSV into the `series` table behind a new persona. |
| Emergency-contact escalation after 30–40 minutes is not built. | No escalation path. | Needs a consent flow first: who, when, and what is shared. |
| Only one person (the first allow-listed number) is linked to a simulated wearable. | Single-user demo. | Map numbers to personas in a table. |

## Memory

| Gap | Impact | Next step |
|---|---|---|
| Notes are written by a model. It can invent or blur facts, and the only repair is "forget everything". | Wrong notes persist. | "Forget that I …" for single items, and show the source message for each note. |
| No retention limit. Messages, notes and images stay until the person erases them. | Health-adjacent text kept indefinitely. | A default expiry (for example 90 days) with a reminder. |
| Embeddings are compared by brute force in SQLite. | Fine for one person, slow at scale. | An approximate index if there are thousands of notes. |
| No memory benchmark was run. | I cannot give a recall number. | Build a small question set from real conversations and score it. |
| Attachments are stored on disk with file mode 600, not encrypted. | Anyone with the laptop and the account can read them. | Disk encryption (FileVault) or per-file encryption. |
| Fixed this session: "forget everything" left behind annotations, reply summaries and the dashboard copy. It now removes them locally and on the dashboard service. | — | Verified against the local service. |

## System design

| Gap | Impact | Next step |
|---|---|---|
| The brain runs on one Mac. If the laptop sleeps, texts stop and the dashboard shows "Monitor offline". | Demo risk. | Run it under `caffeinate -i`, or move the Python service to a small always-on host. |
| The Linq sandbox expires about Oct 31 and uses a shared line (20 contacts, inbound first). | The texting part stops after that. | Move to a paid line before any real use. |
| API keys and the SSH passphrase were pasted into chat. | They should be treated as exposed. | Rotate DeepInfra, OCR.space, OpenRouter and Linq keys, and the SSH key passphrase. |
| The dashboard key lives in the link and in browser storage. Anyone with the link can see the dashboard. | Reasonable for a demo of synthetic data. The reply text is real. | Short-lived links, or sign the link with an expiry. |
| The Worker free tier allows 100k requests a day. Idle polling by the brain is about 17k, health pushes about 9k, and an open dashboard tab about 43k if left running all day. | A forgotten tab plus the brain gets close to the cap. | Dashboard already slows to 15 s when the tab is hidden. |
| D1 free tier allows 100k row writes a day. A full 28-day run writes roughly 4,000 rows. | About 25 full runs a day. | Fine. |
| The reply window uses real time (12 h) while the replay uses simulated time. | At turbo speed a reply arrives "days later" on the chart. | The annotation stores the simulated time of the alert, which is the right anchor. |
| Quiet hours are undefined because simulated time is not the person's local time. | A text can arrive at 3 a.m. for the person. | Needs a real timezone and a schedule if real data is ever used. |
| The Jev endpoint is an alpha API. Option order changed one probability by 0.12 in my test. | Behaviour can shift under me. | Rules and the safety floor sit outside Jev, and Jev only sees typed state. |
| Real wearable data has not been run through the monitor. The PhysioNet wearable dataset is downloaded for that purpose but not normalized yet. | Tuning is in-sample. | Normalize it, replay one subject, and count alerts. |
| No automated tests are committed. The checks above were ad-hoc scripts. | Regressions are easy. | Add a small test suite when you want one. |
| Health data passes through Linq, DeepInfra, OpenRouter (typed state only) and Cloudflare. | Fine for synthetic data. Not fine for real patient data. | Business agreements and a privacy review before any real use. |
