---
name: sample-fetch
description: Return a small pre-baked sample of a weather-skills data source as a standard dataset Zarr, selected by the source's provenance name (chirps, imerg, tahmo, smap, oisst, arco-era5, ghcn-daily, ecmwf-s2s, dynamical:noaa-gfs-analysis, cmip6:...). Use when a demo, test, or offline run needs a real store of a known shape without contacting a data provider or holding credentials.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads only the Zarr samples bundled in this skill's assets/ directory; contacts no data provider and needs no credentials.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - humidity
    - pressure
    - soil_moisture
    - sst
    - 2m_temperature
    - tmax
    - tmin
    - tp
    - temperature_2m
    - tas
---

# sample-fetch

Ships small Zarr samples for ten of the weather-skills data sources and writes
the requested one to `--output`. Each sample is a store a real fetcher produced,
so downstream skills see the dims, coords, units, and CF attrs they would see
after a live fetch.

## When to use

- A demo or an example pipeline needs input without provider credentials, quota,
  or provider availability.
- A test needs a real store of a known source's shape rather than a synthetic
  one.
- You want to see what a source's output looks like before committing to a live
  fetch.

When you need a particular date range, region, or variable, fetch that source
directly instead. This skill takes none of those: it returns the baked window
as-is.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py --source <source> -o <path.zarr>
```

### Arguments

- `--source` — which source's sample to return. Required. The value is the exact
  `weather_skills_source` string that sample carries; the table below lists every
  accepted value. An unrecognized value exits `2` and prints the full list.
- `--output`, `-o` — output Zarr path.

There is no `--start-time`, `--end-time`, `--bbox`, or `--variable`. The sample's
baked window is the only window.

### Bundled sources

Every store is consolidated Zarr. Each row's `--source` value is exactly the
string that sample's `weather_skills_source` root attr stamps.

| `--source` | Dims | Data variables (units) | Time coverage |
|---|---|---|---|
| `chirps` | `time` 7, `latitude` 194, `longitude` 160 | `precip` (`mm day-1`) | daily, 2026-04-06 .. 2026-04-12 |
| `imerg` | `time` 7, `longitude` 80, `latitude` 97 | `precip` (`mm day-1`) | daily, 2026-04-06 .. 2026-04-12 |
| `tahmo` | `time` 7, `station_id` 132 | `precip` (`mm day-1`), `temperature` (`degrees Celsius`), `humidity` (`-`), `pressure` (`kPa`) | daily, 2026-04-06 .. 2026-04-12 |
| `smap` | `time` 1, `latitude` 137, `longitude` 86 | `soil_moisture` (`cm3/cm3`), plus the scalar CF `latitude_longitude` grid-mapping variable it references | one day, 2026-04-09 |
| `oisst` | `time` 7, `latitude` 39, `longitude` 32 | `sst` (`degC`) | daily, 2026-04-06 .. 2026-04-12 |
| `arco-era5` | `time` 168, `latitude` 39, `longitude` 32 | `2m_temperature` (`K`) | hourly, 2026-04-06T00 .. 2026-04-12T23 |
| `ghcn-daily` | `time` 4, `station_id` 1 | `tmax` (`degC`), `tmin` (`degC`) | daily, 2026-04-07 .. 2026-04-10 |
| `ecmwf-s2s` | `number` 101, `step` 11, `latitude` 7, `longitude` 5 | `tp` (`kg m-2`) | scalar `time` init 2026-04-09; `step` 0, 7, 10, 14, 20, 21, 28, 30, 35, 40, 42 days; `valid_time` 2026-04-09 .. 2026-05-21 |
| `dynamical:noaa-gfs-analysis` | `time` 145, `latitude` 39, `longitude` 32 | `temperature_2m` (`degree_Celsius`) | hourly, 2026-04-06T00 .. 2026-04-12T00 |
| `cmip6:GFDL-CM4/ssp245/r1i1p1f1/day/tas/gr1` | `time` 7, `latitude` 10, `longitude` 7 | `tas` (`K`) | daily on a `noleap` calendar, 2026-04-06T12 .. 2026-04-12T12 |

Details worth knowing before you plot or compare:

- Every gridded sample sits inside about 5N to 4.7S and 33.9E to 41.9E, but each
  is on its source's own native grid, so cell size, cell centers, and latitude
  step direction differ between them. Put two of them on a common grid before
  combining them.
- `ghcn-daily` carries one station, `KEM00063686` (`ELDORET INTL`). `tahmo`
  carries 132 stations, spanning 4.5S to 3.5N and 34.2E to 40.7E.
- `ecmwf-s2s` is a forecast: a scalar `time` init plus a `step` lead axis and a
  101-member `number` axis. Its `tp` is an amount accumulated since init (`step`
  0 is zero everywhere), so take successive differences along `step` before
  comparing one lead time to another. The step axis is irregular — 0, 7, 10, 14,
  20, 21, 28, 30, 35, 40, 42 days — so those differences span unequal windows of
  7, 3, 4, 6, 1, 7, 2, 5, 5, and 2 days. Divide by each window's own length, not
  by a nominal interval.
- `cmip6:...` decodes its time axis to `cftime` objects, not `datetime64`, because
  the model calendar is `noleap`. Convert the calendar first if you need a
  `datetime64` axis.

### Output

The sample's values and coordinate labels unchanged: no re-dating, no subsetting,
no spatial or unit conversion. Global attrs pass through as the source stamped
them, including `weather_skills_source` set to the `--source` value, except
`weather_skills_history`, which is replaced (see Provenance below).

### Errors

Exit `2` covers:

- A missing `--source`.
- A `--source` that is not one of the bundled source strings. The message names
  the value and lists every available source.
- An `assets/` directory that is missing or holds no store. The message names the
  directory; the install is incomplete.

### Provenance

The `weather_skills_history` chain on the output records this skill, not the
fetch that originally produced the sample.

## Examples

```bash
# Daily CHIRPS precipitation, no provider call
uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py --source chirps -o /tmp/chirps.zarr

# A forecast ensemble to aggregate or plot
uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py --source ecmwf-s2s -o /tmp/ecmwf.zarr

# A colon-qualified source
uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py --source dynamical:noaa-gfs-analysis \
  -o /tmp/gfs.zarr

# A CMIP6 facet path
uv run ${CLAUDE_SKILL_DIR}/scripts/sample_fetch.py \
  --source cmip6:GFDL-CM4/ssp245/r1i1p1f1/day/tas/gr1 -o /tmp/cmip6.zarr
```
