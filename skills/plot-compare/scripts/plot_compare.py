# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cartopy",
#   "cf-xarray",
#   "geopandas",
#   "matplotlib",
#   "numpy",
#   "pandas",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
# ]
# ///
"""Side-by-side multi-panel PNG comparing two Rhiza Envelope Zarrs.

Top row is dataset A, bottom row is dataset B. When exactly one of A/B
is a station-schema Zarr, it is placed on the top row to match the
canonical "stations vs. satellite" presentation. A shared colormap and
normalization are used across both rows.
"""

import argparse
import sys
from pathlib import Path

# Shared categorical colormap and BoundaryNorm for precipitation (mm).
PRECIP_COLORS = [
    "#bdbdbd",
    "wheat",
    "lightgreen",
    "green",
    "lightblue",
    "blue",
    "yellow",
    "orange",
    "red",
    "purple",
]
PRECIP_BOUNDS = [0, 10, 20, 40, 60, 80, 110, 150, 200, 250, 350]


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _pick_time_dim(ds, override):
    if override:
        return override
    if "time" in ds.dims:
        return "time"
    if "step" in ds.dims:
        return "step"
    return None


def _is_station(ds):
    return "station_id" in ds.dims


def _format_single(t, bin_width=None):
    """Render a time-bin label.

    When ``bin_width`` is a ``pandas.Timedelta``, format as
    ``YYYY-MM-DD to YYYY-MM-DD`` with ``t`` as the bin start. Otherwise
    fall back to a single ISO date.
    """
    import pandas as pd

    try:
        start = pd.Timestamp(t)
    except (TypeError, ValueError):
        return str(t)
    if bin_width is None:
        return start.date().isoformat()
    try:
        end = start + bin_width
    except (TypeError, ValueError):
        return start.date().isoformat()
    return f"{start.date().isoformat()} to {end.date().isoformat()}"


def _load_admin_boundaries(bbox=None):
    """Natural Earth admin-1 boundaries via cartopy downloader.

    Returns a GeoDataFrame, or None if the dependency or download is
    unavailable. The cartopy downloader handles the canonical URL and
    on-disk caching. When ``bbox`` is provided as
    ``(xmin, ymin, xmax, ymax)``, the GeoDataFrame is spatially
    *clipped* to the bbox: polygons that straddle the bbox edge are
    truncated at the edge rather than rendered whole, so neighboring
    regions never extend beyond the gridded base.
    """
    try:
        import cartopy.io.shapereader as shpreader
        import geopandas as gpd

        shp_path = shpreader.natural_earth(
            resolution="10m",
            category="cultural",
            name="admin_1_states_provinces",
        )
        gdf = gpd.read_file(shp_path)
    except Exception as exc:
        print(
            f"Warning: admin boundaries unavailable ({exc}); skipping overlay.",
            file=sys.stderr,
        )
        return None
    if bbox is not None:
        try:
            from shapely.geometry import box

            xmin, ymin, xmax, ymax = bbox
            clip_geom = box(xmin, ymin, xmax, ymax)
            try:
                gdf = gdf.clip(clip_geom)
            except Exception:
                # Older geopandas may not expose .clip cleanly; fall back
                # to a shapely intersection on each geometry.
                gdf = gdf.copy()
                gdf["geometry"] = gdf.geometry.intersection(clip_geom)
            gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
        except Exception as exc:
            print(
                f"Warning: could not clip admin boundaries to bbox ({exc}); "
                "drawing unclipped overlay.",
                file=sys.stderr,
            )
    return gdf


def _resample_to_bins(da, time_dim, base_times, bin_width, agg):
    """Resample ``da`` along ``time_dim`` to ``base_times`` using ``agg``.

    ``base_times`` is a 1-D array of bin-end values (inclusive); ``bin_width``
    is the (pandas Timedelta) width applied uniformly. For each base time
    ``t``, values where ``t - bin_width < source_time <= t`` are aggregated.
    This matches ``aggregate-temporal``'s left-open right-closed bucket
    convention so the resampled overlay aligns with the gridded base's
    inclusive-end labels. The returned DataArray's time coord equals
    ``base_times`` exactly (the ``IntervalIndex`` from ``groupby_bins``
    is dropped).
    """
    import numpy as np
    import pandas as pd

    base_times = pd.to_datetime(np.asarray(base_times))
    edges = [base_times[0] - bin_width] + list(base_times)
    grouped = da.groupby_bins(time_dim, bins=edges, right=True)
    if agg == "sum":
        out = grouped.sum()
    elif agg == "mean":
        out = grouped.mean()
    elif agg == "max":
        out = grouped.max()
    elif agg == "min":
        out = grouped.min()
    else:
        raise ValueError(f"unknown --overlay-resample agg: {agg}")
    bin_dim = f"{time_dim}_bins"
    # Replace the IntervalIndex bin coord with the original base times.
    out = out.rename({bin_dim: time_dim})
    out = out.assign_coords({time_dim: base_times.values})
    return out


