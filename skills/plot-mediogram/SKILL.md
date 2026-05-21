---
name: plot-mediogram
description: Render an ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate (historical) ensemble at a single lat/lon. Two-layer boxplots per time step show an extremes box underneath (p0–p100 whiskers, p10–p90 box, p50 median) with a wider p25–p75 IQR box overlaid on top, whose visible black caps mark the IQR edges.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot-mediogram

Single-point mediogram plotting an ECMWF ensemble forecast distribution against an m-climate (historical) ensemble distribution. For each forecast step, two side-by-side boxplots are drawn (forecast left, m-climate right). Each side has two layers, drawn underneath-to-overlay:

- **Extremes box (drawn first, underneath)** — p0–p100 whiskers, p10–p90 box, p50 median line in black. Caps invisible.
- **IQR box (overlaid on top)** — p25–p75 box, whiskers zero-length so the visible black caps draw as horizontal lines at p25 and p75 — the defining ECMWF mediogram cue.

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

### Provenance

Every PNG carries three `tEXt` chunk keys written via matplotlib's
`savefig(metadata=...)`:

- `rhiza_history_forecast` — a JSON-encoded array of `{skill, version,
  args, input}` entries with the same schema used for the zarr
  `rhiza_history` attribute. The chain belongs to the `--forecast`
  input. The last entry records this `plot-mediogram` invocation, with
  its `input` field set to the forecast-side `{basename, hash}`.
  Preceding entries are the upstream chain inherited from the forecast
  zarr's `rhiza_history` (empty if the input had none — a stderr
  warning is emitted and the array contains only the rendering entry).
- `rhiza_history_mclimate` — the same shape for the `--mclimate` input.
  The last entry's `input` is the m-climate-side `{basename, hash}`;
  preceding entries come from the m-climate zarr's upstream chain.
- `Software` — set to `forecasting-skills` so generic image tools like
  `exiftool` surface the producer prominently.

Both `--forecast` and `--mclimate` are recorded as first-class inputs:
a mediogram plots the forecast against the climatology baseline, so
the PNG can only be reproduced from its own provenance when both input
chains are present. Two keys (not one tree-shaped key) because the
forecast and m-climate inputs typically come from independent
upstream branches (e.g. an ecmwf-fetch forecast vs. an m-climate
sample drawn from the historical archive). Two linear chains keep the
on-disk schema identical to the single-input plotters: a consumer
reading either `rhiza_history_forecast` or `rhiza_history_mclimate`
uses one parse path and gets the full lineage of that branch.

Read-back:

```bash
python3 -c "from PIL import Image; import json; img=Image.open('out.png'); print(json.loads(img.info['rhiza_history_forecast'])); print(json.loads(img.info['rhiza_history_mclimate']))"
```

Or:

```bash
exiftool out.png
```

## Example

```bash
uv run scripts/plot_mediogram.py \
    --forecast /tmp/ecmwf_forecast.zarr \
    --mclimate /tmp/ecmwf_mclimate.zarr \
    --lat -1.3 --lon 36.8 \
    --variable tp \
    --output /tmp/mediogram_nairobi.png
```
