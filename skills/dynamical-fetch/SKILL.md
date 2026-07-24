---
name: dynamical-fetch
description: Fetch a dataset from the dynamical.org open weather catalog (GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, and their analyses) and write a weather-skills envelope Zarr. Use when a task needs credential-free forecast or analysis grids for downstream clipping, aggregation, comparison, or plotting.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Zarr from the dynamical.org open catalog (AWS Open Data) over HTTPS via the dynamical-catalog library; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.12"
  catalog-group: fetchers
---

# dynamical-fetch

Opens a dataset from the [dynamical.org](https://dynamical.org/catalog/) open
catalog with `dynamical-catalog`, subsets it by bounding box, time, and
variables, maps its dimensions onto the weather-skills envelope, and writes a
consolidated Zarr store. One skill covers the whole catalog — the dataset is
selected with `--dataset` and validated at runtime against
`dynamical_catalog.list()`.

## When to use

- A task needs a forecast ensemble, deterministic forecast, or gridded analysis
  that the source-specific fetchers (ECMWF S2S, CHIRPS, IMERG, TAHMO) don't
  provide, with no credentials and no API queue.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills envelope Zarr.

## Usage

```
# Forecast datasets — a single init date:
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --date <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>

# Analysis datasets — an inclusive date range:
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> --start <date> --end <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
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

See <https://dynamical.org/catalog/> for each dataset's variables, resolution,
and update cadence. `--variable`/`-v` lists the exact variable names per dataset
if you pass an unknown one.

The two HRRR datasets (`noaa-hrrr-forecast-48-hour`, `noaa-hrrr-analysis`) are
**not supported**: they are on a projected Lambert Conformal Conic grid (1-D
`y`/`x` in meters with 2-D `latitude(y,x)`/`longitude(y,x)`), which the 1-D
lat/lon envelope does not model. Selecting one exits non-zero. Converting a
projected grid to a regular lat/lon grid is a reprojection — a grid transform
out of scope for this fetcher.

### Arguments

- `--dataset` — catalog dataset id from the table above (validated against
  `dynamical_catalog.list()`; an unknown id prints the available list and exits).
- `--date` — forecast init date (**forecast datasets only**). Selects the **00
  UTC** initialization of the resolved date. Accepts the relative-or-absolute
  date grammar (below).
- `--start`, `--end` — inclusive date range (**analysis datasets only**). Same
  date grammar.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. The slice follows each
  axis's own order, so any region works regardless of how a dataset stores
  latitude. Omit to fetch the dataset's full native grid.
- `--variable`, `-v` — restrict to one data variable; repeat once per variable
  (`-v temperature_2m -v precipitation_surface`). Omit to fetch all variables.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

#### Relative-or-absolute date grammar

`--date`, `--start`, and `--end` each accept:

- an absolute ISO date `YYYY-MM-DD`;
- `now` or `today` — the current UTC date;
- `latest` — the newest date present in the dataset (read from its `init_time`
  for forecasts, `time` for analysis — no extra network call beyond the open);
- an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
  days). The offset is capped at 36525 days; a larger value, a future `+`
  offset, a month/year unit, or any malformed value exits 2 before any data is
  read.

For `--start`/`--end`, absolute endpoints and ordinary relative ranges are
inclusive of both ends. The **duration idiom** — start `B-<int>{d|w}` paired
with end exactly its own base `B` (both `now`, or both `latest`) — yields an
N-day window inclusive of `B` with the far edge shifted in by one (so
`latest-3w .. latest` = 21 days incl. `latest`). For any relative token the
resolved concrete date(s) are echoed to stderr before fetching. The cache key
records the resolved absolute date(s), never the relative token.

### Output

A consolidated weather-skills envelope Zarr. Forecast datasets carry a scalar `time`
coord (the init date), `step` (forecast lead time, `timedelta64`), and — for
ensembles — `number` (member 0 is the control). Analysis datasets carry a
`time` dimension. Source variable units are forwarded verbatim; this fetcher
does not convert them (e.g. GEFS `precipitation_surface` is a rate,
`kg m-2 s-1`, not an accumulation). Stamped with `weather_skills_source=dynamical:<id>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="dynamical-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` is the argparse namespace
minus the `--output` path string, with the resolved concrete date(s)
substituted for any relative token. `version` is the `_SKILL_VERSION`
constant in `scripts/fetch.py`, kept in lockstep with `metadata.version` in
this SKILL.md by the CI version-bump workflow.

The `args` dict stores argparse dest names (underscored), not the hyphenated
CLI flag names. A consumer reconstructing a `uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py <args>`
invocation must translate underscore → hyphen.

## Examples

```bash
# GEFS 35-day ensemble, newest init, Kenya bbox, one variable
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gefs-forecast-35-day --date latest \
  --bbox 7/32/-6/43 -v precipitation_surface -o /tmp/gefs.zarr

# GFS deterministic forecast for a specific init date, full global grid
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-forecast --date 2026-06-01 -o /tmp/gfs.zarr

# GFS analysis over a 3-week window ending at the newest available time
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gfs-analysis --start latest-3w --end latest \
  --bbox 12/-4/4/2 -o /tmp/gfs_analysis.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the full per-dataset
dimension list and the dynamical → envelope coordinate mapping.