def _median_bin_width(time_values):
    """Return median spacing of a 1-D time coord as a pandas Timedelta.

    Returns ``None`` when the array has fewer than two values or cannot
    be coerced to datetimes.
    """
    import numpy as np
    import pandas as pd

    arr = np.asarray(time_values)
    if arr.size < 2:
        return None
    try:
        diffs = np.diff(pd.to_datetime(arr).values)
    except (TypeError, ValueError):
        return None
    if diffs.size == 0:
        return None
    return pd.Timedelta(pd.Series(diffs).median())


def _scatter_panel(ax, ds, sel, cmap, norm, vmin, vmax):
    lats = ds["latitude"].values
    lons = ds["longitude"].values
    return ax.scatter(
        lons,
        lats,
        c=sel.values,
        cmap=cmap,
        norm=norm,
        vmin=vmin,
        vmax=vmax,
        s=30,
    )


def _grid_panel(ax, sel, cmap, norm, vmin, vmax):
    lat_dim = _cf_dim(sel, "latitude")
    lon_dim = _cf_dim(sel, "longitude")
    return sel.transpose(lat_dim, lon_dim).plot.pcolormesh(
        ax=ax,
        x=lon_dim,
        y=lat_dim,
        cmap=cmap,
        norm=norm,
        vmin=vmin,
        vmax=vmax,
        add_colorbar=False,
    )


