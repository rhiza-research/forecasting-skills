---
name: plot
description: Render a 2D heatmap or 1D time series PNG from any gridded or station Rhiza Envelope Zarr. Use when you need to visualize a single dataset as a map or as a time/step profile.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot

Source-agnostic single-dataset visualization. Two styles:
- `heatmap` — CartoPy `PlateCarree` map with country/coastline boundaries. If
  the input has a `step` (or `time`) dimension, panels are laid out one per
  step (up to 4 columns; rows added as needed) with a shared color scale and a
  horizontal colorbar spanning all panels at the bottom. Ensemble members
  (`number` dim) are averaged before plotting. Use `--index` to override the
  default reduction for any other extra dim.
- `timeseries` — 1D profile. Averages across all non-time dims.

## When to use

- Producing a quick-look forecast map panel for any gridded envelope.
- Producing a time/step profile for a gridded or station envelope.

For two-dataset comparisons, use the `plot-compare` skill.

## Usage

```
uv run scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|timeseries] \
    [--colormap NAME] [--title TEXT] [--index DIM=POS,...] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--region NAME]
```

### Arguments
- `--input`, `-i` — Zarr input.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
- `--style` — `heatmap` (default) or `timeseries`.
- `--colormap` — either a matplotlib colormap name (default `viridis`) or a
  comma-separated list of colors to interpolate between (e.g.
  `white,wheat,green`). Named matplotlib colormaps cannot contain commas, so
  the presence of a comma unambiguously selects the custom-list form.
- `--title` — optional plot title.
- `--index` — dim selections like `step=3,number=0`. For `heatmap`, applied
  before panel layout: e.g. `--index step=2` reduces to a single-panel map at
  step 2; otherwise all steps are panelled.
- `--extent` — heatmap map extent as `lon_min,lon_max,lat_min,lat_max`.
  Defaults to the data's cell-center min/max expanded by half the mean
  grid spacing on each side, so the view matches what `pcolormesh`
  actually draws (it treats coords as cell centers and extends ±½
  spacing).
- `--cities` — heatmap city overlay. Inline JSON like
  `'{"Windhoek": [-22.55, 17.08]}'` or a path to such a JSON file. Off by
  default.
- `--fontsize` — base font size for titles/colorbar label (default 16).
- `--region` — optional named region. Slices the gridded input to the
  region's (N, W, S, E) bbox using `da.sel(...)` and sets the heatmap
  extent to that bbox. Cells inside the bbox but outside the country
  polygon are kept — this is a rectangular slice, matching the upstream
  `ECMWF-S2S4AFRICA` convention (admin boundaries are drawn as decoration
  by `cfeature.BORDERS`/`cfeature.COASTLINE`, not used as a mask).
  Accepted values mirror `clip-region`'s `REGIONS` dict
  (`africa`, `kenya`, `ghana`, `senegal`, `ethiopia`, `namibia`, `botswana`,
  `zambia`, `madagascar`, `angola`). Longitudes in `[0, 360]` are
  auto-wrapped to `[-180, 180]` before slicing so global grids still
  intersect negative-lon regions. `--extent` (if passed) wins over the
  bbox-derived extent. Heatmap-only — `--style timeseries` ignores
  `--region` with a stderr warning. Default unset → no slice, identical
  behavior to pre-flag rendering.

### Output

A PNG at `--output`. The colorbar label resolves from variable attrs:
`long_name` → `GRIB_name` → bare variable name → `"value"`, suffixed
with `[units]` when the `units` attr is present.

### Provenance

Every PNG carries two `tEXt` chunk keys written via matplotlib's
`savefig(metadata=...)`:

- `rhiza_history` — a JSON-encoded array of `{skill, version, args,
  input}` entries with the same schema used for the zarr `rhiza_history`
  attribute. Each entry records one pipeline step. The last entry is
  this `plot` invocation; preceding entries are the upstream chain
  inherited from the input zarr's `rhiza_history` (empty array if the
  input had none — a stderr warning is emitted in that case and the
  array contains only the `plot` entry).
- `Software` — set to `forecasting-skills` so generic image tools like
  `exiftool` surface the producer prominently.

Read-back:

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['rhiza_history']))"
```

Or:

```bash
exiftool out.png
```

## Examples

Multi-step forecast panel:
```bash
uv run scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --colormap magma --title "S2S precip"
```

Multi-step forecast panel with a custom precipitation palette:
```bash
uv run scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap \
    --colormap white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple
```

Single-step map with cities and an explicit extent:
```bash
uv run scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_step0.png \
    --variable tp --index step=0 \
    --extent 11,29,-30,-15 \
    --cities '{"Windhoek": [-22.55, 17.08]}'
```

Time series:
```bash
uv run scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts.png \
    --variable tp --style timeseries
```
