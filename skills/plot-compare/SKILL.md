---
name: plot-compare
description: Render a side-by-side multi-panel comparison PNG of two Rhiza Envelope Zarr stores (gridded-vs-gridded or station-vs-gridded). Use for sat-vs-station validation, model-vs-obs comparison, or cross-source QC.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot-compare

Source-agnostic two-dataset visualization. Produces a 2-row figure
with one panel per time slice; row A is one input, row B the other.
Handles:

- Gridded vs. gridded (pcolormesh maps).
- Station (`station_id`-indexed) vs. gridded (scatter over mesh).

When exactly one input is a station-schema Zarr, that input is placed
on the top row to match the canonical "stations vs. satellite" layout.

A shared categorical precipitation colormap with `BoundaryNorm` is the
default so values are visually comparable across rows. An admin-1
country boundary overlay (Natural Earth, fetched and cached via
`cartopy`) is drawn on every panel. The polygon overlay is spatially
*clipped* to the gridded input's bbox (`gdf.clip(box(*bbox))`), so
polygons that straddle the bbox edge are truncated at the edge rather
than rendered whole and neighboring regions never extend beyond the
base.

For station-vs-gridded pairs, the station input's time axis is
aggregated to the gridded input's time bins when the station grid is
finer (e.g. daily station observations against weekly or dekadal
gridded accumulations). Both rows always share the gridded input's
spatial extent so the figure is centered on the gridded base; station
points outside that extent are clipped by matplotlib.

Panel titles render the time-bin range as `YYYY-MM-DD to YYYY-MM-DD`
with the bin coord interpreted as the inclusive right edge: start =
end − bin_width + 1 day. Matches `aggregate-temporal` and
`deaccumulate`'s right-edge convention so a 10-day dekad ending
`2026-05-09` renders as `2026-04-30 to 2026-05-09` (10 days inclusive).

## When to use

- Validating a satellite product against station observations for a country.
- Comparing two forecasts (e.g. model A vs. model B) on the same axes.

## Usage

```
uv run scripts/plot_compare.py -i <a.zarr> -i <b.zarr> --output <out.png> \
    [--variable NAME] [--colormap NAME] [--title TEXT] \
    [--panels N] [--time-dim DIM] [--overlay-resample {sum,mean,max,min}]
```

### Arguments
- `--input`, `-i` — pass exactly twice. The first input is row A, the second is row B. Station-schema is allowed on either.
- `--output`, `-o` — PNG path.
- `--variable` — variable name (must be present in both inputs; default: first var in A).
- `--colormap` — matplotlib colormap. When omitted, the categorical
  precipitation cmap (`["#bdbdbd", "wheat", "lightgreen", "green",
  "lightblue", "blue", "yellow", "orange", "red", "purple"]`) with
  `BoundaryNorm` over `[0, 10, 20, 40, 60, 80, 110, 150, 200, 250, 350]`
  mm is used.
- `--title` — figure title.
- `--panels` — number of panels per row (default 3).
- `--time-dim` — override the time axis. Defaults to `time` if present, else `step`.
- `--overlay-resample` — aggregation rule (`sum`, `mean`, `max`, `min`;
  default `sum`) applied when one input is station-schema and its time
  grid is finer than the gridded input's. For each gridded bin **end**
  `t` and bin width `w` (median of `diff(gridded_time)`), station
  values where `t - w < station_time <= t` are aggregated and assigned
  back to `t`. This matches `aggregate-temporal`'s left-open
  right-closed bucket convention so the resampled overlay aligns with
  the base's inclusive-end labels. Generic — works for any
  station-vs-gridded combination; for accumulating variables
  (precipitation, radiation) use `sum`; for intensive variables
  (temperature) use `mean`. Coarser-than-base station inputs are left
  untouched.

### Behavior

- **Time-bin alignment.** When the station overlay has a finer time
  grid than the gridded base, the overlay is aggregated to the base's
  bins per `--overlay-resample` before plotting. Both rows then share
  the same time-bin labels.
- **Admin-polygon clipping.** The Natural Earth admin-1 GeoDataFrame
  is spatially clipped (`gdf.clip(box(*gridded_bbox))`) so polygons
  that straddle the bbox edge are truncated at the edge rather than
  rendered whole. Empty geometries produced by the clip are dropped.
- **Shared spatial extent.** Both rows' `set_xlim`/`set_ylim` come
  from the gridded input's lat/lon bounds, not from each row's own
  data bounds. Station scatter points outside that extent are clipped.

### Output

A PNG with a `(2, n)` `GridSpec` (`figsize=(22, 10)`,
`wspace=0.08`, `hspace=0.15`). Each row gets its own colorbar.
Station scatter points use `s=30`. Y-axis labels appear only on the
leftmost panel of each row.

## Example

```bash
uv run scripts/plot_compare.py -i /tmp/tahmo.zarr -i /tmp/imerg_dekadal.zarr \
    --variable precip --output /tmp/sat_vs_station.png \
    --title "IMERG vs TAHMO dekadal" \
    --overlay-resample sum
```
