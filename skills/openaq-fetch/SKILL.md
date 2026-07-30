---
name: openaq-fetch
description: Fetch OpenAQ air-quality station observations (PM2.5, PM10, NO2, O3, SO2, CO) for a date range and region, and write a station-schema weather-skills envelope Zarr. Use when a task needs in-situ air-quality and atmospheric-composition data, e.g. to compare against gridded model output.
license: MIT
compatibility: Requires Python 3.12 and uv. Uses the OpenAQ v3 REST API over HTTPS; requires a free OPENAQ_API_KEY in the environment (register at https://explore.openaq.org/register).
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.8"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - OPENAQ_API_KEY
    primaryEnv: OPENAQ_API_KEY
---

# openaq-fetch

Downloads ground-based air-quality observations from the OpenAQ v3 API. It finds
monitoring locations inside the requested bounding box, fetches each matching
sensor's daily-aggregated values over the date range concurrently, and writes a
station-schema Zarr store.

## When to use

- A task needs daily air-quality and atmospheric-composition station observations
  (PM2.5, PM10, NO2, O3, SO2, CO) for a region.
- A downstream skill will compare stations against gridded data (via
  `plot-compare`) or aggregate them temporally.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --start <date> --end <date> [-v VAR ...] -o <path.zarr>
```

Requires `OPENAQ_API_KEY` in the environment (free; register at
https://explore.openaq.org/register).

### Arguments
- `--bbox` — spatial subset `N/W/S/E` decimal degrees (required; selects which
  monitoring locations to fetch). To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest observation day available for the requested locations). Both ends inclusive. Offsets like `latest-3w` / `now` are not accepted (decorator exits 2). Prefer recording resolved absolute dates in provenance.
