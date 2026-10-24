# Signal2Care

An iMessage assistant with layered long-term memory, for the Hackachusetts 2026 healthcare track. It is a demo that uses synthetic data. It is not a medical device and it does not give medical advice.

## What it does today
- Chats over iMessage through a Linq sandbox line: text, photos and PDFs.
- Remembers across the whole thread: a lossless message log, event memory, profile facts that are superseded when things change, a rolling summary, and a recent-message window. Recall fuses keyword search and embedding search.
- Reads images with a Mistral vision model and Mistral OCR.
- Runs a deterministic safety gate (STOP, HELP, emergency and crisis guidance) that never calls a model.
- Commands: "what do you remember", "forget everything" (needs CONFIRM FORGET), STOP, START, HELP.

## Run
1. `cp .env.example .env` and fill it in.
2. `scripts/serve.sh` starts the receiver on 127.0.0.1:8787.
3. `scripts/listen.sh` relays Linq inbound messages to it through the Linq CLI.
4. Text the Linq number from a phone listed in `ALLOWED_NUMBERS`.

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+ and the Linq CLI (`npm install -g @linqapp/cli`). Photo conversion uses macOS `sips`.

## Limits to know
- The Linq sandbox is a 7-day trial, inbound-first (a person must text the line before it can reply), capped at 30 messages per minute.
- Mistral free mode may use inputs for training. Use test content only.
- Everything is stored locally in `data/` (git-ignored). Secrets live in `.env` (git-ignored).
