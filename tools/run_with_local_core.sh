#!/usr/bin/env bash
# Run a skill script against the sibling weather-skills-core checkout.
#
# Usage:
#   tools/run_with_local_core.sh skills/difference/scripts/difference.py --help
#   tools/run_with_local_core.sh skills/chirps-fetch/scripts/fetch.py --start-time 2026-01-01 --end-time 2026-01-02 -o /tmp/out.zarr
#
# Override the core path with WEATHER_SKILLS_CORE=/path/to/weather-skills-core.
#
# Per-script ``*.py.lock`` files pin a remote git rev of core; when present they
# can block ``--with-editable``. This helper temporarily moves the lock aside.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="${WEATHER_SKILLS_CORE:-$ROOT/../weather-skills-core}"
if [[ ! -d "$CORE" ]]; then
  echo "weather-skills-core not found at $CORE" >&2
  echo "Clone it as a sibling of forecasting-skills, or set WEATHER_SKILLS_CORE." >&2
  exit 1
fi
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <script.py> [args...]" >&2
  exit 2
fi
SCRIPT="$1"
shift
# Resolve relative script paths from repo root.
if [[ "$SCRIPT" != /* ]]; then
  SCRIPT="$ROOT/$SCRIPT"
fi
LOCK="${SCRIPT}.lock"
MOVED_LOCK=""
cleanup() {
  if [[ -n "$MOVED_LOCK" && -f "$MOVED_LOCK" ]]; then
    mv "$MOVED_LOCK" "$LOCK"
  fi
}
trap cleanup EXIT
if [[ -f "$LOCK" ]]; then
  MOVED_LOCK="$(mktemp "${LOCK}.XXXXXX")"
  mv "$LOCK" "$MOVED_LOCK"
fi
# Don't exec — trap must restore the lock after uv exits.
uv run --with-editable "$CORE" --script "$SCRIPT" "$@"
