---
name: plot-compare-forecasts
description: Compare two or more gridded datasets as a heatmap grid PNG. Each input is a row; columns are the union of times (forecast init+step, or a time dim on observations / analyses). A dataset that lacks a column's time is a blank n/a cell, not a dropped column. Use after aggregating to a common resolution. For precipitation, convert-to-totals after that aggregation before plotting. For a single dataset use plot; for exactly two datasets including station-vs-grid use plot-compare.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare_forecasts.py *)
metadata:
  catalog-group: figure
---

# plot-compare-forecasts

N-dataset comparison grid. Each `--input` is one row; columns are the
**union** of times across those inputs, sorted earliest-first. A cell whose
dataset has no field at that time stays on the grid as a blank `n/a` panel
(map frame kept, no mesh) — unlike `plot-compare`, which drops any bin the
other input does not share.

Rows may mix:

- **Forecast** cubes — valid time is `init + step` (scalar init `time` +
  `step` lead dim). Do not run `step-to-time` first.
- **Observations / analyses** — a calendar `time` dim (CHIRPS, IMERG, a
  reanalysis, a previously realized forecast, …).

Inputs must already share a time resolution (same median spacing, same
datetime vs timedelta kind, same calendar). Daily CHIRPS against weekly S2S
is refused — aggregate both with `aggregate-temporal` first, then
`convert-to-totals` so precipitation figures are period `mm`, not rates. Ensemble members
(`number`) are averaged. Maps only; no station row. `--variable` must exist
in every input (use `rename` if datasets use different names, e.g. `tp` vs
`precip`).

## When to use

- Comparing several forecasts (S2S, GEFS, IFS ENS, AIFS, …) as maps over the
  same valid-time horizon.
- Comparing those forecasts against a gridded ground-truth or analysis
  product on the same times.
- A shorter-range model (or a shorter obs window) should show blank cells
  rather than shrinking the grid.

For one dataset, use `plot`. For exactly two datasets (including
station-vs-grid), use `plot-compare`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare_forecasts.py -i <a.zarr> -i <b.zarr> [-i <c.zarr> ...] \
    --output <out.png> [--variable NAME] [--title TEXT] [--colormap NAME] \
    [--bbox N/W/S/E] [--mask-geojson PATH] [--panels N]
```

### Arguments
- `--input`, `-i` — input Zarr; repeat once per dataset (at least twice).
  Order is the row order. Row labels come from `weather_skills_source` when
  stamped, else `input 1`, `input 2`, …
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable of
  the first input. Must exist in every input.
- `--colormap` — matplotlib colormap name, or comma-separated colors to
  interpolate. When omitted, precipitation uses the Kenya / ECMWF-S2S
  palette; every other variable uses `viridis`. One shared scale across all
  present cells.
- `--title` — optional figure title.
- `--panels` — cap on columns, keeping the earliest N of the union. Default
  unset → every union column.
- `--bbox` — optional `N/W/S/E` decimal degrees. Rectangular `sel` slice on
  every input; axes are set to that bbox. To restrict to a country, get its
  bbox from the `resolve-region` skill. Longitudes in `[0, 360]` are
  auto-wrapped to `[-180, 180]` before slicing. Default unset → no slice;
  extent comes from the first input.
- `--mask-geojson` — optional GeoJSON boundary polygon. Cells whose centers
  fall outside are NaN. Combine with `--bbox` (slice then mask).

### Behavior

- **Time union.** Columns are unique valid times (forecast `init+step`, or
  the `time` dim) matched within 1 second. Same leads at different inits
  land in different columns. No overlapping time between any pair of inputs
  is an error.
- **Shared resolution.** Median bin width must match across inputs; a
  mismatch asks you to `aggregate-temporal` first.
- **Blank cells.** Missing times keep the map frame (extent, coast/borders)
  and show `n/a`. The axes stay visible so the grid is rectangular.
- **Column titles.** `YYYY-MM-DD`. When median spacing is at least 2 days,
  a right-edge range (`YYYY-MM-DD to YYYY-MM-DD`) is used, matching
  `aggregate-temporal`. A `+7d`-style lead is appended when the source still
  has a `step` coord.
- **Color scale.** One scale from all present (non-`n/a`) cells. Differing
  `units` across inputs print a stderr warning; the figure is still written.

### Output

A PNG at `--output`: `nrows = n inputs`, `ncols = union columns` (or
`--panels`). One horizontal colorbar under the grid.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['weather_skills_history']))"
```

## Examples

Weekly S2S + GEFS + IFS after aggregating each cube to the same period:

```bash
# After resolve-region, fetch, clip-region, aggregate-temporal (+ convert-to-totals
# if you want period mm) for each model:
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare_forecasts.py \
    -i /tmp/s2s_weekly.zarr -i /tmp/gefs_weekly.zarr -i /tmp/ifs_weekly.zarr \
    --variable tp --bbox 5/34/-5/42 \
    --title "East Africa weekly precip" \
    --output /tmp/compare_forecasts.png
```

Forecast vs gridded observations on the same weekly axis (rename if the obs
variable is not `tp`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare_forecasts.py \
    -i /tmp/s2s_weekly.zarr -i /tmp/chirps_weekly.zarr \
    --variable precip --bbox 5/34/-5/42 \
    --title "S2S vs CHIRPS" \
    --output /tmp/s2s_vs_chirps.png
```
