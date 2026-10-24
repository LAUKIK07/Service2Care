#!/usr/bin/env bash
# Relay Linq inbound messages to the local receiver using the Linq CLI.
# Uses the isolated Linq config so any other Linq login on this machine stays untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
LINQ_BIN="${LINQ_BIN:-$HOME/.local/bin/linq}"
export HOME="${LINQ_HOME:-$HOME/.linq-signal2care-home}"
exec "$LINQ_BIN" webhooks listen --events message.received \
  --forward-to "http://${BIND_HOST:-127.0.0.1}:${BIND_PORT:-8787}/webhooks/linq?k=${WEBHOOK_SECRET}"
