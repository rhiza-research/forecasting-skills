---
name: cmip6-fetch
description: Fetch a CMIP6 climate-model projection (e.g. temperature, precipitation) for a date range and region from the public, credential-free Pangeo Google Cloud catalog, and write a weather-skills envelope Zarr. Use when a task needs climate-projection grids (historical or future scenario) for downstream clipping, aggregation, comparison, or plotting.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public Pangeo CMIP6 collection from Google Cloud (gs://cmip6) over anonymous access; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.7"
  catalog-group: fetchers
---

# cmip6-fetch

Resolves a single CMIP6 dataset from the [Pangeo CMIP6](https://pangeo-data.github.io/pangeo-cmip6-cloud/)
catalog on Google Cloud, opens its analysis-ready Zarr store anonymously, subsets
it by bounding box and time, maps its dimensions onto the weather-skills envelope analysis
shape, and writes a consolidated Zarr store. CMIP6 is faceted, so the dataset is
selected with facet flags (model, experiment, variable, member, table, grid) that
are validated against the catalog CSV.

## When to use

- A task needs climate-model projection output — historical runs or future
  scenarios (ssp*) — as gridded data, with no credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  envelope Zarr.

CMIP6 is model projection, not observation or short-range forecast. Historical
runs cover 1850–2014 and scenario (`ssp*`) runs continue to 2100, so the natural
windows are multi-year to multi-decadal.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model <id> --experiment <id> -v <variable> \
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

A consolidated weather-skills envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying the requested variable.

The output is fully CF-1.13 compliant. CMIP6 source data is already strongly
CF-compliant, so the transform preserves the source metadata and repairs what
mapping onto the envelope would otherwise break:

- **Global attrs.** The rich CMIP6 globals (`title`, `source`, `institution`,
  `references`, `tracking_id`, etc.) are preserved; `Conventions` is overwritten
  to `CF-1.13` (the source carries an older inherited value such as
  `CF-1.7 CMIP-6.0 UGRID-1.0`); a `history` line is appended; and
  `weather_skills_source=cmip6:<model>/<experiment>/<member>/<table>/<variable>/<grid>`
  plus the provenance `weather_skills_history` are added.
- **Coordinates.** `latitude`/`longitude`/`time` carry CF `standard_name`,
  `units` (`degrees_north`/`degrees_east`), and `axis` (`Y`/`X`/`T`). cf-xarray
  resolves the X/Y/T axes — verified on the way out.
- **Bounds integrity.** The weather-skills envelope does not carry cell bounds, so the
  `*_bnds` variables are dropped. The `bounds` attr each coordinate would
  otherwise still carry (a dangling CF §7.1 reference to an absent variable) is
  removed, so no coordinate points at a missing variable.
- **Calendar.** Times are decoded with the dataset's native calendar (often
  `noleap` or `360_day`); that source calendar and a udunits-valid `units` are
  carried into the written time encoding and re-verified on the written store, so
  the non-standard calendar is never silently coerced to `standard`.
- **Units.** The data variable's source `units`, `standard_name`, and
  `long_name` are forwarded verbatim; the `units` are validated with a real
  udunits check before writing.

### Memory and performance

The store is opened dask-backed, so the bbox/time selection streams to Zarr
chunk-by-chunk on write and peak resident memory stays bounded to a few chunks
regardless of how long the window is. There is no size threshold or refusal —
what to fetch is your call, from a few months over a small bbox to a global
multi-decadal pull. `--bbox` restricts the spatial extent; the `clip-region`
skill can shrink an existing output further. Monthly tables (`Amon`) are compact
(a full 1850–2014 historical monthly slice is on the order of ~100 MB); daily
tables (`day`) are correspondingly larger.

### Errors

Failures are reactive and each emits a one-line actionable message:

- A catalog-CSV download/network failure reports the catalog URL and what to
  check, then exits non-zero.
- Facets that match no dataset list the available experiments, variables, and
  members for the chosen model and exit 2.
- Several matching `grid_label`s list the grids and ask for `--grid` (exit 2).
- An ocean/curvilinear (2-D lat/lon) grid is rejected with a reprojection
  message (exit 2).
- A store open or write failure surfaces the underlying error.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="cmip6-fetch"` and `input=null`. `args` records the
resolved facets (`model`, `experiment`, `variable`, `member`, `table`, the chosen
`grid`, and the dataset `data_version`), the `bbox`, and the resolved concrete
`start`/`end`. `version` is this skill's version, also printed by `--help`.
Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# GFDL-CM4 historical near-surface air temperature over East Africa,
# a multi-decade monthly slice
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment historical -v tas \
  --table Amon --start 1980-01-01 --end 2014-12-31 --bbox 7/32/-6/43 -o /tmp/cmip6_hist.zarr

# Full historical monthly record (1850–2014), global grid
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment historical -v tas \
  --table Amon --start 1850-01-01 --end 2014-12-31 -o /tmp/cmip6_full_hist.zarr

# A future scenario: monthly precipitation under ssp245, mid-century decade
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment ssp245 -v pr \
  --table Amon --start 2040-01-01 --end 2049-12-31 -o /tmp/cmip6_ssp245.zarr
```
