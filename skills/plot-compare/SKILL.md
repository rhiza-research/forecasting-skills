---
name: plot-compare
description: Render a side-by-side multi-panel comparison PNG of two Rhiza Envelope Zarr stores (gridded-vs-gridded or station-vs-gridded). Use for sat-vs-station validation, model-vs-obs comparison, or cross-source QC.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot-compare

Source-agnostic two-dataset visualization. Produces a 2-row figure
with one panel per time slice; row A is one input, row B the other.
Handles:

- Gridded vs. gridded (pcolormesh maps).
- Station (`station_id`-indexed) vs. gridded (scatter over mesh).

When exactly one input is a station-schema Zarr, that input is placed
on the top row to match the canonical "stations vs. satellite" layout.

A shared categorical precipitation colormap with `BoundaryNorm` is the
default so values are visually comparable across rows. An admin-1
country boundary overlay (Natural Earth, fetched and cached via
`cartopy`) is drawn on every panel.

## When to use

- Validating a satellite product against station observations for a country.
- Comparing two forecasts (e.g. model A vs. model B) on the same axes.

## Usage

```
uv run scripts/plot_compare.py --a <a.zarr> --b <b.zarr> --output <out.png> \
    [--variable NAME] [--colormap NAME] [--title TEXT] \
    [--panels N] [--time-dim DIM] [--agg dekadal|weekly]
```

### Arguments
- `--a`, `--b` — the two Zarr inputs. Station-schema is allowed on either.
- `--output`, `-o` — PNG path.
- `--variable` — variable name (must be present in both inputs; default: first var in A).
- `--colormap` — matplotlib colormap. When omitted, the categorical
  precipitation cmap (`["#bdbdbd", "wheat", "lightgreen", "green",
  "lightblue", "blue", "yellow", "orange", "red", "purple"]`) with
  `BoundaryNorm` over `[0, 10, 20, 40, 60, 80, 110, 150, 200, 250, 350]`
  mm is used.
- `--title` — figure title.
- `--panels` — number of panels per row (default 3). Ignored when `--agg` is set.
- `--time-dim` — override the time axis. Defaults to `time` if present, else `step`.
- `--agg` — `dekadal` (3 panels, 10-day window) or `weekly` (4 panels,
  7-day window). Sets panel count and labels each panel title with
  the start/end date of the aggregation window. The skill does not
  itself roll the data; pass appropriately aggregated inputs.

### Output

A PNG with a `(2, n)` `GridSpec` (`figsize=(22, 10)`,
`wspace=0.08`, `hspace=0.15`). Each row gets its own colorbar.
Station scatter points use `s=30`. Y-axis labels appear only on the
leftmost panel of each row.

## Example

```bash
uv run scripts/plot_compare.py --a /tmp/tahmo.zarr --b /tmp/imerg_dekadal.zarr \
    --variable precip --output /tmp/sat_vs_station.png \
    --title "IMERG vs TAHMO dekadal" --agg dekadal
```