def _ax_bounds(ds, variable):
    if _is_station(ds):
        lons = ds["longitude"].values
        lats = ds["latitude"].values
    else:
        lat_dim = _cf_dim(ds[variable], "latitude")
        lon_dim = _cf_dim(ds[variable], "longitude")
        lons = ds[lon_dim].values
        lats = ds[lat_dim].values
    import numpy as np

    return (
        float(np.nanmin(lons)),
        float(np.nanmax(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lats)),
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        "-i",
        action="append",
        required=True,
        help="Input Zarr; pass exactly twice (first = A, second = B)",
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument(
        "--colormap",
        default=None,
        help="matplotlib colormap name. When omitted, the categorical "
        "precipitation colormap with BoundaryNorm is used.",
    )
    p.add_argument("--title")
    p.add_argument("--panels", type=int, default=3)
    p.add_argument("--time-dim")
    p.add_argument(
        "--overlay-resample",
        choices=("sum", "mean", "max", "min"),
        default="sum",
        help="When one input is station-schema and its time grid is finer "
        "than the gridded input's, aggregate the station time axis to the "
        "gridded input's bins using this rule.",
    )
    args = p.parse_args()

    if len(args.input) != 2:
        print(
            f"Error: --input must be passed exactly twice; got {len(args.input)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    path_a, path_b = args.input

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
    from matplotlib.gridspec import GridSpec

    for pth in (path_a, path_b):
        if not Path(pth).exists():
            print(f"Error: {pth} not found.", file=sys.stderr)
            sys.exit(2)

    ds_a = xr.open_zarr(path_a, consolidated=False)
    ds_b = xr.open_zarr(path_b, consolidated=False)

    variable = args.variable or (list(ds_a.data_vars)[0] if ds_a.data_vars else None)
    if variable is None or variable not in ds_a or variable not in ds_b:
        print(
            f"Error: variable '{variable}' must exist in both inputs. "
            f"A: {list(ds_a.data_vars)}  B: {list(ds_b.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)

    td_a = _pick_time_dim(ds_a, args.time_dim)
    td_b = _pick_time_dim(ds_b, args.time_dim)
    if td_a is None or td_b is None:
        print(
            f"Error: both inputs need a time/step dim. A: {list(ds_a.dims)}  B: {list(ds_b.dims)}",
            file=sys.stderr,
        )
        sys.exit(2)

    n = min(args.panels, ds_a.sizes[td_a], ds_b.sizes[td_b])
    if n < 1:
        print("Error: no overlapping panels to plot.", file=sys.stderr)
        sys.exit(1)

    da_a = ds_a[variable]
    da_b = ds_b[variable]

    a_lat = _cf_dim(da_a, "latitude")
    a_lon = _cf_dim(da_a, "longitude")
    b_lat = _cf_dim(da_b, "latitude")
    b_lon = _cf_dim(da_b, "longitude")
    spatial_dims = {a_lat, a_lon, b_lat, b_lon} - {None}

    def _flatten(da, time_dim):
        for d in list(da.dims):
            if d == time_dim or d == "station_id" or d in spatial_dims:
                continue
            da = da.mean(d) if d == "number" else da.isel({d: 0}, drop=True)
        return da

    da_a = _flatten(da_a, td_a)
    da_b = _flatten(da_b, td_b)

    # When exactly one input is station-schema and its time grid is finer
    # than the gridded input's, resample the station's time axis to the
    # gridded input's bins. The gridded input is the "base"; the station
    # input is the "overlay". This keeps the skill generic — no
    # TAHMO/IMERG/CHIRPS or variable-specific assumptions.
    a_station = _is_station(ds_a)
    b_station = _is_station(ds_b)
    base_bin_width = None
    if a_station ^ b_station:
        if a_station:
            station_da, station_td = da_a, td_a
            base_da, base_td = da_b, td_b
        else:
            station_da, station_td = da_b, td_b
            base_da, base_td = da_a, td_a
        base_bin_width = _median_bin_width(base_da[base_td].values)
        station_bin_width = _median_bin_width(station_da[station_td].values)
        if (
            base_bin_width is not None
            and station_bin_width is not None
            and station_bin_width < base_bin_width
        ):
            base_times = base_da[base_td].values
            station_da = _resample_to_bins(
                station_da,
                station_td,
                base_times,
                base_bin_width,
                args.overlay_resample,
            )
            if a_station:
                da_a = station_da
            else:
                da_b = station_da

    # Decide row layout: when exactly one is station-schema, put it on top.
    label_a = Path(path_a).name
    label_b = Path(path_b).name
    if b_station and not a_station:
        top = (ds_b, da_b, td_b, label_b)
        bottom = (ds_a, da_a, td_a, label_a)
    else:
        top = (ds_a, da_a, td_a, label_a)
        bottom = (ds_b, da_b, td_b, label_b)

    if args.colormap is None:
        cmap = LinearSegmentedColormap.from_list("wgbrp", PRECIP_COLORS)
        norm = BoundaryNorm(PRECIP_BOUNDS, cmap.N)
        vmin = vmax = None
    else:
        cmap = args.colormap
        norm = None
        vmax = float(np.nanmax([da_a.max().values, da_b.max().values]))
        vmin = float(np.nanmin([da_a.min().values, da_b.min().values]))

    fig = plt.figure(figsize=(22, 10))
    gs = GridSpec(2, n, figure=fig, wspace=0.08, hspace=0.15)
    top_axes = [fig.add_subplot(gs[0, i]) for i in range(n)]
    bottom_axes = [fig.add_subplot(gs[1, i]) for i in range(n)]
    if args.title:
        fig.suptitle(args.title)

    # The "gridded base" determines both the admin-polygon clip bbox and
    # the shared spatial extent across both rows. Pick whichever of A/B
    # is NOT station-schema; if neither is station-schema, default to A.
    if a_station and not b_station:
        gridded_ds = ds_b
    elif b_station and not a_station:
        gridded_ds = ds_a
    else:
        gridded_ds = ds_a
    g_xmin, g_xmax, g_ymin, g_ymax = _ax_bounds(gridded_ds, variable)
    gridded_bbox = (g_xmin, g_ymin, g_xmax, g_ymax)

    boundaries = _load_admin_boundaries(bbox=gridded_bbox)

    def _plot_row(axes, row, n_panels):
        ds, da, td, label = row
        is_station = _is_station(ds)
        n_avail = da.sizes[td]
        first = max(0, n_avail - n_panels)
        last_im = None
        for col in range(n_panels):
            ax = axes[col]
            sel = da.isel({td: first + col})
            t_val = da[td].values[first + col]
            title_t = _format_single(t_val, bin_width=base_bin_width)
            if is_station:
                last_im = _scatter_panel(ax, ds, sel, cmap, norm, vmin, vmax)
            else:
                last_im = _grid_panel(ax, sel, cmap, norm, vmin, vmax)
            ax.set_title(f"{label}: {title_t}", fontsize=9)
            if boundaries is not None:
                boundaries.boundary.plot(
                    edgecolor="grey",
                    linewidth=1.0,
                    ax=ax,
                )
            if col != 0:
                ax.set_ylabel("")
                ax.tick_params(left=False, labelleft=False)
            else:
                ax.set_ylabel("lat")
        return last_im

    sc_top = _plot_row(top_axes, top, n)
    im_bottom = _plot_row(bottom_axes, bottom, n)

    # Force both rows to share the gridded base's spatial extent. The
    # station scatter still renders at actual lat/lon — matplotlib clips
    # any points outside the limits naturally.
    for ax in top_axes:
        ax.set_xlim(g_xmin, g_xmax)
        ax.set_ylim(g_ymin, g_ymax)
        ax.set_xlabel("")
    for col, ax in enumerate(bottom_axes):
        ax.set_xlim(g_xmin, g_xmax)
        ax.set_ylim(g_ymin, g_ymax)
        ax.set_xlabel("lon" if col == n // 2 else "")

    fig.colorbar(
        sc_top,
        ax=top_axes,
        label=f"{top[3]} {variable}",
        shrink=0.6,
        fraction=0.02,
        pad=0.02,
    )
    fig.colorbar(
        im_bottom,
        ax=bottom_axes,
        label=f"{bottom[3]} {variable}",
        shrink=0.6,
        fraction=0.02,
        pad=0.02,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
