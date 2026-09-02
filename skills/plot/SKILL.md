---
name: plot
description: Render a 2D heatmap, filled-contour map, 1D time series, xy scatter, wind-rose, u/v quiver, or layered map PNG from weather-skills standard dataset Zarrs. Overlay multiple inputs with repeatable --layer KIND:PATH (heatmap, scatter, quiver, GeoJSON outline/mask). Heatmaps overlay scale-appropriate coastlines, country borders, lakes, and admin-1 boundaries. Use for a single dataset as a map/profile/rose/vectors, or stacked layers (e.g. precip heatmap + station scatter). --style xy plots one 1D series against another (--x/--y, or -i with --x-variable/--y-variable), pairing on time, year, or index. For precipitation, run aggregate-temporal then convert-to-totals first. For side-by-side two-row comparison, use plot-compare. Use --fontsize to enlarge titles, axis labels, city labels, and colorbar text (default 18).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot

Source-agnostic visualization. Single-input styles (`-i`) plus layered maps
(`--layer`, repeatable):
- `heatmap` — CartoPy `PlateCarree` map with scale-appropriate geographic
  overlays (Natural Earth, fetched and cached via `cartopy`): coastlines,
  country borders, and lake outlines at 10m / 50m / 110m depending on the
  view size, plus admin-1 (states / provinces / counties) on country-scale
  maps (span ≤ 20°). Overlays are clipped to the map extent. If
  the input has a `step` (or `time`) dimension, panels are laid out one per
  step with a shared color scale and a horizontal colorbar spanning all
  panels at the bottom. Panel titles show calendar dates (`YYYY-MM-DD`) or,
  for multi-day bins (from `aggregation_period` or time spacing), inclusive
  ranges (`YYYY-MM-DD to YYYY-MM-DD`); forecast lead panels keep
  `<start> until <end>`. Default layout is up to 4 columns (rows added as
  needed). `--rows` and/or `--columns` override that and must pack the
  panel count exactly (`rows × columns` equals the number of steps/times;
  leftover blank cells are not allowed). Ensemble members
  (`number` dim) are averaged before plotting. Use `--index` to override the
  default reduction for any other extra dim. Precipitation totals default to
  the CHIRPS-GEFS total-rainfall classes (white `<2` through green→blue→
  purple→yellow→red→pink at 2, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500,
  750, 1000, 1500, 2500 mm) when `aggregation_period` is missing or ≥ 5 days.
  Sub-pentad totals (`aggregation_period` < 5 days) use the same colors with
  lower breaks (0.5 … 200 mm). Precipitation anomalies (negative values, or
  `anomal` in the variable / long name — e.g. after `difference`) use the
  CHIRPS-GEFS diverging classes (brown/red dry ↔ white ↔ green/blue wet at
  ±10, 25, 50, 100, 200, 300, 500 mm). Other variables default to `viridis`.
- `contour` — the same map layout as `heatmap` (panels, shared color scale,
  colorbar, geo overlays, `--bbox` / `--mask-geojson` / `--extent` /
  `--cities` / `--index` / `--draw-box` / `--rows` / `--columns`), but filled
  isolines (`contourf`) plus thin black contour lines. Values are interpolated
  between grid points rather than drawn as cell rectangles. Cannot mix with
  `--layer`.
- `timeseries` — 1D profile. Averages across all non-time dims. Line plus a
  marker at each time point. A forecast cube (`step` lead times + scalar init
  `time`) is plotted against **valid time** (`init + step`) with calendar dates
  on the x-axis, not raw lead-time nanoseconds. An analysis / obs cube with a
  `time` dim is plotted against that axis as-is.
- `xy` — scatter one 1D series against another. Pass `--x` and `--y` Zarrs
  (or one `-i` with `--x-variable` and `--y-variable`). Each input is reduced
  the same way as `timeseries` (mean over non-time dims; `--bbox` /
  `--mask-geojson` subset first when lat/lon remain). `--pair-on time`
  (default) inner-joins on the time/valid-time coord; `year` joins on
  calendar year (September IOD vs October rain); `index` pairs by position
  (same length required). Duplicate keys are an error — aggregate or select
  first. Points are labeled with the pair key when `--pair-on year`, or when
  `--pair-on time` and there are ≤ 25 points. Distinct from `--layer scatter`,
  which plots stations on a map.
