---
name: smap-fetch
description: Fetch NASA SMAP SPL3SMP_E daily 9 km volumetric soil moisture for a bounded region and short date range via Earthdata, and write a fully CF-1.13 weather-skills standard dataset. Use when a task needs gridded land-surface soil-moisture observations, e.g. for drought or agricultural analysis, or comparison against models.
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.9"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# smap-fetch

Downloads NASA SMAP Enhanced L3 radiometer soil moisture (`SPL3SMP_E`, 9 km,
daily) granules from NASA Earthdata via `earthaccess` and writes a gridded Zarr
store. The product is HDF5 on the global EASE-Grid 2.0; its degenerate 2-D
latitude/longitude reduce to 1-D coordinate vectors, so the output is a regular
1-D lat/lon weather-skills standard dataset. Each requested day is one ~690 MB granule, so this
skill is built for a bounded `--bbox` over a short window — pass `--bbox`.

## When to use

- A task needs gridded surface soil-moisture observations (daily, 9 km, land)
  over a bounded region.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  standard dataset.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] [--overpass AM|PM] -o <path.zarr>
```

Requires Earthdata credentials in the environment (`EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`) or a `.netrc` entry for `urs.earthdata.nasa.gov`, exactly
like `imerg-fetch`.

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest SPL3SMP_E granule date on or before today (UTC)). Both ends inclusive. See CONVENTIONS date grammar.
