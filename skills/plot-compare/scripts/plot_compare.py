# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cartopy",
#   "cf-xarray",
#   "cftime",
#   "geopandas>=1",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "numpy",
#   "pandas",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs."""

import sys
from pathlib import Path

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.standard_utils import (
    dataset_label,
    lat_slice,
    pick_time_dim,
    polygon_from_geojson,
)
from weather_skills_core.units import (
    format_units_for_display,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

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


def _is_station(ds):
    return "station_id" in ds.dims


def _format_single(t, bin_width=None):
    """Render a time-bin label; with bin_width, ``YYYY-MM-DD to YYYY-MM-DD`` (right-edge)."""
    import datetime as _dt

    import pandas as pd

    if hasattr(t, "calendar"):
        if bin_width is None:
            return t.strftime("%Y-%m-%d")
        try:
            start = t - bin_width + _dt.timedelta(days=1)
        except (TypeError, ValueError):
            return t.strftime("%Y-%m-%d")
        return f"{start.strftime('%Y-%m-%d')} to {t.strftime('%Y-%m-%d')}"

    try:
        end = pd.Timestamp(t)
    except (TypeError, ValueError):
        return str(t)
    if bin_width is None:
        return end.date().isoformat()
    try:
        start = end - bin_width + pd.Timedelta(days=1)
    except (TypeError, ValueError):
        return end.date().isoformat()
    return f"{start.date().isoformat()} to {end.date().isoformat()}"


def _load_admin_boundaries(bbox=None):
    """Natural Earth admin-1 via cartopy; optional shapely clip to bbox. None on failure."""
    try:
        import cartopy.io.shapereader as shpreader
        import geopandas as gpd

        shp_path = shpreader.natural_earth(
            resolution="10m", category="cultural", name="admin_1_states_provinces"
        )
        gdf = gpd.read_file(shp_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: admin boundaries unavailable ({exc}); skipping overlay.", file=sys.stderr)
        return None
    if bbox is None:
        return gdf
    try:
        from shapely.geometry import box

        xmin, ymin, xmax, ymax = bbox
        clip_geom = box(xmin, ymin, xmax, ymax)
        try:
            gdf = gdf.clip(clip_geom)
        except Exception:  # noqa: BLE001
            gdf = gdf.copy()
            gdf["geometry"] = gdf.geometry.intersection(clip_geom)
        return gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: could not clip admin boundaries to bbox ({exc}); drawing unclipped overlay.",
            file=sys.stderr,
        )
        return gdf


def _median_bin_width(time_values):
    """Median spacing of a 1-D time coord (pandas.Timedelta or datetime.timedelta)."""
    import datetime as _dt

    import numpy as np
    import pandas as pd

    arr = np.asarray(time_values)
    if arr.size < 2:
        return None
    if arr.dtype.kind == "O" and hasattr(arr.flat[0], "calendar"):
        ordered = np.sort(arr)
        deltas = [
            abs((ordered[i + 1] - ordered[i]).total_seconds()) for i in range(ordered.size - 1)
        ]
        return _dt.timedelta(seconds=float(np.median(deltas))) if deltas else None
    try:
        diffs = np.diff(pd.to_datetime(arr).values)
    except (TypeError, ValueError):
        return None
    return pd.Timedelta(pd.Series(diffs).median()) if diffs.size else None


def _scatter_panel(ax, ds, sel, cmap, norm, vmin, vmax):
    return ax.scatter(
        ds["longitude"].values,
        ds["latitude"].values,
        c=sel.values,
        cmap=cmap,
        norm=norm,
        vmin=vmin,
        vmax=vmax,
        s=30,
    )


def _grid_panel(ax, sel, cmap, norm, vmin, vmax):
    lat_dim = cf_dim(sel, "latitude")
    lon_dim = cf_dim(sel, "longitude")
    return sel.transpose(lat_dim, lon_dim).plot.pcolormesh(
        ax=ax, x=lon_dim, y=lat_dim, cmap=cmap, norm=norm, vmin=vmin, vmax=vmax, add_colorbar=False
    )


def _ax_bounds(ds, variable):
    import numpy as np

    if _is_station(ds):
        lons, lats = ds["longitude"].values, ds["latitude"].values
    else:
        lat_dim = cf_dim(ds[variable], "latitude")
        lon_dim = cf_dim(ds[variable], "longitude")
        lons, lats = ds[lon_dim].values, ds[lat_dim].values
    return (
        float(np.nanmin(lons)),
        float(np.nanmax(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lats)),
    )


def _is_cftime_axis(values):
    import numpy as np

    return (
        getattr(values.dtype, "kind", None) == "O"
        and values.size > 0
        and hasattr(np.asarray(values).flat[0], "calendar")
    )


def _axis_kind(values):
    kind = getattr(values.dtype, "kind", None)
    if kind == "M":
        return "datetime"
    if kind == "m":
        return "timedelta"
    if _is_cftime_axis(values):
        return "datetime"
    return None


@weather_skill(
    name="plot-compare",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--variable-a",
    default=None,
    help="Variable for row A. Overrides --variable. Default: --variable or auto.",
)
@weather_skill.argument(
    "--variable-b",
    default=None,
    help="Variable for row B. Overrides --variable. Default: --variable or auto.",
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help="matplotlib colormap. Shared-scale default: categorical precip BoundaryNorm.",
)
@weather_skill.argument(
    "--colormap-a",
    default=None,
    help="Colormap for row A in independent-scale mode.",
)
@weather_skill.argument(
    "--colormap-b",
    default=None,
    help="Colormap for row B in independent-scale mode.",
)
@weather_skill.argument(
    "--shared-scale",
    action="store_true",
    help="Force one shared color scale across both rows.",
)
@weather_skill.argument(
    "--independent-scale",
    action="store_true",
    help="Force per-row color scales.",
)
@weather_skill.argument("--panels", type=int, default=3)
@weather_skill.argument(
    "--time-dim", default=None, help="Override the time axis. Defaults to time, else step."
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; gridded cells outside become NaN.",
)
def plot_compare(
    ds,
    bbox,
    variable,
    variable_a,
    variable_b,
    colormap,
    colormap_a,
    colormap_b,
    shared_scale,
    independent_scale,
    title,
    panels,
    time_dim,
    mask_geojson,
    output,
    **kwargs,
):
    """Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_a, ds_b = ds
    if shared_scale and independent_scale:
        raise UsageError("--shared-scale and --independent-scale are mutually exclusive.")

    label_a = dataset_label(ds_a, "A")
    label_b = dataset_label(ds_b, "B")

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
    from matplotlib.gridspec import GridSpec

    var_a = variable_a or variable or auto_variable(ds_a)
    var_b = variable_b or variable or auto_variable(ds_b)
    for side, var, ds in (("A", var_a, ds_a), ("B", var_b, ds_b)):
        if var is None or var not in ds:
            mapping_targets = {
                ds[d].attrs.get("grid_mapping")
                for d in ds.data_vars
                if ds[d].attrs.get("grid_mapping")
            }
            real_vars = [
                v
                for v in ds.data_vars
                if "grid_mapping_name" not in ds[v].attrs and v not in mapping_targets
            ]
            raise UsageError(
                f"variable '{var}' must exist in input {side}. {side} real data vars: {real_vars}"
            )

    ds_a = precip_for_display(to_standard_units(ds_a, variables=[var_a]), var_a)
    ds_b = precip_for_display(to_standard_units(ds_b, variables=[var_b]), var_b)

    try:
        td_a = pick_time_dim(ds_a, time_dim)
        td_b = pick_time_dim(ds_b, time_dim)
    except UsageError:
        raise UsageError(
            f"both inputs need a time/step dim. A: {list(ds_a.dims)}  B: {list(ds_b.dims)}"
        ) from None

    raw_a = ds_a[td_a].values
    raw_b = ds_b[td_b].values
    kind_a = _axis_kind(raw_a)
    kind_b = _axis_kind(raw_b)
    if kind_a is None or kind_b is None or kind_a != kind_b:
        raise DataError(
            "the two inputs have different time resolutions "
            f"('{td_a}' dtype={raw_a.dtype}, '{td_b}' dtype={raw_b.dtype}); "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill."
        )

    a_is_cftime = _is_cftime_axis(raw_a)
    b_is_cftime = _is_cftime_axis(raw_b)
    if a_is_cftime != b_is_cftime:
        cf_td = td_a if a_is_cftime else td_b
        std_td = td_b if a_is_cftime else td_a
        raise DataError(
            "cannot compare a model-calendar (cftime) time axis "
            f"('{cf_td}') against a standard-calendar (datetime64) time axis "
            f"('{std_td}'); convert both to a common calendar first with the "
            "convert-calendar skill."
        )
    if a_is_cftime and b_is_cftime and raw_a.flat[0].calendar != raw_b.flat[0].calendar:
        raise DataError(
            "the two inputs use different model calendars "
            f"('{td_a}' calendar={raw_a.flat[0].calendar!r} vs "
            f"'{td_b}' calendar={raw_b.flat[0].calendar!r}); "
            "convert both to a common calendar first with the convert-calendar skill."
        )

    cftime_axes = a_is_cftime and b_is_cftime
    if cftime_axes:
        import cftime

        _epoch = "days since 1970-01-01"
        enc_a = np.asarray(
            cftime.date2num(raw_a, units=_epoch, calendar=raw_a.flat[0].calendar), dtype="float64"
        )
        enc_b = np.asarray(
            cftime.date2num(raw_b, units=_epoch, calendar=raw_b.flat[0].calendar), dtype="float64"
        )
        tol_enc = 1.0 / 86400.0
    else:
        ns_dtype = "datetime64[ns]" if kind_a == "datetime" else "timedelta64[ns]"
        enc_a = raw_a.astype(ns_dtype).astype("int64")
        enc_b = raw_b.astype(ns_dtype).astype("int64")
        tol_enc = 1_000_000_000

    def _median_spacing(enc_values, ds, dim):
        bound_name = ds[dim].attrs.get("bounds") if dim in ds else None
        if isinstance(bound_name, str) and bound_name in ds:
            pairs = np.asarray(ds[bound_name].values)
            if pairs.ndim == 2 and pairs.shape[1] == 2:
                try:
                    widths = np.abs(
                        pairs[:, 1].astype("timedelta64[ns]").astype("int64")
                        - pairs[:, 0].astype("timedelta64[ns]").astype("int64")
                    )
                except (TypeError, ValueError):
                    try:
                        widths = np.abs(
                            pairs[:, 1].astype("datetime64[ns]").astype("int64")
                            - pairs[:, 0].astype("datetime64[ns]").astype("int64")
                        )
                    except (TypeError, ValueError):
                        widths = None
                if widths is not None and widths.size:
                    return float(np.median(widths))
        if enc_values.size < 2:
            return None
        return float(np.median(np.abs(np.diff(enc_values))))

    width_a = _median_spacing(enc_a, ds_a, td_a)
    width_b = _median_spacing(enc_b, ds_b, td_b)
    if width_a is not None and width_b is not None:
        rel = abs(width_a - width_b) / max(width_a, width_b, 1.0)
        if rel > 1e-3:
            if cftime_axes:
                wa_str, wb_str = f"{width_a:.4g} days", f"{width_b:.4g} days"
            else:
                wa_str, wb_str = f"{width_a:.0f} ns", f"{width_b:.0f} ns"
            raise DataError(
                "the two inputs have different time resolutions "
                f"(median bin width '{td_a}'≈{wa_str} vs '{td_b}'≈{wb_str}); "
                "aggregate both inputs to a common resolution first, e.g. with the "
                "aggregate-temporal skill."
            )

    order_a = np.argsort(enc_a, kind="stable")
    sorted_enc_a = enc_a[order_a]
    sorted_enc_b = np.sort(enc_b)
    common_enc, common_src = [], []
    for pos, va in zip(order_a, sorted_enc_a, strict=True):
        idx = np.searchsorted(sorted_enc_b, va)
        nearest = None
        for cand in (idx - 1, idx):
            if 0 <= cand < sorted_enc_b.size:
                d = abs(float(sorted_enc_b[cand]) - float(va))
                if nearest is None or d < nearest:
                    nearest = d
        if nearest is not None and nearest <= tol_enc:
            common_enc.append(va)
            common_src.append(int(pos))

    if not common_enc:
        raise DataError(f"no overlapping time bins between the two inputs on '{td_a}'/'{td_b}'.")

    n = min(panels, len(common_enc))
    src_last = common_src[-n:]
    if cftime_axes:
        import datetime as _dt

        common_labels = np.asarray(raw_a, dtype=object)[src_last]
        common_tol = _dt.timedelta(seconds=1)
    else:
        common_labels = np.asarray(common_enc[-n:], dtype="int64").astype(ns_dtype)
        common_tol = np.timedelta64(1, "s")

    da_a = ds_a[var_a]
    da_b = ds_b[var_b]
    units_a = variable_units(da_a)
    units_b = variable_units(da_b)
    units_match = (
        isinstance(units_a, str) and isinstance(units_b, str) and units_equal(units_a, units_b)
    )
    if shared_scale:
        use_shared_scale = True
    elif independent_scale:
        use_shared_scale = False
    else:
        use_shared_scale = var_a == var_b and units_match

    if (
        use_shared_scale
        and not units_match
        and isinstance(units_a, str)
        and isinstance(units_b, str)
    ):
        print(
            f"Warning: the two rows have differing units "
            f"({label_a} {var_a!r} units={units_a!r}, "
            f"{label_b} {var_b!r} units={units_b!r}). "
            f"The two rows are drawn on one shared color scale, so values in "
            f"different units are not directly comparable in this figure.",
            file=sys.stderr,
        )

    a_lat = cf_dim(da_a, "latitude")
    a_lon = cf_dim(da_a, "longitude")
    b_lat = cf_dim(da_b, "latitude")
    b_lon = cf_dim(da_b, "longitude")
    spatial_dims = {a_lat, a_lon, b_lat, b_lon} - {None}

    def _flatten(da, tdim):
        for d in list(da.dims):
            if d == tdim or d == "station_id" or d in spatial_dims:
                continue
            da = da.mean(d) if d == "number" else da.isel({d: 0}, drop=True)
        return da

    da_a = _flatten(da_a, td_a)
    da_b = _flatten(da_b, td_b)

    region_bbox = bbox
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None

    if region_bbox is not None or region_polygon is not None:
        r_n, r_w, r_s, r_e = region_bbox if region_bbox is not None else (None, None, None, None)
        for side, ds_label in (("a", label_a), ("b", label_b)):
            ds = ds_a if side == "a" else ds_b
            da = da_a if side == "a" else da_b
            if _is_station(ds):
                if region_bbox is not None:
                    lons, lats = ds["longitude"].values, ds["latitude"].values
                    lon_keep = (
                        (lons >= r_w) | (lons <= r_e)
                        if r_w > r_e
                        else (lons >= r_w) & (lons <= r_e)
                    )
                    keep = lon_keep & (lats >= r_s) & (lats <= r_n)
                    keep_ids = ds["station_id"].values[keep]
                    if len(keep_ids) == 0:
                        print(
                            f"Warning: 0 stations inside --bbox {r_n}/{r_w}/{r_s}/{r_e} "
                            f"on input '{ds_label}'; scatter will render empty.",
                            file=sys.stderr,
                        )
                    ds = ds.sel(station_id=keep_ids)
                    da = da.sel(station_id=keep_ids)
                    if r_w > r_e:
                        shifted_lon = ((ds["longitude"].values - r_w) % 360.0) + r_w
                        ds = ds.assign_coords(longitude=("station_id", shifted_lon))
            else:
                lat_dim = cf_dim(da, "latitude")
                lon_dim = cf_dim(da, "longitude")
                if lat_dim is not None and lon_dim is not None:
                    lon_vals = da[lon_dim].values
                    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
                        wrap = (ds[lon_dim] + 180) % 360 - 180
                        ds = ds.assign_coords({lon_dim: wrap}).sortby(lon_dim)
                        da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(
                            lon_dim
                        )
                    if region_bbox is not None:
                        lat_sl = lat_slice(da[lat_dim].values, r_n, r_s)
                        ds = ds.sel({lat_dim: lat_sl})
                        da = da.sel({lat_dim: lat_sl})
                        if r_w > r_e:
                            ds = ds.where((ds[lon_dim] >= r_w) | (ds[lon_dim] <= r_e), drop=True)
                            da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
                        else:
                            ds = ds.sel({lon_dim: slice(r_w, r_e)})
                            da = da.sel({lon_dim: slice(r_w, r_e)})
                    if region_polygon is not None:
                        import shapely
                        import xarray as _xr

                        lon_grid, lat_grid = np.meshgrid(da[lon_dim].values, da[lat_dim].values)
                        mask = shapely.contains_xy(region_polygon, lon_grid, lat_grid)
                        if not bool(mask.any()):
                            print(
                                f"Warning: --mask-geojson polygon does not intersect "
                                f"input '{ds_label}'; its panel will be entirely empty.",
                                file=sys.stderr,
                            )
                        da = da.where(_xr.DataArray(mask, dims=(lat_dim, lon_dim)))
                    if region_bbox is not None and r_w > r_e:
                        ds = ds.assign_coords(
                            {lon_dim: ((ds[lon_dim] - r_w) % 360.0) + r_w}
                        ).sortby(lon_dim)
                        da = da.assign_coords(
                            {lon_dim: ((da[lon_dim] - r_w) % 360.0) + r_w}
                        ).sortby(lon_dim)
                elif region_bbox is not None:
                    print(
                        f"Warning: input '{ds_label}' has no CF lat/lon "
                        f"dims; --bbox {r_n}/{r_w}/{r_s}/{r_e} slice not applied.",
                        file=sys.stderr,
                    )
                elif region_polygon is not None:
                    print(
                        f"Warning: input '{ds_label}' has no CF lat/lon "
                        f"dims; --mask-geojson polygon not applied.",
                        file=sys.stderr,
                    )
            if side == "a":
                ds_a, da_a = ds, da
            else:
                ds_b, da_b = ds, da

    a_station = _is_station(ds_a)
    b_station = _is_station(ds_b)

    def _row_units(da):
        return variable_units(da)

    if use_shared_scale:
        if colormap is None:
            shared_cmap = LinearSegmentedColormap.from_list("wgbrp", PRECIP_COLORS)
            shared_norm = BoundaryNorm(PRECIP_BOUNDS, shared_cmap.N)
            shared_vmin = shared_vmax = None
        else:
            shared_cmap = colormap
            shared_norm = None
            shared_vmax = float(np.nanmax([da_a.max().values, da_b.max().values]))
            shared_vmin = float(np.nanmin([da_a.min().values, da_b.min().values]))
        scale_a = scale_b = (shared_cmap, shared_norm, shared_vmin, shared_vmax)
    else:
        scale_a = (
            colormap_a or colormap or "viridis",
            None,
            float(da_a.min().values),
            float(da_a.max().values),
        )
        scale_b = (
            colormap_b or colormap or "viridis",
            None,
            float(da_b.min().values),
            float(da_b.max().values),
        )

    side_a = (ds_a, da_a, td_a, label_a, var_a, _row_units(da_a), scale_a)
    side_b = (ds_b, da_b, td_b, label_b, var_b, _row_units(da_b), scale_b)
    top, bottom = (side_b, side_a) if b_station and not a_station else (side_a, side_b)

    fig = plt.figure(figsize=(22, 10))
    gs = GridSpec(2, n, figure=fig, wspace=0.08, hspace=0.15)
    top_axes = [fig.add_subplot(gs[0, i]) for i in range(n)]
    bottom_axes = [fig.add_subplot(gs[1, i]) for i in range(n)]
    if title:
        fig.suptitle(title)

    if a_station and not b_station:
        gridded_ds, gridded_var = ds_b, var_b
    elif b_station and not a_station:
        gridded_ds, gridded_var = ds_a, var_a
    else:
        gridded_ds, gridded_var = ds_a, var_a
    g_xmin, g_xmax, g_ymin, g_ymax = _ax_bounds(gridded_ds, gridded_var)
    wrapped_bbox = region_bbox is not None and region_bbox[1] > region_bbox[3]
    if region_bbox is not None:
        r_n, r_w, r_s, r_e = region_bbox
        g_xmin, g_ymin, g_ymax = r_w, r_s, r_n
        g_xmax = r_e + 360.0 if wrapped_bbox else r_e
    gridded_bbox = (g_xmin, g_ymin, g_xmax, g_ymax)

    boundaries = _load_admin_boundaries(bbox=None if wrapped_bbox else gridded_bbox)
    if boundaries is not None and wrapped_bbox:
        import shapely

        def _wrap_coords(coords):
            out = coords.copy()
            out[:, 0] = np.where(out[:, 0] < r_w, out[:, 0] + 360.0, out[:, 0])
            return out

        boundaries = boundaries.copy()
        boundaries["geometry"] = boundaries.geometry.apply(
            lambda g: shapely.transform(g, _wrap_coords) if g is not None and not g.is_empty else g
        )

    def _plot_row(axes, row, n_panels):
        ds, da, td, label, _var, _units, scale = row
        cmap, norm, vmin, vmax = scale
        is_station = _is_station(ds)
        row_sel = da.sel({td: common_labels}, method="nearest", tolerance=common_tol)
        bin_width = _median_bin_width(da[td].values)
        last_im = None
        for col in range(n_panels):
            ax = axes[col]
            sel = row_sel.isel({td: col})
            title_t = _format_single(row_sel[td].values[col], bin_width=bin_width)
            if is_station:
                last_im = _scatter_panel(ax, ds, sel, cmap, norm, vmin, vmax)
            else:
                last_im = _grid_panel(ax, sel, cmap, norm, vmin, vmax)
            ax.set_title(f"{label}: {title_t}", fontsize=9)
            if boundaries is not None:
                boundaries.boundary.plot(edgecolor="grey", linewidth=1.0, ax=ax)
            if col != 0:
                ax.set_ylabel("")
                ax.tick_params(left=False, labelleft=False)
            else:
                ax.set_ylabel("lat")
        return last_im

    sc_top = _plot_row(top_axes, top, n)
    im_bottom = _plot_row(bottom_axes, bottom, n)

    for ax in top_axes:
        ax.set_xlim(g_xmin, g_xmax)
        ax.set_ylim(g_ymin, g_ymax)
        ax.set_xlabel("")
    for col, ax in enumerate(bottom_axes):
        ax.set_xlim(g_xmin, g_xmax)
        ax.set_ylim(g_ymin, g_ymax)
        ax.set_xlabel("lon" if col == n // 2 else "")

    def _cbar_label(row):
        _ds, _da, _td, label, var, units, _scale = row
        shown = format_units_for_display(units)
        if shown:
            return f"{label} {var} [{shown}]"
        return f"{label} {var}"

    fig.colorbar(sc_top, ax=top_axes, label=_cbar_label(top), shrink=0.6, fraction=0.02, pad=0.02)
    fig.colorbar(
        im_bottom, ax=bottom_axes, label=_cbar_label(bottom), shrink=0.6, fraction=0.02, pad=0.02
    )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_compare()
