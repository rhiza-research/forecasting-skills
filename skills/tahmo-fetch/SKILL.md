---
name: tahmo-fetch
description: Fetch TAHMO station observations for one or more African countries and write a weather-skills envelope Zarr (station schema). Use when a task needs in-situ station rainfall/temperature/humidity/pressure, e.g. to compare against gridded satellite or forecast data.
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.14"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch

Downloads TAHMO station observations via the TAHMO SDK for the requested countries and date range. For each `(time, variable)` it picks the best-quality sensor (lowest TAHMO quality flag, filtering to flags <= 2), then resamples to daily (sum for `precip`, mean for `temperature`/`humidity`/`pressure`), and writes a station-schema Zarr store.

## When to use

- A task needs recent daily station observations for one or more named African countries.
- A downstream skill will compare stations against gridded precip (via `plot-compare`) or aggregate them temporally.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya [--country Ghana ...] --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--country` — country name (pass once per country). Supported: Kenya, Ghana, Senegal, Ethiopia, Burkina Faso, Benin, DR Congo, Côte d'Ivoire, Cameroon, Lesotho, Madagascar, Mali, Malawi, Mozambique, Niger, Nigeria, Rwanda, Chad, Togo, Tanzania, Uganda, South Africa, Zambia, Zimbabwe.
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest observation day on or before today (UTC) over a bounded lookback). Both ends inclusive. Offsets like `latest-3w` / `now` are not accepted (decorator exits 2). Prefer recording resolved absolute dates in provenance.
