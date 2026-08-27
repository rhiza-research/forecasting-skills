---
name: plot
description: Render a 2D heatmap, 1D time series, wind-rose, or u/v quiver PNG from any gridded or station weather-skills standard dataset Zarr. Heatmaps overlay scale-appropriate coastlines, country borders, lakes, and admin-1 boundaries. Wind roses convert eastward/northward (u/v) components to meteorological-from direction and speed. Quiver maps match ECMWF S2S Africa 10 m / 700 hPa wind-vector panels (YlGn speed field, regridded arrows). Use when you need to visualize a single dataset as a map, a time/step profile, a wind rose, or wind vectors. For precipitation, run aggregate-temporal then convert-to-totals first — plot period totals (`mm`), not fetch rates (`mm day-1`).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  catalog-group: figure
---

# plot

Source-agnostic single-dataset visualization. Four styles:
- `heatmap` — CartoPy `PlateCarree` map with scale-appropriate geographic
  overlays (Natural Earth, fetched and cached via `cartopy`): coastlines,
  country borders, and lake outlines at 10m / 50m / 110m depending on the
  view size, plus admin-1 (states / provinces / counties) on country-to-
  regional maps (span ≤ 45°). Overlays are clipped to the map extent. If
  the input has a `step` (or `time`) dimension, panels are laid out one per
  step with a shared color scale and a horizontal colorbar spanning all
  panels at the bottom. Default layout is up to 4 columns (rows added as
  needed). `--rows` and/or `--columns` override that and must pack the
  panel count exactly (`rows × columns` equals the number of steps/times;
  leftover blank cells are not allowed). Ensemble members
  (`number` dim) are averaged before plotting. Use `--index` to override the
  default reduction for any other extra dim. Precipitation variables default
  to the discrete Kenya / ECMWF-S2S product classes (white–wheat–green–blue–
  yellow–orange–red–purple at 0, 10, 20, 40, 60, 80, 110, 150, 200, 250,
  350 mm), matching `kenya-forecast-png` weekly/dekadal precip maps; other
  variables default to `viridis`.
- `timeseries` — 1D profile. Averages across all non-time dims. Line plus a
  marker at each time point. A forecast cube (`step` lead times + scalar init
  `time`) is plotted against **valid time** (`init + step`) with calendar dates
  on the x-axis, not raw lead-time nanoseconds. An analysis / obs cube with a
  `time` dim is plotted against that axis as-is.
- `windrose` — one polar rose of meteorological-from wind direction (0° = N,
  90° = E, clockwise) stacked by speed. Converts eastward (`u`) and northward
  (`v`) components; auto-detects `u10`/`v10`, CF `eastward_wind` /
  `northward_wind`, and other common pairs, or pass `--u-variable` /
  `--v-variable`. Flattens remaining space, time/step, and ensemble dims into
  samples (does **not** average the ensemble — a frequency rose needs the
  members). `--bbox` / `--mask-geojson` / `--index` subset samples first.
  16 compass sectors; speed classes 0–2, 2–4, …, ≥12 m/s (empty high-speed
  bins dropped). `--colormap` colors the speed stacks (default blue→orange).
- `quiver` — ECMWF S2S Africa `quiver_plot_variable` wind-vector map: speed as
  `pcolormesh` (`YlGn` by default, matching `plot_s2s` 10 m / 700 hPa
  `10m-wind_vectors.png`) with `u`/`v` arrows regridded to a 10×10 projection
  grid (`regrid_shape=10`, matplotlib `scale=40`). Same panel layout, geo
  overlays, `--bbox` / `--mask-geojson` / `--index` / `--cities` / `--draw-box`
  as heatmap. Ensemble `number` is averaged. Auto-detects u/v like windrose
  (`u10`/`v10`, CF `eastward_wind`/`northward_wind`, …). Colorbar is
  `Wind speed [m/s]` (or `Wind speed anomaly` if the u field name says so).
  Arrow keys for 5 and 10 m/s. `--quiver-scale 20` matches their anomaly maps.

## When to use

