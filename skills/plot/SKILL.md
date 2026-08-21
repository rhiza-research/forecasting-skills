---
name: plot
description: Render a 2D heatmap or 1D time series PNG from any gridded or station weather-skills standard dataset Zarr. Heatmaps overlay scale-appropriate coastlines, country borders, lakes, and admin-1 boundaries. Use when you need to visualize a single dataset as a map or as a time/step profile. For precipitation, run aggregate-temporal then convert-to-totals first — plot period totals (`mm`), not fetch rates (`mm day-1`).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  catalog-group: figure
---

# plot

Source-agnostic single-dataset visualization. Two styles:
- `heatmap` — CartoPy `PlateCarree` map with scale-appropriate geographic
  overlays (Natural Earth, fetched and cached via `cartopy`): coastlines,
  country borders, and lake outlines at 10m / 50m / 110m depending on the
  view size, plus admin-1 (states / provinces / counties) on country-to-
  regional maps (span ≤ 45°). Overlays are clipped to the map extent.
  If `--mask-geojson` is set, that polygon is also outlined on top. If
  the input has a `step` (or `time`) dimension, panels are laid out one per
  step (up to 4 columns; rows added as needed) with a shared color scale and a
  horizontal colorbar spanning all panels at the bottom. Ensemble members
  (`number` dim) are averaged before plotting. Use `--index` to override the
  default reduction for any other extra dim. Precipitation variables default
  to the Kenya / ECMWF-S2S product palette (white–wheat–green–blue–yellow–
  orange–red–purple), matching `kenya-forecast-png` weekly/dekadal precip
  maps; other variables default to `viridis`.
- `timeseries` — 1D profile. Averages across all non-time dims. A forecast
  cube (`step` lead times + scalar init `time`) is plotted against **valid
  time** (`init + step`) with calendar dates on the x-axis, not raw lead-time
  nanoseconds. An analysis / obs cube with a `time` dim is plotted against
  that axis as-is.

## When to use

- Producing a quick-look forecast map panel for any gridded dataset.
- Producing a time/step profile for a gridded or station standard dataset.
- Precipitation: only after `aggregate-temporal` and `convert-to-totals`.
  Fetchers write rates; figures should show period totals (`mm`).

For two-dataset comparisons, use the `plot-compare` skill. For N gridded
datasets as a valid-time grid with blank cells where a dataset has no time,
use `plot-compare-forecasts`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|timeseries] \
    [--colormap NAME] [--title TEXT] [--index DIM=POS,...] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--bbox N/W/S/E] \
    [--mask-geojson PATH] [--draw-box N/W/S/E ...]
```

### Arguments
- `--input`, `-i` — Zarr input.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
- `--style` — `heatmap` (default) or `timeseries`. Timeseries of a forecast
  (`step` + scalar init) uses valid times on the x-axis.
- `--colormap` — either a matplotlib colormap name or a comma-separated
  list of colors to interpolate between (e.g. `white,wheat,green`). Named
  matplotlib colormaps cannot contain commas, so the presence of a comma
  unambiguously selects the custom-list form. When omitted, precipitation
  (rate or amount) uses the Kenya / ECMWF-S2S product palette
  (`white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple`);
  every other variable uses `viridis`.
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
  rectangular slice (geographic overlays are decoration, not a mask). To
  restrict to a country, get its bbox from the `resolve-region` skill.
  Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so
  global grids still intersect negative-lon bboxes. `--extent` (if passed) wins
  over the bbox-derived extent. Heatmap-only — `--style timeseries` ignores
  `--bbox` with a stderr warning. Default unset → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon (e.g. the
  `--geojson` output of the `resolve-region` skill). Gridded cells whose centers
  fall outside the polygon are set to NaN before plotting, so the heatmap shows
  the country shape rather than its bounding rectangle. All features in the file
  are unioned. The same polygon is outlined on top of the scale-appropriate
  Natural Earth overlays. Combine with `--bbox` to crop to the rectangle first,
  then mask to the polygon within it. Heatmap-only — `--style timeseries`
  ignores it with a stderr warning. Default unset → no mask.
- `--draw-box` — optional black outline rectangle(s) drawn on each heatmap panel.
  Same `N/W/S/E` form as `--bbox`. Repeat the flag for multiple boxes (e.g.
  IOD west `10/50/-10/70` and east `0/90/-10/110`). Unlike `--bbox`, this does
  **not** crop the data — it only overlays outlines. Antimeridian spans
  (`W > E`) are drawn as two segments. Heatmap-only — `--style timeseries`
  ignores it with a stderr warning. Default unset → no boxes.

### Output

A PNG at `--output`. The colorbar label resolves from variable attrs:
`GRIB_name` → `long_name` → bare variable name → `"value"`, suffixed
with `[units]` when the `units` attr is present. Units on the figure are a
short display form (`mm/day`, `°C`, `mm`), not the on-disk CF string.
Prefer an amount Zarr from
`convert-to-totals` (labeled `Total precipitation [mm]`). If the input is
still a precip **rate** with `aggregation_period`, plot converts it to a
period total for the figure only. Unaggregated fetch rates stay `mm day-1`.

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

Multi-step forecast panel (precip uses the Kenya/S2S palette by default):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --title "S2S precip"
```

Override the palette (e.g. magma):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --colormap magma --title "S2S precip"
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

Indian Ocean map with IOD west/east dipole boxes overlaid:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ts_anom.zarr -o /tmp/iod_boxes.png \
    --variable ts_anomaly --extent 40,120,-20,20 \
    --draw-box 10/50/-10/70 --draw-box 0/90/-10/110
```

Time series:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts.png \
    --variable tp --style timeseries
```
