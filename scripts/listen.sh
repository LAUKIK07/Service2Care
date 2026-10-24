#!/usr/bin/env bash
# Relay Linq webhook events to the local receiver using the Linq CLI.
# Uses the isolated Linq config so any other Linq login on this machine stays untouched.
# No --events filter: the CLI cannot filter on poll votes (it rejects poll.vote.added), and the receiver ignores events it does not use.
# The ones it uses: inbound messages, delivery and read receipts, failures, tapbacks and poll votes.
set -euo pipefail
cd "$(dirname "$0")/.."
REAL_HOME="/Users/$(id -un)"          # resolve before HOME is redirected (HOME may already be redirected)
set -a; . ./.env; set +a
LINQ_BIN="${LINQ_BIN:-$REAL_HOME/.local/bin/linq}"
export HOME="${LINQ_HOME:-$REAL_HOME/.linq-signal2care-home}"
exec "$LINQ_BIN" webhooks listen \
  --forward-to "http://${BIND_HOST:-127.0.0.1}:${BIND_PORT:-8787}/webhooks/linq?k=${WEBHOOK_SECRET}"
