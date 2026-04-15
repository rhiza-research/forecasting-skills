---
name: tahmo-fetch
description: Fetch TAHMO station observations for one or more African countries and write a Rhiza Envelope Zarr (station-dim schema). Use when a task needs in-situ station rainfall/temperature/humidity/pressure, e.g. to compare against gridded satellite or forecast data.
compatibility: Requires Python 3.10+ and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
---

# tahmo-fetch

Downloads TAHMO station observations via the TAHMO SDK for the requested countries and date range. For each `(time, variable)` it picks the best-quality sensor (lowest TAHMO quality flag, filtering to flags <= 2), then resamples to daily (sum for `precip`, mean for `temperature`/`humidity`/`pressure`), and writes a station-schema Zarr store.

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
