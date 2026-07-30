---
name: ghcn-daily-fetch
description: Fetch NOAA GHCN-Daily global in-situ station observations (precipitation, max/min/avg temperature) for a date range and region, and write a station-schema weather-skills standard dataset. Use when a task needs credential-free worldwide daily station data, e.g. to compare against gridded satellite, reanalysis, or forecast data.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA GHCN-Daily from the public S3 website endpoint (noaa-ghcn-pds.s3.amazonaws.com) over HTTPS; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.7"
  catalog-group: fetchers
---

# ghcn-daily-fetch

Downloads NOAA Global Historical Climatology Network daily station observations
from the public, credential-free GHCN-Daily S3 dataset. It reads the station
metadata file to find stations inside the requested bounding box, downloads each
candidate station's per-station CSV concurrently, keeps only QC-passed rows for
the requested elements and date range, scales them to canonical units, and writes
a station-schema Zarr store.

The output is a fully CF-1.13 timeSeries Discrete Sampling Geometries (DSG) Zarr
plus the `weather_skills_history` provenance key — a superset of CF, not a separate format.

GHCN-Daily has ~130k stations and each is a separate whole-history download, so a
`--bbox` bounds the work to the stations you need.

## When to use

- A task needs recent or historical daily station observations anywhere in the
  world, without credentials.
- A downstream skill will compare stations against gridded precip/temperature
  (via `plot-compare`) or aggregate them temporally.

For African stations with sub-daily sensor data, `tahmo-fetch` is an alternative
(credentialed).

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--bbox N/W/S/E] --start <date> --end <date> [-v VAR ...] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest observation day available for the requested stations). Both ends inclusive (see CONVENTIONS date grammar).
