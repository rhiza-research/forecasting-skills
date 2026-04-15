---
name: plot
description: Render a 2D heatmap or 1D time series PNG from any gridded or station Rhiza Envelope Zarr. Use when you need to visualize a single dataset as a map or as a time/step profile.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot

Source-agnostic single-dataset visualization. Two styles:
- `heatmap` — 2D map. Auto-reduces higher-dimensional arrays to `(lat, lon)` by averaging `number` and selecting the first slice along `step`/`time` (override with `--index`).
- `timeseries` — 1D profile. Averages across all non-time dims.

## When to use

- Producing a quick-look map for any gridded envelope.
- Producing a time/step profile for a gridded or station envelope.

For two-dataset comparisons, use the `plot-compare` skill.

## Usage

```
uv run scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|timeseries] \
    [--colormap NAME] [--title TEXT] [--index DIM=POS,...]
```

### Arguments
- `--input`, `-i` — Zarr input.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
- `--style` — `heatmap` (default) or `timeseries`.
- `--colormap` — matplotlib colormap name (default `viridis`).
- `--title` — optional plot title.
- `--index` — dim selections like `step=3,number=0` to override the default slice choice for `heatmap`.

### Output

A PNG at `--output`.

## Examples

```bash
uv run scripts/plot.py -i /tmp/ecmwf_kenya.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --colormap magma --title "S2S precip"
```

```bash
uv run scripts/plot.py -i /tmp/ecmwf_kenya.zarr -o /tmp/ts.png \
    --variable tp --style timeseries
```
