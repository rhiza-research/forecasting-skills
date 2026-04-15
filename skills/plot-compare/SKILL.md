---
name: plot-compare
description: Render a side-by-side multi-panel comparison PNG of two Rhiza Envelope Zarr stores (gridded-vs-gridded or station-vs-gridded). Use for sat-vs-station validation, model-vs-obs comparison, or cross-source QC.
compatibility: Requires Python 3.10+ and uv.
---

# plot-compare

Source-agnostic two-dataset visualization. Produces a figure with one panel per time/step slice, top row from input A and bottom row from input B. Handles:
- Gridded A vs. gridded B (pcolormesh maps).
- Station A (station_id-indexed) vs. gridded B (scatter over mesh).

A shared colormap and normalization are used across both rows so values are visually comparable.

## When to use

- Validating a satellite product against station observations for a country.
- Comparing two forecasts (e.g. model A vs. model B) on the same axes.

## Usage

```
uv run scripts/plot_compare.py --a <a.zarr> --b <b.zarr> --output <out.png> \
    [--variable NAME] [--colormap NAME] [--title TEXT] \
    [--panels N] [--time-dim DIM]
```

### Arguments
- `--a`, `--b` — the two Zarr inputs. Station-schema is allowed on either.
- `--output`, `-o` — PNG path.
- `--variable` — variable name (must be present in both inputs; default: first var in A).
- `--colormap` — matplotlib colormap, default `viridis`.
- `--title` — figure title.
- `--panels` — number of panels per row (default 3).
- `--time-dim` — override the time axis. Defaults to `time` if present, else `step`.

### Output

A PNG with `2 * panels` subplots.

## Example

```bash
uv run scripts/plot_compare.py --a /tmp/tahmo.zarr --b /tmp/imerg_dekadal.zarr \
    --variable precip --output /tmp/sat_vs_station.png --title "IMERG vs TAHMO dekadal"
```
