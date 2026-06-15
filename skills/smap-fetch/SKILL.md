---
name: smap-fetch
description: Fetch NASA SMAP SPL3SMP_E daily 9 km volumetric soil moisture for a bounded region and short date range via Earthdata, and write a fully CF-1.13 weather-skills envelope Zarr. Use when a task needs gridded land-surface soil-moisture observations, e.g. for drought or agricultural analysis, or comparison against models.
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
metadata:
  version: "0.1.5"
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
1-D lat/lon weather-skills envelope. Each requested day is one ~690 MB granule, so this
skill is built for a bounded `--bbox` over a short window — pass `--bbox`.

## When to use

- A task needs gridded surface soil-moisture observations (daily, 9 km, land)
  over a bounded region.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  envelope Zarr.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] [--overpass AM|PM] -o <path.zarr>
```

Requires Earthdata credentials in the environment (`EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`) or a `.netrc` entry for `urs.earthdata.nasa.gov`, exactly
like `imerg-fetch`.

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
  never the token. Keep windows short — each day is a separate large granule.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees (optional). Strongly
  recommended for every run, since each daily granule is the entire ~690 MB
  global grid. SMAP longitudes are already in [-180, 180), so negative west/east
  values select directly. A box with west > east is treated as
  antimeridian-crossing (e.g. `12/170/-6/-170`): it selects the union of the two
  longitude bands (lon ≥ west or lon ≤ east). To fetch over a country, resolve
  its bbox with the `resolve-region` skill.
- `--overpass` — which half-orbit group to read: `AM` (6am descending, default)
  or `PM` (6pm ascending).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated, fully **CF-1.13** weather-skills envelope Zarr with dims
`(time, latitude, longitude)`. The store is CF-compliant first, with
`weather_skills_history` added on top — the envelope is a CF superset, not a separate
format.

- Global attrs: `Conventions="CF-1.13"`, `title`, `source` (SMAP SPL3SMP_E),
  `institution`, `references`, `history`, plus `weather_skills_source="smap"` and
  `weather_skills_history`.
- `latitude`: `standard_name=latitude`, `units=degrees_north`, `axis=Y`;
  `longitude`: `standard_name=longitude`, `units=degrees_east`, `axis=X`;
  `time`: `standard_name=time`, `axis=T`, with udunits `units` + `calendar` in
  the write encoding.
- `soil_moisture`:
  `standard_name="volume_fraction_of_condensed_water_in_soil"`, `units="m3 m-3"`
  (the file's `cm3/cm3` is a dimensionless volumetric ratio; `m3 m-3` is its
  udunits-valid, dimensionless CF form — equivalent to a bare `1` but more
  meaningful to downstream tools; the human form is kept in `long_name`). The
  granule's own `soil_moisture` units attribute is read and confirmed to be the
  expected dimensionless ratio before relabeling; an unexpected value fails the
  run rather than silently mislabeling. Also carries `long_name`, an explicit NaN
  `_FillValue`, and a `grid_mapping="latitude_longitude"` reference. The written
  units are validated against udunits before write.
- A `latitude_longitude` `grid_mapping` variable carries the geographic CRS for
  the lat/lon presentation of the EASE-Grid 2.0 cells, on the WGS84 ellipsoid
  (EASE-Grid 2.0 is defined on WGS84). Latitude is the EASE-Grid 2.0 vector
  (non-uniformly spaced, descending), preserved exactly; ocean and no-retrieval
  cells are NaN.

`cf-xarray` resolves the X/Y/T axes from these attrs, and the script runs that
decode check on write — a stamping regression fails loudly rather than shipping
a store downstream skills cannot read.

### Auth errors

Earthdata authentication is non-interactive: the script tries the `environment`
(EARTHDATA_USERNAME/PASSWORD or EARTHDATA_TOKEN) then `netrc` strategy and never
prompts. If neither yields an authenticated session — or a search/download
returns 401/403 — it exits non-zero with a single actionable line
("configure EARTHDATA_USERNAME/EARTHDATA_PASSWORD or a urs.earthdata.nasa.gov
entry in ~/.netrc"). No credential value is read, printed, or checked, and no
traceback is shown for the auth path.

### Missing days

If some requested days have no published granule, the script writes the days
that exist and emits a stderr warning naming the skipped days, rather than
silently returning a shorter result. The warning distinguishes trailing missing
days (after the last available day — the usual publication-lag case near the
present) from interior gaps (a missing day between two available days), so it
does not assert a false "not yet published" cause for a genuine interior gap.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="smap-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records `bbox`, `overpass`,
and the resolved concrete `start`/`end`. `version` is this skill's version, also
printed by `--help`. Inspect a written output's provenance with the `provenance`
skill.

### Memory and performance

The output is streamed one day at a time — each daily granule is downloaded, read
into a single (latitude, longitude) array, written/appended to Zarr, and released
before the next — so peak resident memory is bounded to one day's grid. Downloaded
granules are large (~690 MB each) and are staged to a temp directory, so disk,
not RAM, bounds the window. Keep the window short and `--bbox` tight, and run the
`clip-region` skill afterward for further trimming.

## Examples

```bash
# Soil moisture over a Horn-of-Africa bbox for two days (AM overpass)
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 12/32/-6/52 --start 2024-06-01 --end 2024-06-02 \
  -o /tmp/smap.zarr

# A bounded bbox over the last week ending at the newest available granule, PM overpass
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 12/32/-6/52 --start latest-1w --end latest \
  --overpass PM -o /tmp/smap_pm.zarr
```
