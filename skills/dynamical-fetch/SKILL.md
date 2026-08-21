---
name: dynamical-fetch
description: Prefer this over credentialed fetchers when the dynamical.org catalog has the dataset. Default source for IMERG (`nasa-imerg-analysis-late` / `nasa-imerg-analysis-early`); do not start with imerg-fetch. Fetch a dataset from the open weather catalog (GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, their analyses, and the IMERG precipitation analyses) and write a weather-skills standard dataset Zarr. Use when a task needs credential-free forecast or analysis grids for downstream clipping, aggregation, comparison, or plotting. `-v` must be the catalog name (e.g. precipitation_surface), not total_precipitation / 2m_temperature from other fetchers. Pressure-level fields (`temperature_850hpa`, `geopotential_height_500hpa`) are stacked onto a `vertical` dim; `-v t` / `-v gh` select all native levels. Precip is already a rate — do not deaccumulate; aggregate-temporal then convert-to-totals for period mm.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Zarr from the dynamical.org open catalog (AWS Open Data) over HTTPS via the dynamical-catalog library; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precipitation_surface
    - temperature_2m
    - temperature_850hpa
    - geopotential_height_500hpa
---

# dynamical-fetch

Opens a dataset from the [dynamical.org](https://dynamical.org/catalog/) open
catalog with `dynamical-catalog`, subsets it by bounding box, time, and
variables, maps its dimensions onto the weather-skills standard dataset, and writes a
consolidated Zarr store. One skill covers the whole catalog — the dataset is
selected with `--dataset` and validated at runtime against
`dynamical_catalog.list()`.

## When to use

Prefer this fetcher whenever the [dynamical.org catalog](https://dynamical.org/catalog/)
has the dataset — GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, their analyses,
and IMERG early/late. It is credential-free and has no API queue.

**IMERG:** this is the default source (`--dataset nasa-imerg-analysis-late`
or `nasa-imerg-analysis-early`, `-v precipitation_surface`). Do not start
with `imerg-fetch`. That skill is the Earthdata daily Late/Final fallback
only (GES DISC granules, no bbox, credentials).

- A task needs a forecast ensemble, deterministic forecast, or gridded analysis
  from that catalog.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr. Fetch writes known precip as a **rate**
  (`mm day-1`) and known air temperature as `degree_Celsius`. Next steps are
  `aggregate-temporal` and (for `mm` totals) `convert-to-totals`. Do **not**
  run `deaccumulate` — fetchers already write rates.

Use `ecmwf-fetch` only for ECMWF **S2S** (subseasonal, ECDS credentials, 2-day
embargo, fuller pressure/ocean fields). Use source-specific fetchers (CHIRPS,
TAHMO, OISST, ARCO-ERA5, CMIP6, …) when the catalog does not carry that
product. For IMERG, use `imerg-fetch` only when you need GES DISC **daily**
Late or Final, not as the first choice.

## Usage

```
# Forecast datasets — a single init date:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --date <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>

# Analysis datasets — an inclusive date range:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --start-time <date> --end-time <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>

# Latest available day (no download):
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest <id>
```

### Supported datasets

The dataset shape determines which time flags apply and the output dims.

| Dataset id | Shape | Coverage | Output dims |
|---|---|---|---|
| `noaa-gefs-forecast-35-day` | ensemble forecast (31) | global | `(number, step, latitude, longitude)` |
| `ecmwf-ifs-ens-forecast-15-day-0-25-degree` | ensemble forecast (51) | global | `(number, step, latitude, longitude)` |
| `ecmwf-aifs-ens-forecast` | ensemble forecast (51) | global | `(number, step, latitude, longitude)` |
| `noaa-gfs-forecast` | deterministic forecast | global | `(step, latitude, longitude)` |
| `ecmwf-aifs-single-forecast` | deterministic forecast | global | `(step, latitude, longitude)` |
| `dwd-icon-eu-forecast-5-day` | deterministic forecast | Europe | `(step, latitude, longitude)` |
| `noaa-gfs-analysis` | analysis | global | `(time, latitude, longitude)` |
| `noaa-gefs-analysis` | analysis | global | `(time, latitude, longitude)` |
| `noaa-mrms-conus-analysis-hourly` | analysis | CONUS | `(time, latitude, longitude)` |
| `nasa-imerg-analysis-early` | analysis | global | `(time, latitude, longitude)` |
| `nasa-imerg-analysis-late` | analysis | global | `(time, latitude, longitude)` |

Pressure-level catalog fields add a `vertical` dim (hPa). The catalog only
publishes selected levels (IFS/AIFS: 925/850/500 hPa; GEFS: 500 hPa
geopotential only; GFS and ICON-EU: none), not a full native stack.

See <https://dynamical.org/catalog/> for each dataset's variables, resolution,
and update cadence. `--variable`/`-v` is the catalog name for that `--dataset`
(or `-v t` / `-v gh` for all native pressure levels). An unknown name exits
non-zero and prints `Available:`.

### Variable names

Do **not** reuse names from other fetchers. ARCO-ERA5 and ECMWF S2S use
`total_precipitation` / `2m_temperature` / `tp`; dynamical.org does not.

| Want | Typical dynamical `-v` | Do not pass |
|---|---|---|
| Precipitation | `precipitation_surface` | `total_precipitation`, `tp`, `precip` |
| 2 m temperature | `temperature_2m` | `2m_temperature`, `t2m`, `tas` |
| Pressure-level temperature | `temperature_850hpa` or `-v t` | `t2m` |
| Geopotential height | `geopotential_height_500hpa` or `-v gh` | `z` |

Those surface names are the ones on GEFS, GFS, and `ecmwf-ifs-ens-forecast-15-day-0-25-degree`. Catalog fields ending in `_Nhpa` are stacked onto a `vertical` coordinate (hPa) and renamed to the prefix (`temperature_850hpa` + `temperature_925hpa` → `temperature`). Height-above-ground fields (`temperature_2m`, `wind_u_80m`) stay separate. If you are unsure, pass `-v` once with a guess and read the `Available:` list — do not omit `-v` (that pulls every field).

The two HRRR datasets (`noaa-hrrr-forecast-48-hour`, `noaa-hrrr-analysis`) are
**not supported**: they are on a projected Lambert Conformal Conic grid (1-D
`y`/`x` in meters with 2-D `latitude(y,x)`/`longitude(y,x)`), which the 1-D
lat/lon standard dataset does not model. Selecting one exits non-zero. Converting a
projected grid to a regular lat/lon grid is a reprojection — a grid transform
out of scope for this fetcher.

### Arguments

- `--dataset` — catalog dataset id from the table above (validated against
  `dynamical_catalog.list()`; an unknown id prints the available list and exits).
- `--probe-latest [dataset-id]` — print the latest init or analysis time (`YYYY-MM-DD`) on stdout and exit. No `-o`. Pass the catalog id here or as `--dataset`.
- `--date` — forecast init date (**forecast datasets only**). Absolute ISO date `YYYY-MM-DD`. Selects the **00 UTC** initialization. Latest listed init: `--probe-latest <id>`. A very fresh GEFS 35-day init can still be filling its long leads.
- `--start-time`, `--end-time` — inclusive date range (**analysis datasets only**). Absolute ISO dates `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published time: `--probe-latest <this-id>`.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. The slice follows each
  axis's own order, so any region works regardless of how a dataset stores
  latitude. Omit to fetch the dataset's full native grid. Named places: compose
  with the `resolve-region` skill.
- `--variable`, `-v` — restrict to one data variable; repeat once per variable
  (`-v temperature_2m -v precipitation_surface`). Names are catalog-exact and
  dataset-specific — not `total_precipitation` (that is ARCO / ECMWF S2S).
  `-v t` / `-v gh` (and the prefixes `temperature` / `geopotential_height`)
  select every `*_Nhpa` field of that prefix. Omit to fetch all variables
  (usually too much).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr. Forecast datasets carry a scalar `time`
coord (the init date), `step` (forecast lead time, `timedelta64`), and — for
ensembles — `number` (member 0 is the control). Analysis datasets carry a
`time` dimension. Pressure-level fields add `vertical` (hPa). Known precip is converted to `mm day-1` and known air
temperature to `degree_Celsius`; other variables keep source units. Skip
`deaccumulate` — precip is already a rate. Stamped with `weather_skills_source=dynamical:<id>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="dynamical-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` is the argparse namespace
minus the `--output` path string, with the resolved concrete date(s)
substituted for any relative token. `version` is the `_SKILL_VERSION`
constant in `scripts/fetch.py`.

The `args` dict stores argparse dest names (underscored), not the hyphenated
CLI flag names. A consumer reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py <args>`
invocation must translate underscore → hyphen.

## Examples

```bash
# GEFS 35-day ensemble over a country (dummy bbox; use resolve-region for a real one)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gefs-forecast-35-day --date 2026-06-01 \
  --bbox 5/34/-5/42 -v precipitation_surface -o /tmp/gefs.zarr

# ECMWF IFS ensemble — precip is precipitation_surface, not total_precipitation
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf-ifs-ens-forecast-15-day-0-25-degree --date 2026-06-01 \
  --bbox 5/34/-5/42 -v precipitation_surface -o /tmp/ifs_ens.zarr

# IFS pressure-level temperature (850 + 925 hPa, stacked on `vertical`)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf-ifs-ens-forecast-15-day-0-25-degree --date 2026-06-01 \
  --bbox 5/34/-5/42 -v t -o /tmp/ifs_t.zarr

# GFS deterministic forecast for a specific init date, full global grid
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-forecast --date 2026-06-01 -o /tmp/gfs.zarr

# GFS analysis over a date range
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-analysis --start-time 2026-05-10 --end-time 2026-05-30 \
  --bbox 5/34/-5/42 -o /tmp/gfs_analysis.zarr

# IMERG Late (default IMERG source; do not start with imerg-fetch)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset nasa-imerg-analysis-late \
  --start-time 2026-07-21 --end-time 2026-08-19 --bbox 5/34/-5/42 \
  -v precipitation_surface -o /tmp/imerg.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the full per-dataset
dimension list and the dynamical → standard dataset coordinate mapping.