- `windrose` — one polar rose of meteorological-from wind direction (0° = N,
  90° = E, clockwise) stacked by speed. Converts eastward (`u`) and northward
  (`v`) components; auto-detects `u10`/`v10`, CF `eastward_wind` /
  `northward_wind`, and other common pairs, or pass `--u-variable` /
  `--v-variable`. Flattens remaining space, time/step, and ensemble dims into
  samples (does **not** average the ensemble — a frequency rose needs the
  members). `--bbox` / `--mask-geojson` / `--index` subset samples first.
  16 compass sectors; speed classes 0–2, 2–4, …, ≥12 m/s (empty high-speed
  bins dropped). `--colormap` colors the speed stacks (default blue→orange).
- `quiver` — wind-vector map: speed as `pcolormesh` (`YlGn` by default,
  matching `plot_s2s` 10 m / 700 hPa `10m-wind_vectors.png`) with native-grid
  `u`/`v` arrows like `plot_wind_and_sst_anomaly` (optional `--quiver-step`
  stride; no cartopy `regrid_shape`). Arrow length is auto-scaled so a
  typical wind is about 1.5× the subsampled grid spacing — a fixed matplotlib
  `scale=100` matches S2S *anomaly* magnitudes and overdraws 10 m/s basin
  winds. Same panel layout, geo overlays, `--bbox` / `--mask-geojson` /
  `--index` / `--cities` / `--draw-box` as heatmap. Ensemble `number` is
  averaged. Auto-detects u/v like windrose (`u10`/`v10`, CF `eastward_wind` /
  `northward_wind`, …). Colorbar is `Wind speed [m/s]` (or `Wind speed
  anomaly` if the u field name says so). Arrow keys for 5 and 10 m/s. Finer
  grids (GFS 0.25°) auto-thin to ~1.5° (native S2S spacing) unless
  `--quiver-step` is set.

Layered maps (`--layer KIND:PATH`, repeatable) draw several inputs on the
**same** axes. `-i` is the single-input shorthand and cannot mix with `--layer`.
Kinds: `heatmap` (gridded Zarr), `scatter` (`station_id` / `point_id` Zarr),
`quiver` (gridded u/v arrows; speed mesh only if there is no heatmap layer),
`outline` (GeoJSON edges), `mask` (GeoJSON NaN mask, same as `--mask-geojson`).
Optional `::k=v` suffix: `variable`, `colormap`, `index`, `u-variable`,
`v-variable`, `quiver-scale`, `quiver-step`. Figure-level `--variable` /
`--colormap` / `--index` are defaults a layer inherits. A forecast `step` axis
still panels one map per lead; static layers (outline, cities, a single-time
field) repeat on every panel. Another data layer on the same axis kind is
intersected on labels. Overlaying calendar `time` on a raw `step` forecast is
an error — run `step-to-time` first. Same-variable heatmap+scatter layers share
one color scale unless `--independent-scale`.

## When to use

- Overlaying stations or a GeoJSON outline on a forecast/obs heatmap
  (`--layer heatmap:… --layer scatter:…`).
- Producing a quick-look forecast map panel for any gridded dataset.
- Producing a time/step profile for a gridded or station standard dataset.
- Scattering one index or field against another (IOD vs rainfall, two
  variables in one Zarr).
- Producing a wind rose from u/v (or eastward/northward) components.
- Producing S2S-style wind-vector maps (speed + quiver) from u/v.
- Precipitation: only after `aggregate-temporal` and `convert-to-totals`.
  Fetchers write rates; figures should show period totals (`mm`).
- If the PNG looks empty or wrong, run `inspect-figure` on it (then
  `inspect-zarr` on the input Zarr) before regenerating.

