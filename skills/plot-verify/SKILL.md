---
name: plot-verify
description: Plot a lead-week event-verification grid of maps for one observation week. Columns are week-4 through week-1 forecasts (least recent to most recent); rows are the obs product, the forecast product, and hits. Use after coarsening obs onto the forecast grid and selecting each forecast's verifying week. For precipitation, aggregate-temporal then convert-to-totals first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py *)
metadata:
  catalog-group: figure
---

# plot-verify

Event verification for **one observation week** as a **grid of maps**.
Columns run **least recent to most recent** (week-4 on the left, week-1
on the right):

| | Week 4 | Week 3 | Week 2 | Week 1 |
| --- | --- | --- | --- | --- |
| obs product | obs map | obs map | obs map | obs map |
| forecast product | week-4 map | week-3 map | week-2 map | week-1 map |
| Hits | hit map | hit map | hit map | hit map |

Each panel is a CartoPy heatmap (same style as `plot` /
`plot-compare-forecasts`), not a table of numbers. Each `--forecast` is
one column (pass week-4 first, week-1 last). Observation is the same
map in every column so you can read down a lead. The left of each row is
labeled with that row's product (`weather_skills_source` when stamped,
else Observation / Forecast / Hits). Hits use the
`event-hits` classification (event = variable ≥ `--threshold`, default 1):

| Value | Meaning |
| --- | --- |
| `1` (`hit`) | forecast and obs both ≥ threshold |
| `-1` (`disagree`) | one is ≥ threshold and the other is not |
| `0` (`below`) | both below the threshold |

**Hit rate** is probability of detection: hits / obs-events among finite
cells. It is printed to stdout per column; it is not drawn on the maps.

This skill does **not** fetch. Compose it after fetch → aggregate →
convert-to-totals → coarsen obs onto the forecast grid → `select` each
forecast's verifying week.

## When to use

- "For obs week W, how did the week-4 / week-3 / week-2 / week-1 forecasts
  do?" — as maps of obs, forecast, and hits.
- A verification figure that is a heatmap grid, not a table of values.

For a hits Zarr you can re-plot later, use `event-hits` then `plot`. For
several models over many times with no hit layer, use
`plot-compare-forecasts`.

## Pipeline (one obs week)

Pick the verifying week as absolute dates (`resolve-time` if the user said
"last week"). Then, for precipitation:

1. Fetch obs for that week; `aggregate-temporal --period weekly`;
   `convert-to-totals`.
2. For lead *k* in 1..4, fetch the forecast initialized *k* weeks before
   the week start. Aggregate to weekly, `convert-to-totals`, `step-to-time`
   if the cube still has `step`, then `select` the entry whose valid time
   is that obs week.
3. `coarsen --obs` onto the **forecast** lat/lon spacing and offset. Do
   not downscale the forecast onto obs.
4. `rename` if the variable names differ (`tp` vs `precip`).
5. This skill: `--obs` plus four `--forecast` paths, week-4 first
   (least recent) through week-1 last (most recent).

Inputs must already be **one week** (time/step size 1). A leftover `step`
axis without `time` is refused — run `step-to-time` and `select` first.
Obs must already be on the forecast grid.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs <obs.zarr> \
    --forecast <week4.zarr> --forecast <week3.zarr> \
    --forecast <week2.zarr> --forecast <week1.zarr> \
    -o <out.png> [--variable NAME] [--threshold 1] \
    [--lead "Week 4" --lead "Week 3" ...] \
    [--title TEXT] [--colormap NAME] [--bbox N/W/S/E] [--mask-geojson PATH]
```

### Arguments

- `--obs` — observation Zarr for the verifying week (required). Must
  already be on the forecast's spatial resolution.
- `--forecast` — forecast Zarr for that same week at one lead. Repeat
  once per lead, **week-4 first** (least recent) through **week-1 last**
  (most recent). At least one; typically four.
- `--variable`, `-v` — data variable. Default: each input's first usable
  variable (names may differ, like `event-hits`).
- `--threshold` — event cutoff in the stored units (default `1`).
- `--lead` — column title, once per `--forecast`. Default: `Week 4`,
  `Week 3`, … `Week 1` in input order (least recent → most recent).
- `--colormap` — matplotlib name or comma-separated colors for the
  obs/forecast rows. Default: discrete Kenya / ECMWF-S2S precip classes
  (same bins as `plot`); other variables `viridis`. Hits always use the
  discrete disagree / below / hit colors.
- `--title` — optional figure title.
- `--bbox` — optional `N/W/S/E` slice on every input; axes are set to
  that bbox.
- `--mask-geojson` — optional GeoJSON polygon; cells outside become NaN.
- `--output`, `-o` — PNG path.

### Output

A PNG at `--output`: a 3 × N heatmap grid (N = number of `--forecast`).
Rows are labeled on the left with the obs product, forecast product
(`weather_skills_source` when stamped), and Hits. One colorbar for the
obs/forecast maps, one for the hit flags. Stdout one
line per column with the hit rate, e.g. `Week 4  hit rate 72%  (18/25 obs events)`.

### Provenance

The decorator stamps `weather_skills_history` on the PNG. Inspect with
`provenance`. After plotting, run `inspect-figure` and look at the PNG.

## Example

```bash
# After fetch / weekly aggregate / convert-to-totals / coarsen / select:
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs /tmp/chirps_week.zarr \
    --forecast /tmp/s2s_week4.zarr \
    --forecast /tmp/s2s_week3.zarr \
    --forecast /tmp/s2s_week2.zarr \
    --forecast /tmp/s2s_week1.zarr \
    --variable precip --threshold 1 --bbox 5/34/-5/42 \
    --title "Kenya weekly precip ≥ 1 mm" \
    -o /tmp/verify_week.png
```
