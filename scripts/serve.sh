#!/usr/bin/env bash
# Run the Signal2Care receiver on localhost. Settings come from .env.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
exec uv run uvicorn signal2care.app:app --host "${BIND_HOST:-127.0.0.1}" --port "${BIND_PORT:-8787}"
