# Signal2Care

An iMessage assistant with layered long-term memory and an always-on wearable monitor, for the Hackachusetts 2026 healthcare track. Everything it watches is synthetic. It is not a medical device and it does not give medical advice.

## What it does
- **Chats over iMessage** through a Linq sandbox line: text, photos and PDFs. General questions are fine. Health lines are firm (see `docs/rulebook.md`).
- **Remembers**: a lossless message log, event memory, profile facts that get superseded, a rolling summary and a recent window. Recall fuses keyword and embedding search.
- **Watches a wearable stream**: a seeded simulator replays 28 days of WHOOP-style data for three people (a runner, a desk worker, a steady control). The monitor compares every signal with that person's own usual range for the same context (asleep, or awake at that time of day).
- **Decides what is worth a text**: Jev judges a typed, computed state. Plain code applies cooldowns and a safety floor. The language model only words one question around numbers that code supplies, and the result is checked.
- **Links replies to the chart**: when you answer an alert, your words are summarized, attached to that moment on the series, and checked with arithmetic against your own history ("bigger than your workouts usually produce"). Nothing here says what caused a change.
- **Shows it all on a dashboard** (GitHub Pages) fed by a Cloudflare Worker and D1: live signal cards, detail charts with the usual range, an alert timeline, the iMessage thread and a model activity feed.

## Run it
1. `cp .env.example .env` and fill it in.
2. `scripts/serve.sh` starts the monitor and receiver on 127.0.0.1:8787.
3. `scripts/listen.sh` relays Linq inbound messages to it through the Linq CLI.
4. Text the Linq number from a phone listed in `ALLOWED_NUMBERS`.

Texts you can send:

| Text | What happens |
|---|---|
| `demo play` / `demo pause` / `demo reset` | Start, pause or clear the replay |
| `demo day 18` | Start at a given simulated day (history before it loads instantly) |
| `demo speed 6` | Ticks per second (one tick is 15 simulated minutes) |
| `demo alerts on` / `off` | Turn alert texts on or off. They are **off** by default: alerts are drafted on the dashboard only |
| `demo status` | Where the replay is |
| `demo link` | A link that opens the dashboard already signed in |
| `what do you remember`, `forget everything` | See or erase what is stored (the erase also removes your replies from the dashboard) |
| `STOP`, `START`, `HELP` | Opt out, opt in, commands |

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+ and the Linq CLI. Photo conversion uses macOS `sips`; PDF text uses poppler (`pdftotext`, `pdftoppm`).

## Dashboard and cloud
- `cloudflare/` is the Worker (plain JavaScript) and the D1 schema. `cd cloudflare && npm install && npm run db:local && npm run dev` runs it locally on port 8788 with tokens from `cloudflare/.dev.vars` (`INGEST_TOKEN`, `DASH_TOKEN`).
- `dashboard/` is static HTML, CSS and JS with no build step and no third-party requests. Serve it with any static server. Without a key it shows a recorded run (`sample.json`, made by `uv run python scripts/make_sample.py`).
- There is no sign-up or login page. The brain pushes to the Worker with `INGEST_TOKEN`. You open the dashboard with a link that carries `DASH_TOKEN` in its fragment (text `demo link`), the page keeps it in browser storage and removes it from the address bar.
- Phone numbers never reach the Worker. Dashboard buttons write to a command queue that the brain polls.

Deploy steps are in `docs/deploy.md`.

## Docs
- `docs/rulebook.md`: what is enforced by code, what is in the prompt, what each model may and may not do.
- `docs/gaps.md`: measured results and what is still missing, for the agent, the memory and the system design.
- `.kiro/steering/architecture.md`: the target architecture and build order.

## Limits to know
- The Linq sandbox is a 7-day trial, inbound-first (a person must text the line before it can reply), capped at 30 messages per minute.
- DeepInfra, OpenRouter and OCR.space are metered. Set spend caps.
- The monitor runs on your machine. If it sleeps, the dashboard shows "Monitor offline".
- Data is stored locally in `data/` (git-ignored). Secrets live in `.env` (git-ignored).
- The three extra channels (stress, blood pressure, sweat) are synthetic additions. WHOOP's API does not provide them.
