#!/usr/bin/env bash
# Garmin Coach launcher (macOS / Linux / Git-Bash).
#   ./scripts/run.sh          # real data (needs one-time auth)
#   ./scripts/run.sh --demo   # synthetic demo data, no Garmin account
#   ./scripts/run.sh --auth   # one-time Garmin login (email/pw/MFA)
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--auth" ]]; then
  echo "Launching one-time Garmin authentication (token -> ~/.garminconnect)..."
  exec uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
fi

if [[ "${1:-}" == "--demo" ]]; then export GARMIN_COACH_DEMO=1; else unset GARMIN_COACH_DEMO; fi
export GARMIN_COACH_PORT="${GARMIN_COACH_PORT:-8765}"

echo "Starting Garmin Coach on http://127.0.0.1:${GARMIN_COACH_PORT}  (demo=${GARMIN_COACH_DEMO:-0})"
exec uv run python -m server.app
