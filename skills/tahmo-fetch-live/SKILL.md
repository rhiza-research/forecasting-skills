---
name: tahmo-fetch-live
description: Fetch TAHMO station observations directly from the TAHMO REST API for one or more African countries and write a station-schema Rhiza Envelope Zarr. This is the ONLY working live-TAHMO path today — sheerwater's `tahmo-fetch` is cache-only and cannot fetch fresh station data. Use when a task needs precip / temperature / humidity / pressure observations for any date range that may not be in sheerwater's pre-populated cache.
license: MIT
compatibility: Requires Python 3.10+ and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
metadata:
  openclaw:
    requires:
      bins:
        - uv
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch-live

Downloads TAHMO station observations via the TAHMO SDK for the requested countries and date range. For each `(time, variable)` it picks the best-quality sensor (lowest TAHMO quality flag, filtering to flags <= 2), then resamples to daily (sum for `precip`, mean for `temperature`/`humidity`/`pressure`), and writes a station-schema Zarr store.

## How this differs from sheerwater's `tahmo-fetch`

Sheerwater also exposes a `tahmo-fetch` skill, but its underlying `tahmo_deployment` and `tahmo_station_cleaned` functions are stubs that raise `RuntimeError` whenever the cache misses. Live API access is not implemented in sheerwater yet (see TODO in `sheerwater.data.tahmo`). That means sheerwater's `tahmo-fetch` only returns whatever a separate ingest job has pre-populated.

This skill is the live API path. Use it when:

- You need TAHMO data for any date range that may not be in sheerwater's cache (e.g. recent or out-of-range dates).
- You have your own TAHMO_API_USERNAME / TAHMO_API_PASSWORD.

If both skills are installed and you don't know whether the cache covers your range, prefer this one — it always works as long as you have credentials.

When sheerwater implements its live TAHMO path, this skill becomes redundant and will be removed.

## When to use

- A task needs recent daily station observations for one or more named African countries.
- A downstream skill will compare stations against gridded precip (via `plot-compare`) or aggregate them temporally.

## Usage

```
uv run scripts/fetch.py --country K,G,... --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--country` — comma-separated country names. Supported: Kenya, Ghana, Senegal, Ethiopia, Burkina Faso, Benin, DR Congo, Côte d'Ivoire, Cameroon, Lesotho, Madagascar, Mali, Malawi, Mozambique, Niger, Nigeria, Rwanda, Chad, Togo, Tanzania, Uganda, South Africa, Zambia, Zimbabwe.
- `--start`, `--end` — inclusive date range (ISO).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`, `longitude(station_id)`, `country(station_id)`, and data variables `precip` (mm/day), `temperature` (°C), `humidity` (fraction), `pressure` (kPa) — whichever variables the stations report. Stamped with `rhiza_source=tahmo`.

## Example

```bash
uv run scripts/fetch.py --country Kenya,Ghana --start 2026-01-01 --end 2026-02-15 \
    --output /tmp/tahmo.zarr
```
