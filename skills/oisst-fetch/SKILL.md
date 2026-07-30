---
name: oisst-fetch
description: Fetch NOAA OISST v2.1 daily sea-surface temperature for a date range and region from NOAA PSL's public OPeNDAP server, and write a weather-skills envelope Zarr. Use when a task needs credential-free gridded SST observations, e.g. for ocean analysis or comparison against forecasts/reanalysis.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA OISST v2.1 from NOAA PSL's OPeNDAP server (psl.noaa.gov) over HTTPS; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.8"
  catalog-group: fetchers
---

# oisst-fetch

Reads NOAA's Optimum Interpolation Sea Surface Temperature (OISST) v2.1 daily
0.25° global analysis from NOAA PSL's public OPeNDAP server, subsets it by
bounding box and time range, maps it onto the weather-skills envelope analysis shape, and
writes a consolidated Zarr store. OPeNDAP lets the skill pull only the requested
window rather than whole yearly files, with no credentials.

## When to use

- A task needs gridded sea-surface temperature observations (daily, 0.25°,
  global, 1981-09 to present), without credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  envelope Zarr.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest day present in the served OISST year files). Both ends inclusive. Offsets like `latest-3w` / `now` are not accepted (decorator exits 2). Prefer recording resolved absolute dates in provenance.
