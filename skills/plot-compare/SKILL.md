---
name: plot-compare
description: Render a side-by-side multi-panel comparison PNG of two weather-skills standard dataset Zarr stores (gridded-vs-gridded or station-vs-gridded). Use for sat-vs-station validation, model-vs-obs comparison, or cross-source QC. For precipitation, convert-to-totals after aggregate-temporal before plotting.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py *)
metadata:
  catalog-group: figure
---

# plot-compare

Source-agnostic two-dataset visualization. Produces a 2-row figure
with one panel per time slice; row A is one input, row B the other.
Handles:

- Gridded vs. gridded (pcolormesh maps).
- Station (`station_id`-indexed) vs. gridded (scatter over mesh).

When exactly one input is a point_obs Zarr, that input is placed
on the top row to match the canonical "stations vs. satellite" layout.

The two inputs must already be at the same time resolution and are
compared on the time bins they share. `plot-compare` intersects the two
axes' bin labels and renders the last `N` of the COMMON labels, selecting
those same labels from both inputs so panel `i` shows the same time window
for both rows. A reporting-latency offset that drops one input's trailing
bin (e.g. a station whose final week is not yet in) is not an error — it
just yields one fewer common bin. `plot-compare` exits non-zero only when
the two axes are at different resolutions (different median bin width, or
one a calendar `time` axis and the other a forecast `step` axis) or have
no overlapping bins; in either case it asks you to aggregate to a common
resolution first. To compare data captured at different cadences (e.g.
daily station observations against weekly or dekadal gridded
rates), aggregate each input to the same window with the
`aggregate-temporal` skill before comparing, then `convert-to-totals` so
precipitation figures are period amounts (`mm`), not rates.

Each row can draw a different variable: `--variable-a`/`--variable-b`
select per-row, with `--variable` as a both-rows shorthand. This lets
you compare different quantities (e.g. soil moisture vs. precipitation)
on one figure.

The color scale adapts to what is being compared. When both rows resolve
to the same variable and matching units, one shared scale is used (a
categorical precipitation colormap with `BoundaryNorm` by default, so
values are visually comparable across rows). When the rows are different
variables or have differing units, each row gets its own independent
scale, colormap, and labeled colorbar. `--shared-scale` and
`--independent-scale` force either mode. An admin-1 country boundary
overlay (Natural Earth, fetched and cached via `cartopy`) is drawn on
every panel. The polygon overlay is spatially
*clipped* to the gridded input's bbox (`gdf.clip(box(*bbox))`), so
polygons that straddle the bbox edge are truncated at the edge rather
than rendered whole and neighboring regions never extend beyond the
base.

Both rows always share the gridded input's spatial extent so the figure
is centered on the gridded base; station points outside that extent are
clipped by matplotlib.

Panel titles render the time-bin range as `YYYY-MM-DD to YYYY-MM-DD`
with the bin coord interpreted as the inclusive right edge: start =
end − bin_width + 1 day. Matches `aggregate-temporal` and
`deaccumulate`'s right-edge convention so a 10-day dekad ending
`2026-05-09` renders as `2026-04-30 to 2026-05-09` (10 days inclusive).

## When to use

- Validating a satellite product against station observations for a country.
- Comparing two forecasts (e.g. model A vs. model B) on the same axes.

For N gridded datasets as a valid-time grid with blank cells where a
dataset has no matching time, use `plot-compare-forecasts`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py -i <a.zarr> -i <b.zarr> --output <out.png> \
    [--variable NAME] [--variable-a NAME] [--variable-b NAME] \
    [--colormap NAME] [--colormap-a NAME] [--colormap-b NAME] \
    [--shared-scale | --independent-scale] [--title TEXT] \
    [--panels N] [--time-dim DIM] \
    [--bbox N/W/S/E] [--mask-geojson PATH]
