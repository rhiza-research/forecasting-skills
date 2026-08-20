---
name: plot-mediogram
description: Render an ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate (historical) ensemble at a single lat/lon. Two-layer boxplots per time step show an extremes box underneath (p0–p100 whiskers, p10–p90 box, p50 median) with a wider p25–p75 IQR box overlaid on top, whose visible black caps mark the IQR edges. For precipitation, run convert-to-totals after aggregate-temporal before plotting.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py *)
metadata:
  catalog-group: figure
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
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py -i <forecast.zarr> -i <mclimate.zarr> \
    --lat <lat> --lon <lon> --output <out.png> \
    [--variable NAME] [--title TEXT]
```

### Arguments
- `--input`, `-i` — pass exactly twice: forecast Zarr first, m-climate Zarr second.
- `--lat`, `--lon` — point location (nearest-neighbor selection).
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable in the forecast input.
- `--title` — optional plot title.

### Output

A PNG at `--output`, single axes, figsize `(10, 5)`, up to 6 forecast steps on the x-axis labeled with actual leads (`+7d`, `+10d`, …).

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; img=Image.open('out.png'); print(json.loads(img.info['weather_skills_history']))"
```

Or:

```bash
exiftool out.png
```

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py \
    -i /tmp/ecmwf_forecast.zarr \
    -i /tmp/ecmwf_mclimate.zarr \
    --lat -1.3 --lon 36.8 \
    --variable tp \
    --output /tmp/mediogram_nairobi.png
```
