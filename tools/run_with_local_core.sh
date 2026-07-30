#!/usr/bin/env bash
# Run a skill script against the sibling weather-skills-core checkout.
#
# Usage:
#   tools/run_with_local_core.sh skills/difference/scripts/difference.py --help
#   tools/run_with_local_core.sh skills/chirps-fetch/scripts/fetch.py --start 2026-01-01 --end 2026-01-02 -o /tmp/out.zarr
#
# Override the core path with WEATHER_SKILLS_CORE=/path/to/weather-skills-core.
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
exec uv run --with-editable "$CORE" --script "$@"
