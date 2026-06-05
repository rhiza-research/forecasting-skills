---
name: cmip6-fetch
description: Fetch a CMIP6 climate-model projection (e.g. temperature, precipitation) for a date range and region from the public, credential-free Pangeo Google Cloud catalog, and write a Rhiza Envelope Zarr. Use when a task needs climate-projection grids (historical or future scenario) for downstream clipping, aggregation, comparison, or plotting.
license: MIT
compatibility: Requires Python 3.11+ and uv. Reads the public Pangeo CMIP6 collection from Google Cloud (gs://cmip6) over anonymous access; no credentials required.
metadata:
  version: "0.1.0"
---

# cmip6-fetch

Resolves a single CMIP6 dataset from the [Pangeo CMIP6](https://pangeo-data.github.io/pangeo-cmip6-cloud/)
catalog on Google Cloud, opens its analysis-ready Zarr store anonymously, subsets
it by bounding box and time, maps its dimensions onto the Rhiza Envelope analysis
shape, and writes a consolidated Zarr store. CMIP6 is faceted, so the dataset is
selected with facet flags (model, experiment, variable, member, table, grid) that
are validated against the catalog CSV.

## When to use

- A task needs climate-model projection output — historical runs or future
  scenarios (ssp*) — as gridded data, with no credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a Rhiza
  Envelope Zarr.

CMIP6 is model projection, not observation or short-range forecast. For
reanalysis ground truth use `arco-era5-fetch`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model <id> --experiment <id> -v <variable> \
  [--member <id>] [--table <id>] [--grid <label>] \
  --start <date> --end <date> [--bbox N/W/S/E] -o <path.zarr>
```

### Arguments

- `--model` — CMIP6 `source_id` (e.g. `GFDL-CM4`).
- `--experiment` — CMIP6 `experiment_id` (e.g. `historical`, `ssp245`).
- `--variable`, `-v` — CMIP6 `variable_id`. CMIP6 stores one variable per
  dataset, so this both selects the dataset and names the output variable
  (e.g. `tas`, `pr`).
- `--member` — CMIP6 `member_id` (default `r1i1p1f1`).
- `--table` — CMIP6 `table_id` fixing the frequency/realm (default `Amon`;
  e.g. `day`).
- `--grid` — CMIP6 `grid_label` (e.g. `gn`, `gr1`). Required only when more than
  one grid matches the other facets; otherwise the single match is used.
- `--start`, `--end` — inclusive date range. Each value is one of an absolute
  ISO date `YYYY-MM-DD`; `now`/`today`; `latest` (the newest time present in the
  resolved dataset); or an offset `now-<int>{d|w}` / `latest-<int>{d|w}`
  (`w` = 7 days, capped at 36525 days). Absolute **future** dates are allowed
  (scenario experiments run to 2100); only future `+` offsets are rejected. The
  duration idiom and inclusive-both-ends boundary handling match the other
  fetchers. The cache key records the resolved absolute dates, never the token.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Longitudes are normalized
  to the [-180, 180) convention so negative west/east values select correctly.
  Omit for the full native grid. To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

If the facets match no dataset, the error lists the available experiments,
variables, and members for the chosen model. Ocean/curvilinear grids (2-D
latitude/longitude over index dims) are rejected — this fetcher handles only
regular 1-D lat/lon grids.

### Output

A consolidated Rhiza Envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying the requested variable. Source variable
units are forwarded verbatim. Times are decoded with the dataset's native
calendar (often `noleap` or `360_day`). Stamped with
`rhiza_source=cmip6:<model>/<experiment>/<member>/<table>/<variable>/<grid>`.

### Memory and performance

The store is opened dask-backed, so the bbox/time selection streams to Zarr
chunk-by-chunk on write and peak resident memory stays bounded to a few chunks
regardless of how long the window is. `--bbox` and the window length are the
levers. On tight-memory hosts keep the window short and the bbox tight, and run
the `clip-region` skill immediately after to shrink to your area of interest.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="cmip6-fetch"` and `input=null`. `args` records the
resolved facets (including the chosen `grid` and the dataset `data_version`), the
`bbox`, and the resolved concrete `start`/`end`. `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/fetch.py`, kept in lockstep with
`metadata.version` by the CI version-bump workflow.

## Examples

```bash
# GFDL-CM4 historical near-surface air temperature over Kenya, six months
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment historical -v tas \
  --start 2010-01-01 --end 2010-06-30 --bbox 7/32/-6/43 -o /tmp/cmip6.zarr

# A future scenario: precipitation under ssp245, full global grid, one year
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment ssp245 -v pr \
  --start 2050-01-01 --end 2050-12-31 -o /tmp/cmip6_ssp245.zarr
```