For two-dataset **side-by-side** (two-row) comparison, use `plot-compare`.
To overlay stations on a heatmap, use `--layer` here instead. For N gridded
datasets as a valid-time grid with blank cells where a dataset has no time,
use `plot-compare-forecasts`. For one obs week versus week-4 through week-1 forecasts
with a hits row, use `plot-verify`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--style heatmap|contour|timeseries|xy|windrose|quiver] \
    [--u-variable NAME] [--v-variable NAME] [--quiver-scale N] [--quiver-step N] \
    [--colormap NAME] [--title TEXT] [--xlabel TEXT] [--ylabel TEXT] \
    [--index DIM=POS,...] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--bbox N/W/S/E] \
    [--mask-geojson PATH] [--draw-box N/W/S/E ...] \
    [--rows N] [--columns N]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --output <out.png> \
    --layer heatmap:<a.zarr>[::variable=NAME] \
    [--layer scatter:<b.zarr>] [--layer outline:<c.geojson>] \
    [--layer quiver:<wind.zarr>] [--shared-scale | --independent-scale]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --style xy --output <out.png> \
    --x <x.zarr> --y <y.zarr> [--x-variable NAME] [--y-variable NAME] \
    [--pair-on time|year|index] [--bbox N/W/S/E]
```

### Arguments
- `--input`, `-i` — Zarr input (single-dataset mode). Mutually exclusive with `--layer`
  and with `--x` / `--y`. For `--style xy`, a single `-i` requires both
  `--x-variable` and `--y-variable`.
- `--x` / `--y` — X- and Y-axis Zarrs for `--style xy`. Mutually exclusive
  with `-i` and `--layer`.
- `--x-variable` / `--y-variable` — variables for `--style xy`. Default: first
  data variable of `--x` / `--y` (or of `-i` when both flags are set).
- `--pair-on` — `time` (default), `year`, or `index`. `--style xy` only.
- `--layer` — repeatable map layer `KIND:PATH` or `KIND:PATH::k=v`. Kinds:
  `heatmap`, `scatter`, `quiver`, `outline`, `mask`. Cannot mix with `-i` or
  with `--style timeseries|xy|contour|windrose|quiver`.
- `--label` — colorbar label for each `--layer`, in order. When omitted,
  heatmap/scatter/quiver layers infer a short product name from provenance;
  outline/mask layers ignore it.
- `--shared-scale` / `--independent-scale` — layered heatmap/scatter color
  scales. Default: share when the layers resolve to the same variable and
  matching units.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
  Ignored for `--style xy` (use `--x-variable` / `--y-variable`) and for
  `--style windrose` and `--style quiver` (use `--u-variable` / `--v-variable`).
- `--style` — `heatmap` (default), `contour`, `timeseries`, `xy`, `windrose`, or
  `quiver`. `contour` is the heatmap layout with filled isolines instead of
  grid cells. Timeseries of a forecast (`step` + scalar init) uses valid times
  on the x-axis. `xy` is a 1D-vs-1D scatter (`--x`/`--y` or one `-i`); see
  `--pair-on`. Windrose converts u/v to meteorological-from direction (the
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
  totals (rate or amount) use the CHIRPS-GEFS total-rainfall classes
  (`BoundaryNorm` over
  `[2, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2500]`
  mm with white under `<2` and pale-pink over `>2500`) when
  `aggregation_period` is missing or ≥ 5 days. Sub-pentad totals
  (`aggregation_period` < 5 days) keep the same colors with lower breaks
  (`[0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 75, 100, 150, 200]` mm).
  Precipitation anomalies (negatives, or `anomal` in the name — e.g. after
  `difference`) use the CHIRPS-GEFS diverging classes
  (`[-500, -300, -200, -100, -50, -25, -10, 10, 25, 50, 100, 200, 300, 500]`
  mm with under/over colors). Every other variable uses `viridis`. Windrose uses a blue→orange
  speed palette; `--colormap` recolors the speed stacks. Quiver defaults to
  `YlGn` (S2S 10 m / 700 hPa wind-vector maps); `--colormap PiYG` matches their
  anomaly quivers. A variable with CF `flag_values` (e.g. `verify --metric hits`) uses a
  discrete colormap and labeled colorbar ticks; `--colormap` as comma-separated
  colors must then match the flag count.
- `--title` — optional plot title.
- `--xlabel` / `--ylabel` — optional axis-label overrides. When omitted, maps
  use `Longitude` / `Latitude`, timeseries uses `Valid time` (or the time
  dim) / the variable label, and `xy` uses each series' variable label.
  Passed text is used as-is (not re-cased).
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
  (`step=2,0` renders position 2 first). Heatmap, quiver, windrose, and `xy`
  apply the spec; `--style timeseries` syntax-checks it, then ignores it with
  a stderr warning. Windrose flattens remaining positions into samples (a list
  like `step=0,1,2` keeps those steps in one rose, rather than panelling).
  `xy` reduces leftover dims after `--index` the same way as timeseries.
- `--extent` — heatmap/quiver map extent as `lon_min,lon_max,lat_min,lat_max`.
  Defaults to the data's cell-center min/max expanded by half the mean
  grid spacing on each side, so the view matches what `pcolormesh`
  actually draws (it treats coords as cell centers and extends ±½
  spacing).
- `--cities` — heatmap/quiver city overlay. Inline JSON like
  `'{"Windhoek": [-22.55, 17.08]}'` or a path to such a JSON file. Off by
  default.
- `--fontsize` — base font size for titles (including panel date labels), axis
  labels, city labels, and colorbar text (default 18). Raise on user request
  (e.g. `--fontsize 22`).
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
  over the bbox-derived extent. Heatmap, quiver, windrose, and `xy` use it;
  `--style timeseries` ignores `--bbox` with a stderr warning. Default unset
  → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon (e.g. the
  `--geojson` output of the `resolve-region` skill). Gridded cells whose centers
  fall outside the polygon are set to NaN before plotting, so the heatmap shows
  the country shape rather than its bounding rectangle. All features in the file
  are unioned. Combine with `--bbox` to crop to the rectangle first,
  then mask to the polygon within it. Heatmap, quiver, windrose, and `xy` use
  it; `--style timeseries` ignores it with a stderr warning. Default unset
  → no mask.
- `--draw-box` — optional black outline rectangle(s) drawn on each map panel.
  Same `N/W/S/E` form as `--bbox`. Repeat the flag for multiple boxes (e.g.
  IOD west `10/50/-10/70` and east `0/90/-10/110`). Unlike `--bbox`, this does
  **not** crop the data — it only overlays outlines. Antimeridian spans
  (`W > E`) are drawn as two segments. Heatmap and quiver — `--style
  timeseries` and `--style windrose` ignore it with a stderr warning. Default
  unset → no boxes.
- `--quiver-scale` — matplotlib quiver `scale` for `--style quiver`. Larger
  values draw shorter arrows. When omitted, the skill sizes a typical
  (95th-percentile) wind to about 1.5× the subsampled grid spacing as a
  fraction of the map width, so 10 m/s basin winds stay readable. Pass `100`
  to match `plot_wind_and_sst_anomaly` (that default was tuned for small
  *anomaly* vectors, not full 10 m wind).
- `--quiver-step` — plot every Nth grid point for `--style quiver`. Matches
  `plot_wind_and_sst_anomaly`'s `quiver_step`. When omitted, the skill uses
  stride 1 on ~1.5° (S2S) grids and auto-thins finer grids to about 1.5° so
  a GFS basin map looks like the S2S Indian Ocean wind overlay. Pass `1` to
  plot every native point.

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

Multi-step forecast panel (precip uses the CHIRPS-GEFS totals palette by default):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --style heatmap --title "S2S precip"
```

Filled-contour map of the same field:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_contour.png \
    --variable tp --style contour --title "S2S precip"
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

Precip heatmap with station scatter and a country outline on the same axes:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -o /tmp/imerg_vs_tahmo.png \
    --layer heatmap:/tmp/imerg.zarr::variable=precip \
    --layer scatter:/tmp/tahmo.zarr::variable=precip \
    --layer outline:/tmp/kenya.geojson \
    --title "IMERG vs TAHMO"
```

Time series:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts.png \
    --variable tp --style timeseries
```

XY scatter (September IOD vs October rainfall, one point per year):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --style xy \
  --x /tmp/iod_sep.zarr --y /tmp/rain_oct.zarr \
  --pair-on year \
  --xlabel "September IOD" --ylabel "October rainfall" \
  -o /tmp/iod_vs_rain.png
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
