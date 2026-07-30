---
name: arco-era5-fetch
description: Fetch ARCO-ERA5 reanalysis (temperature, wind, precipitation, pressure, and more) for a date range and region from the public, credential-free Google Cloud Zarr store, and write a weather-skills envelope Zarr. Use when a task needs multi-variable gridded reanalysis ground truth for comparison, verification, or downstream clipping/aggregation/plotting.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public ARCO-ERA5 analysis-ready Zarr from Google Cloud (gs://gcp-public-data-arco-era5) over anonymous access; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.9"
  catalog-group: fetchers
---

# arco-era5-fetch

Opens the [ARCO-ERA5](https://github.com/google-research/arco-era5) analysis-ready
Zarr store (`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`),
subsets it by bounding box, time range, and variables, maps it onto the weather-skills
envelope analysis shape, and writes a consolidated Zarr store. The store is a
uniform 0.25° equiangular lat/lon grid, hourly, opened with anonymous Google
Cloud access — no credentials and no API queue.

## When to use

- A task needs multi-variable reanalysis (2m temperature, winds, precipitation,
  geopotential, pressure-level fields) as gridded observations/ground truth.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  envelope Zarr.

Not a forecast — ERA5 is reanalysis. For forecast grids use `ecmwf-fetch` or
`dynamical-fetch`.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest time available in the ARCO ERA5 store). Both ends inclusive. Offsets like `latest-3w` / `now` are not accepted (decorator exits 2). Prefer recording resolved absolute dates in provenance.
