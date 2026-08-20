---
name: dynamical-fetch
description: Fetch a dataset from the dynamical.org open weather catalog (GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, their analyses, and the IMERG precipitation analyses) and write a weather-skills standard dataset Zarr. Use when a task needs credential-free forecast or analysis grids for downstream clipping, aggregation, comparison, or plotting. `-v` must be the catalog name (e.g. precipitation_surface), not total_precipitation / 2m_temperature from other fetchers. Precip is already a rate — do not deaccumulate; aggregate-temporal then convert-to-totals for period mm.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Zarr from the dynamical.org open catalog (AWS Open Data) over HTTPS via the dynamical-catalog library; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  availability:
    shape: either
    policy: lag
    lag_days: 1
    note: dynamical.org catalog; ~1 day conservative lag (dataset-dependent)
    variants:
      nasa-imerg-analysis-late:
        shape: range
        earliest: 2000-06-01
        note: dynamical.org IMERG late analysis
      nasa-imerg-analysis-early:
        shape: range
        earliest: 2000-06-01
        note: dynamical.org IMERG early analysis
  variables:
    - precipitation_surface
    - temperature_2m
---

# dynamical-fetch

Opens a dataset from the [dynamical.org](https://dynamical.org/catalog/) open
catalog with `dynamical-catalog`, subsets it by bounding box, time, and
variables, maps its dimensions onto the weather-skills standard dataset, and writes a
consolidated Zarr store. One skill covers the whole catalog — the dataset is
selected with `--dataset` and validated at runtime against
`dynamical_catalog.list()`.

## When to use

- A task needs a forecast ensemble, deterministic forecast, or gridded analysis
  that the source-specific fetchers (ECMWF S2S, CHIRPS, TAHMO) don't provide,
  with no credentials and no API queue.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr. Precip comes out as a **rate** — next
  steps are `aggregate-temporal` and (for `mm` totals) `convert-to-totals`.
  Do **not** run `deaccumulate` (that is for ECMWF S2S `tp` only).

## Usage

```
# Forecast datasets — a single init date:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --date <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>

# Analysis datasets — an inclusive date range:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --start-time <date> --end-time <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
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

See <https://dynamical.org/catalog/> for each dataset's variables, resolution,
and update cadence. `--variable`/`-v` must be an **exact catalog name** for
that `--dataset`. An unknown name exits non-zero and prints `Available:`.

### Variable names

Do **not** reuse names from other fetchers. ARCO-ERA5 and ECMWF S2S use
`total_precipitation` / `2m_temperature` / `tp`; dynamical.org does not.

| Want | Typical dynamical `-v` | Do not pass |
|---|---|---|
| Precipitation | `precipitation_surface` | `total_precipitation`, `tp`, `precip` |
| 2 m temperature | `temperature_2m` | `2m_temperature`, `t2m`, `tas` |

Those two names are the ones on GEFS, GFS, and `ecmwf-ifs-ens-forecast-15-day-0-25-degree`. Other fields differ per dataset (`wind_u_10m`, `temperature_850hpa`, …). If you are unsure, pass `-v` once with a guess and read the `Available:` list — do not omit `-v` (that pulls every field).

The two HRRR datasets (`noaa-hrrr-forecast-48-hour`, `noaa-hrrr-analysis`) are
**not supported**: they are on a projected Lambert Conformal Conic grid (1-D
`y`/`x` in meters with 2-D `latitude(y,x)`/`longitude(y,x)`), which the 1-D
lat/lon standard dataset does not model. Selecting one exits non-zero. Converting a
projected grid to a regular lat/lon grid is a reprojection — a grid transform
out of scope for this fetcher.

### Arguments

- `--dataset` — catalog dataset id from the table above (validated against
  `dynamical_catalog.list()`; an unknown id prints the available list and exits).
- `--date` — forecast init date (**forecast datasets only**). Absolute ISO date `YYYY-MM-DD`. Selects the **00 UTC** initialization.
- `--start-time`, `--end-time` — inclusive date range (**analysis datasets only**). Absolute ISO dates `YYYY-MM-DD`.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. The slice follows each
  axis's own order, so any region works regardless of how a dataset stores
  latitude. Omit to fetch the dataset's full native grid. Named places: compose
  with the `resolve-region` skill.
- `--variable`, `-v` — restrict to one data variable; repeat once per variable
  (`-v temperature_2m -v precipitation_surface`). Names are catalog-exact and
  dataset-specific — not `total_precipitation` (that is ARCO / ECMWF S2S).
  Omit to fetch all variables (usually too much).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr. Forecast datasets carry a scalar `time`
coord (the init date), `step` (forecast lead time, `timedelta64`), and — for
ensembles — `number` (member 0 is the control). Analysis datasets carry a
`time` dimension. Source variable units are forwarded verbatim; this fetcher
does not convert them (e.g. GEFS / IFS-ENS `precipitation_surface` is a rate,
`kg m-2 s-1`, not an accumulation — skip `deaccumulate`). Stamped with `weather_skills_source=dynamical:<id>`.

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

# GFS deterministic forecast for a specific init date, full global grid
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-forecast --date 2026-06-01 -o /tmp/gfs.zarr

# GFS analysis over a date range
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-analysis --start-time 2026-05-10 --end-time 2026-05-30 \
  --bbox 5/34/-5/42 -o /tmp/gfs_analysis.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the full per-dataset
dimension list and the dynamical → standard dataset coordinate mapping.
