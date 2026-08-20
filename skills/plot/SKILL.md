---
name: plot
description: Render a 2D heatmap or 1D time series PNG from any gridded or station weather-skills standard dataset Zarr. Use when you need to visualize a single dataset as a map or as a time/step profile.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  catalog-group: figure
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

- Producing a quick-look forecast map panel for any gridded dataset.
- Producing a time/step profile for a gridded or station standard dataset.

For two-dataset comparisons, use the `plot-compare` skill.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|timeseries] \
    [--colormap NAME] [--title TEXT] [--index DIM=POS,...] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--bbox N/W/S/E] \
    [--mask-geojson PATH]
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
- `--index` — dim selections like `step=3,number=0`. A dim may take several
  comma-separated positions, e.g. `step=0,1,2`, which keeps the dim with just
  those positions. Negative positions are accepted and count from the end,
  Python-style (`step=-1` is the last step). Repeating a dim is an error, as
  are positions that address the same element — including negative aliases
  (`step=0,-3` on a 3-step axis). List selections are only supported on the
  panel (step/time) dimension; other dims take a single position. Applied
  before panel layout: e.g. `--index step=2` reduces to a single-panel map at
  step 2, while `--index step=0,1,2` panels exactly those three steps;
  otherwise all steps are paneled. Panels follow the order given in the spec
  (`step=2,0` renders position 2 first). Heatmap-only — with `--style
  timeseries` the spec is syntax-checked, then ignored with a stderr warning.
- `--extent` — heatmap map extent as `lon_min,lon_max,lat_min,lat_max`.
  Defaults to the data's cell-center min/max expanded by half the mean
  grid spacing on each side, so the view matches what `pcolormesh`
  actually draws (it treats coords as cell centers and extends ±½
  spacing).
- `--cities` — heatmap city overlay. Inline JSON like
  `'{"Windhoek": [-22.55, 17.08]}'` or a path to such a JSON file. Off by
  default.
- `--fontsize` — base font size for titles/colorbar label (default 16).
- `--bbox` — optional `N/W/S/E` decimal degrees. Slices the gridded input to the
  bbox using `da.sel(...)` and sets the heatmap extent to that bbox. This is a
  rectangular slice (admin boundaries are drawn as decoration by
  `cfeature.BORDERS`/`cfeature.COASTLINE`, not used as a mask). To restrict to a
  country, get its bbox from the `resolve-region` skill. Longitudes in
  `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so global grids
  still intersect negative-lon bboxes. `--extent` (if passed) wins over the
  bbox-derived extent. Heatmap-only — `--style timeseries` ignores `--bbox` with
  a stderr warning. Default unset → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon (e.g. the
  `--geojson` output of the `resolve-region` skill). Gridded cells whose centers
  fall outside the polygon are set to NaN before plotting, so the heatmap shows
  the country shape rather than its bounding rectangle. All features in the file
  are unioned. Combine with `--bbox` to crop to the rectangle first, then mask to
  the polygon within it. Heatmap-only — `--style timeseries` ignores it with a
  stderr warning. Default unset → no mask.

### Output

A PNG at `--output`. The colorbar label resolves from variable attrs:
`long_name` → `GRIB_name` → bare variable name → `"value"`, suffixed
with `[units]` when the `units` attr is present.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata (same schema as Zarr provenance). Read-back:

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['weather_skills_history']))"
```

Or:

```bash
exiftool out.png
```

## Examples

Multi-step forecast panel:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --colormap magma --title "S2S precip"
```

Multi-step forecast panel with a custom precipitation palette:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap \
    --colormap white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple
```

Single-step map with cities and an explicit extent:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_step0.png \
    --variable tp --index step=0 \
    --extent 11,29,-30,-15 \
    --cities '{"Windhoek": [-22.55, 17.08]}'
```

Country-shaped map masked to a boundary polygon:
```bash
# After resolve-region writes --geojson /tmp/kenya.geojson (dummy bbox below):
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/chirps_kenya.zarr -o /tmp/kenya.png \
    --variable precip --bbox 5/34/-5/42 --mask-geojson /tmp/kenya.geojson
```

Time series:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts.png \
    --variable tp --style timeseries
```
