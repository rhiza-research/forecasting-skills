---
name: plot-mediogram
description: Render an ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate (historical) ensemble at a single lat/lon. Two-layer boxplots per time step show p25–p75 IQR with caps invisible plus an extremes overlay (p0–p100 whiskers, p10–p90 box, p50 median).
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot-mediogram

Single-point mediogram plotting an ECMWF ensemble forecast distribution against an m-climate (historical) ensemble distribution. For each forecast step, two side-by-side boxplots are drawn (forecast left, m-climate right). Each side has two layers:

- **Outer box** — p25–p75 IQR, whiskers do not extend (caps invisible).
- **Inner box overlaid** — p0–p100 whiskers, p10–p90 box, p50 median line in black.

Forecast boxes are filled cyan; m-climate boxes are filled red. The forecast ensemble mean is plotted as a black line across steps.

## Input schema

Both inputs are Zarr stores with at least:
- a `number` dim (ensemble members)
- a `step` dim (forecast lead time)
- spatial coords identifiable as `latitude`/`longitude` (CF-style or common aliases)
- at least one data variable

Lat/lon selection is nearest-neighbor.

## Usage

```
uv run scripts/plot_mediogram.py --forecast <forecast.zarr> --mclimate <mclimate.zarr> \
    --lat <lat> --lon <lon> --output <out.png> \
    [--variable NAME] [--title TEXT]
```

### Arguments
- `--forecast` — forecast Zarr (`number` × `step` × spatial).
- `--mclimate` — m-climate Zarr (same schema).
- `--lat`, `--lon` — point location (nearest-neighbor selection).
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable in the forecast input.
- `--title` — optional plot title.

### Output

A PNG at `--output`, single axes, figsize `(10, 5)`, up to 6 forecast steps on the x-axis.

## Example

```bash
uv run scripts/plot_mediogram.py \
    --forecast /tmp/ecmwf_forecast.zarr \
    --mclimate /tmp/ecmwf_mclimate.zarr \
    --lat -1.3 --lon 36.8 \
    --variable tp \
    --output /tmp/mediogram_nairobi.png
```
