---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a weather-skills standard dataset. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station-vs-satellite comparison or verification.
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.14"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch

Downloads IMERG daily precipitation granules from NASA GES DISC via `earthaccess` for the requested date range and writes a global-grid Zarr store. The IMERG late release runs ~4 days behind realtime; callers typically shift the requested end date accordingly.

## When to use

- Need recent IMERG rainfall for a forecast-verification or station-comparison task.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> --output <path.zarr> [--version late|final]
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest available IMERG granule on or before today (UTC)). Both ends inclusive. See CONVENTIONS date grammar.
