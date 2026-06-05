---
name: smap-fetch
description: Fetch NASA SMAP SPL3SMP_E daily 9 km volumetric soil moisture for a date range and region via Earthdata, and write a Rhiza Envelope Zarr. Use when a task needs gridded land-surface soil-moisture observations, e.g. for drought or agriculture analysis or comparison against models.
license: MIT
compatibility: Requires Python 3.11+ and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
metadata:
  version: "0.1.0"
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
1-D lat/lon Rhiza Envelope.

## When to use

- A task needs gridded surface soil-moisture observations (daily, 9 km, global
  land).
- A downstream skill will clip, aggregate, compare, or plot the result as a Rhiza
  Envelope Zarr.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] [--overpass AM|PM] -o <path.zarr>
```

Requires Earthdata credentials in the environment (`EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`) or a `.netrc` entry, exactly like `imerg-fetch`.

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest available SPL3SMP_E granule date (discovered via
    `earthaccess` over a bounded lookback);
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days). The offset is capped at 36525 days; a larger value, a future `+`
    offset, a month/year unit, or any malformed value exits 2 before any network
    call.

  Boundary handling matches the other fetchers (inclusive both ends; duration
  idiom for `B-<int>{d|w}` .. `B`). For a relative token the resolved concrete
  window is echoed to stderr. The cache key records the resolved absolute dates,
  never the token.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. SMAP longitudes are
  already in [-180, 180), so negative west/east values select directly. Omit for
  the full global grid (large). To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--overpass` — which half-orbit group to read: `AM` (6am descending, default)
  or `PM` (6pm ascending).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated Rhiza Envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying `soil_moisture` (volumetric, cm3/cm3).
Latitude is the EASE-Grid 2.0 global vector (non-uniformly spaced, descending);
ocean and no-retrieval cells are NaN. Stamped with `rhiza_source=smap`.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="smap-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records `bbox`, `overpass`,
and the resolved concrete `start`/`end`. `version` is the `_RHIZA_SKILL_VERSION`
constant in `scripts/fetch.py`, kept in lockstep with `metadata.version` by the
CI version-bump workflow.

## Examples

```bash
# Soil moisture over the Horn of Africa for two days (AM overpass)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 12/32/-6/52 --start 2024-06-01 --end 2024-06-02 \
  -o /tmp/smap.zarr

# Last 3 weeks ending at the newest available granule, PM overpass
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start latest-3w --end latest --overpass PM \
  --bbox 12/32/-6/52 -o /tmp/smap_pm.zarr
```