- Producing a quick-look forecast map panel for any gridded dataset.
- Producing a time/step profile for a gridded or station standard dataset.
- Producing a wind rose from u/v (or eastward/northward) components.
- Producing S2S-style wind-vector maps (speed + quiver) from u/v.
- Precipitation: only after `aggregate-temporal` and `convert-to-totals`.
  Fetchers write rates; figures should show period totals (`mm`).
- If the PNG looks empty or wrong, run `inspect-figure` on it (then
  `inspect-zarr` on the input Zarr) before regenerating.

For two-dataset comparisons, use the `plot-compare` skill. For N gridded
datasets as a valid-time grid with blank cells where a dataset has no time,
use `plot-compare-forecasts`. For one obs week versus week-4 through week-1 forecasts
with a hits row, use `plot-verify`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|timeseries|windrose|quiver] \
    [--u-variable NAME] [--v-variable NAME] [--quiver-scale N] \
    [--colormap NAME] [--title TEXT] [--index DIM=POS,...] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--bbox N/W/S/E] \
    [--mask-geojson PATH] [--draw-box N/W/S/E ...] \
    [--rows N] [--columns N]
```

### Arguments
- `--input`, `-i` — Zarr input.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
  Ignored for `--style windrose` and `--style quiver` (use `--u-variable` /
  `--v-variable`).
- `--style` — `heatmap` (default), `timeseries`, `windrose`, or `quiver`.
  Timeseries of a forecast (`step` + scalar init) uses valid times on the
  x-axis. Windrose converts u/v to meteorological-from direction (the
  direction the wind blows **from**) and speed, then histograms every remaining
  sample. Quiver is the S2S wind-vector map (speed field + arrows).
- `--u-variable` / `--v-variable` — eastward and northward wind variables for
  `--style windrose` and `--style quiver`. When omitted, the skill auto-detects
  a pair from CF `standard_name` (`eastward_wind` / `northward_wind`) or common
  names (`u10`/`v10`, `10m_u_component_of_wind`/`10m_v_component_of_wind`,
  `u`/`v`, …). Passing only one infers its partner (`u10` → `v10`).
  Heatmap/timeseries ignore these with a stderr warning.
- `--colormap` — either a matplotlib colormap name or a comma-separated
  list of colors to interpolate between (e.g. `white,wheat,green`). Named
  matplotlib colormaps cannot contain commas, so the presence of a comma
  unambiguously selects the custom-list form. When omitted, precipitation
  (rate or amount) uses the discrete Kenya / ECMWF-S2S product classes
  (`white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple`
  with `BoundaryNorm` over `[0, 10, 20, 40, 60, 80, 110, 150, 200, 250,
  350]` mm); every other variable uses `viridis`. Windrose uses a blue→orange
  speed palette; `--colormap` recolors the speed stacks. Quiver defaults to
  `YlGn` (S2S 10 m / 700 hPa wind-vector maps); `--colormap PiYG` matches their
  anomaly quivers. A variable with CF `flag_values` (e.g. `event-hits`) uses a
  discrete colormap and labeled colorbar ticks; `--colormap` as comma-separated
  colors must then match the flag count.
- `--title` — optional plot title.
- `--index` — dim selections like `step=3,number=0`. A dim may take several
  comma-separated positions, e.g. `step=0,1,2`, which keeps the dim with just
  those positions. Negative positions are accepted and count from the end,
  Python-style (`step=-1` is the last step). Repeating a dim is an error, as
  are positions that address the same element — including negative aliases
  (`step=0,-3` on a 3-step axis). Heatmap list selections are only supported on
  the panel (step/time) dimension; other dims take a single position. Applied
  before panel layout: e.g. `--index step=2` reduces to a single-panel map at
  step 2, while `--index step=0,1,2` panels exactly those three steps;
  otherwise all steps are paneled. Panels follow the order given in the spec
  (`step=2,0` renders position 2 first). Heatmap, quiver, and windrose — with
  `--style timeseries` the spec is syntax-checked, then ignored with a stderr
  warning. Windrose flattens remaining positions into samples (a list like
  `step=0,1,2` keeps those steps in one rose, rather than panelling).
- `--extent` — heatmap/quiver map extent as `lon_min,lon_max,lat_min,lat_max`.
  Defaults to the data's cell-center min/max expanded by half the mean
  grid spacing on each side, so the view matches what `pcolormesh`
  actually draws (it treats coords as cell centers and extends ±½
  spacing).
- `--cities` — heatmap/quiver city overlay. Inline JSON like
  `'{"Windhoek": [-22.55, 17.08]}'` or a path to such a JSON file. Off by
  default.
- `--fontsize` — base font size for titles/colorbar label (default 16).
- `--rows` / `--columns` — heatmap/quiver panel grid. Pass either or both. When
  either is set, the layout must pack the data exactly: both given →
  `rows × columns` must equal the number of panels (steps/times after
  `--index`); only `--columns` → that count must divide the panel count
  (rows = n / columns); only `--rows` → that count must divide (columns
  = n / rows). A mismatch is an error. When both are omitted, the default
  is up to 4 columns with extra rows as needed (blank leftover cells
  allowed). Heatmap and quiver — `--style timeseries` and `--style windrose`
  ignore them with a stderr warning.
- `--bbox` — optional `N/W/S/E` decimal degrees. Slices the gridded input to the
  bbox using `da.sel(...)` and sets the heatmap extent to that bbox. This is a
  rectangular slice (geographic overlays are decoration, not a mask). To
  restrict to a country, get its bbox from the `resolve-region` skill.
  Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so
  global grids still intersect negative-lon bboxes. `--extent` (if passed) wins
  over the bbox-derived extent. Heatmap, quiver, and windrose — `--style
  timeseries` ignores `--bbox` with a stderr warning. Default unset → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon (e.g. the
  `--geojson` output of the `resolve-region` skill). Gridded cells whose centers
  fall outside the polygon are set to NaN before plotting, so the heatmap shows
  the country shape rather than its bounding rectangle. All features in the file
  are unioned. Combine with `--bbox` to crop to the rectangle first,
  then mask to the polygon within it. Heatmap, quiver, and windrose — `--style
  timeseries` ignores it with a stderr warning. Default unset → no mask.
- `--draw-box` — optional black outline rectangle(s) drawn on each map panel.
  Same `N/W/S/E` form as `--bbox`. Repeat the flag for multiple boxes (e.g.
  IOD west `10/50/-10/70` and east `0/90/-10/110`). Unlike `--bbox`, this does
  **not** crop the data — it only overlays outlines. Antimeridian spans
  (`W > E`) are drawn as two segments. Heatmap and quiver — `--style
  timeseries` and `--style windrose` ignore it with a stderr warning. Default
  unset → no boxes.
- `--quiver-scale` — matplotlib quiver `scale` for `--style quiver`. Default
  `40` (S2S `quiver_plot_variable`). Their anomaly vector maps use `20`.

### Output

A PNG at `--output`. The colorbar (and timeseries y-axis) label resolves
from variable attrs: `long_name` → `GRIB_name` → bare variable name →
`"value"`, suffixed with `[units]` when the `units` attr is present. Units
on the figure are a short display form (`mm/day`, `°C`, `mm`, `m/s`), not the
on-disk CF string. A wind rose labels speed stacks in those display units and
the radial axis as frequency percent. A quiver map colors speed and overlays
u/v arrows; the colorbar is `Wind speed [m/s]`.
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

Six weekly maps in two rows of three (`rows × columns` must equal the panel count):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/weekly.zarr -o /tmp/weekly.png \
    --variable tp --rows 2 --columns 3
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

Wind rose from 10 m u/v (auto-detected `u10`/`v10`):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/era5_wind.zarr -o /tmp/windrose.png \
    --style windrose --bbox 5/34/-5/42 --title "Kenya 10 m wind"
```

S2S-style 10 m wind-vector map (YlGn speed + quiver, one panel per step):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/s2s_10wind.zarr -o /tmp/10m-wind_vectors.png \
    --style quiver --bbox 5/34/-5/42 --title "10 m wind"
```