```

### Arguments
- `--input`, `-i` — pass exactly twice. The first input is row A, the second is row B. Station-schema is allowed on either.
- `--output`, `-o` — PNG path.
- `--variable`, `-v` — variable for both rows. Per-row `--variable-a`/`-b`
  override it. Each resolved variable must exist in its own input.
- `--variable-a` — variable for row A (overrides `--variable` for that row).
  Default per row: `--variable`, else that input's first real data var
  (CF grid-mapping/CRS container vars such as `latitude_longitude` are
  skipped during auto-pick).
- `--variable-b` — variable for row B (same resolution as `--variable-a`).
- `--colormap` — matplotlib colormap. In shared-scale mode, when omitted
  the categorical precipitation cmap (`["#bdbdbd", "wheat", "lightgreen",
  "green", "lightblue", "blue", "yellow", "orange", "red", "purple"]`)
  with `BoundaryNorm` over `[0, 10, 20, 40, 60, 80, 110, 150, 200, 250,
  350]` mm is used. In independent-scale mode it is the per-row default
  (falling back to `viridis`).
- `--colormap-a` / `--colormap-b` — per-row matplotlib colormap in
  independent-scale mode. Precedence per row: `--colormap-a`/`-b`, then
  `--colormap`, then `viridis`.
- `--shared-scale` / `--independent-scale` — mutually exclusive; force one
  shared color scale across both rows or a per-row scale + colorbar. When
  neither is given, the mode is chosen automatically: shared when both
  rows resolve to the same variable AND matching units, else independent.
- `--title` — figure title.
- `--panels` — number of panels per row (default 3).
- `--time-dim` — override the time axis. Defaults to `time` if present, else `step`.
- `--bbox` — optional `N/W/S/E` decimal degrees. Rectangular clipping:
  gridded inputs get a `ds.sel(...)` slice to the bbox and station inputs
  are filtered to the bbox (no polygon test); axes are set to the bbox.
  To restrict to a country, get its bbox from the `resolve-region` skill. Longitudes in
  `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so global
  grids intersect negative-lon bboxes. Default unset → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon. Gridded
  inputs get a `shapely.contains_xy` polygon mask that NaN's cells outside
  the polygon (station inputs are unaffected). Use `resolve-region`'s
  `--geojson` output to produce a country polygon. May be combined with
  `--bbox` (slice then mask) or used alone (mask, no rectangular slice).
  Admin-1 boundary overlay is drawn on top as decoration regardless.

### Behavior

- **Shared-resolution, overlapping bins.** The two inputs must already be
  at the same time resolution; `plot-compare` compares them on their
  overlapping bins. It checks that the two axes are the same kind (both a
  calendar `time` axis, or both a forecast `step` axis — compared within
  the native dtype, never cross-cast) and share a median bin width, then
  intersects the bin labels (matched within a small fraction of the bin
  width) and renders the last `N` of the COMMON labels, selecting the same
  labels from both inputs so each panel shows the same window for both
  rows. A latency offset that drops one input's trailing bin is tolerated
  (one fewer common bin). The run exits non-zero only on a resolution
  mismatch ("different time resolutions; aggregate to a common resolution
  first, e.g. with the `aggregate-temporal` skill") or an empty
  intersection ("no overlapping time bins"). `plot-compare` performs no
  temporal aggregation or unit transformation of its own.
- **Admin-polygon clipping.** The Natural Earth admin-1 GeoDataFrame
  is spatially clipped (`gdf.clip(box(*gridded_bbox))`) so polygons
  that straddle the bbox edge are truncated at the edge rather than
  rendered whole. Empty geometries produced by the clip are dropped.
- **Shared spatial extent.** Both rows' `set_xlim`/`set_ylim` come
  from the gridded input's lat/lon bounds, not from each row's own
  data bounds. Station scatter points outside that extent are clipped.
- **Longitude wrap before bbox slice.** When `--bbox` or `--mask-geojson`
  is set and a gridded input has lon in `[0, 360]`, lons are auto-wrapped
  to `[-180, 180]` (and the dim re-sorted) before the rectangular slice
  and polygon mask. Inputs already in `[-180, 180]` are unaffected.
- **Color-scale mode.** By default the scale is shared when both rows
  resolve to the same variable AND matching (stripped) `units`, and
  independent otherwise. `--shared-scale` / `--independent-scale` force
  the mode. In shared mode both rows use one colormap, normalization,
  vmin, and vmax. In independent mode each row computes its own vmin/vmax
  from its own data, uses its own colormap (precedence `--colormap-a`/`-b`,
  then `--colormap`, then `viridis`) with a continuous norm, and gets its
  own colorbar labeled `{file} {var} [{units}]`. Shared-scale colorbars
  include units too. Units on the figure are a short display form
  (`mm/day`, `°C`), not the on-disk CF string.
- **Input units.** In shared mode, when the two rows carry differing
  `units`, the figure colors values from different units on a single
  scale, so a warning naming both units is printed to stderr. This is a
  rendering caveat only — the figure is still produced and the exit status
  is 0. The check applies only when both rows carry a string `units` attr;
  a missing value is not compared. In independent mode each row has its
  own scale, so no cross-row units warning is emitted.

### Output

A PNG with a `(2, n)` `GridSpec` (`figsize=(22, 10)`,
`wspace=0.08`, `hspace=0.15`). Each row gets its own colorbar.
Station scatter points use `s=30`. Y-axis labels appear only on the
leftmost panel of each row.

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
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py -i /tmp/tahmo_dekadal.zarr -i /tmp/imerg_dekadal.zarr \
    --variable precip --output /tmp/sat_vs_station.png \
    --title "IMERG vs TAHMO dekadal"
```

Both inputs are on the same dekadal axis here: the station `tahmo.zarr`
was aggregated to `tahmo_dekadal.zarr` with the `aggregate-temporal`
skill (same period/method/anchor as the IMERG dekadal aggregation)
before comparing.
