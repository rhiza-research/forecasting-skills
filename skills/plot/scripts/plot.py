# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cartopy",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "cf-units>=3.3",
# ]
# ///
"""Render a heatmap or timeseries PNG from a weather-skills envelope Zarr.

The heatmap mode produces a CartoPy panel layout: one subplot per step
(up to 4 columns), shared color scale, country/coastline boundaries, and a
horizontal colorbar at the bottom spanning all panels.
"""

import json
import re
import sys
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.dataset import auto_variable, cf_dim, lat_slice, polygon_from_geojson
from weather_skills_core.units import to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.16"

# Strict decimal integer: optional single sign, then ASCII digits only.
_INDEX_INT_RE = re.compile(r"[+-]?[0-9]+")

def _parse_index(spec):
    """Parse an ``--index`` spec into ``{dim: int | list[int]}``.

    Comma-separated tokens are walked left to right. A token containing
    ``=`` starts a new dimension; bare integer tokens append to the current
    dimension's values, so ``step=0,1,2`` selects three positions along
    ``step``. One value yields a scalar ``int``; several yield a
    ``list[int]``. Whitespace around dimension names and values is ignored;
    a blank or missing spec yields ``{}``.

    Raises ``ValueError`` for a bare token before any ``dim=`` key, an empty
    dimension name or value, an empty token (a leading, doubled, or trailing
    comma), a non-integer value, a repeated dimension, or a repeated position
    within one dimension's list.
    """
    if not spec or not spec.strip():
        return {}
    values = {}
    current = None
    for token in spec.split(","):
        if "=" in token:
            key, _, raw = token.partition("=")
            current = key.strip()
            if not current:
                raise ValueError(f"--index token {token.strip()!r} has an empty dimension name")
            if current in values:
                raise ValueError(f"--index dimension {current!r} is given more than once")
            values[current] = []
            raw = raw.strip()
            if not raw:
                raise ValueError(f"--index value for {current!r} is empty")
        else:
            raw = token.strip()
            if not raw:
                raise ValueError("--index spec has an empty token (stray comma)")
            if current is None:
                raise ValueError(f"--index token {raw!r} appears before any 'dim=' assignment")
        if not _INDEX_INT_RE.fullmatch(raw):
            raise ValueError(f"--index value {raw!r} for {current!r} is not an integer")
        pos = int(raw)
        if pos in values[current]:
            raise ValueError(f"--index position {pos} is repeated for dimension {current!r}")
        values[current].append(pos)
    return {k: v[0] if len(v) == 1 else v for k, v in values.items()}

def _parse_extent(spec):
    if not spec:
        return None
    parts = [float(x) for x in spec.split(",")]
    if len(parts) != 4:
        raise ValueError(
            "--extent expects 4 comma-separated floats: lon_min,lon_max,lat_min,lat_max"
        )
    return parts

def _parse_colormap(spec):
    """Pass through a named matplotlib colormap, or build one from a color list.

    Named matplotlib colormap identifiers cannot contain commas, so the
    presence of a comma in ``spec`` unambiguously indicates a custom color
    list. Whitespace around each color is stripped.
    """
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)

def _parse_cities(spec):
    if not spec:
        return {}
    p = Path(spec)
    raw = p.read_text() if p.exists() else spec
    data = json.loads(raw)
    out = {}
    for name, val in data.items():
        if isinstance(val, dict):
            out[name] = (float(val["lat"]), float(val["lon"]))
        else:
            out[name] = (float(val[0]), float(val[1]))
    return out

def _figsize_from_extent(lon_min, lon_max, lat_min, lat_max, base_height=5.0):
    lat_range = abs(lat_max - lat_min)
    lon_range = abs(lon_max - lon_min)
    if lat_range == 0 or lon_range == 0:
        return base_height, base_height
    height = base_height
    width = height * lon_range / lat_range
    return max(width, 2.0), height

def _step_dim(da):
    for cand in ("step", "time", "valid_time"):
        if cand in da.dims:
            return cand
    cf = cf_dim(da, "time")
    return cf if cf and cf in da.dims else None

def _format_step(value):
    import numpy as np

    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return str(arr)[:16]
    if arr.dtype.kind == "m":
        days = arr.astype("timedelta64[D]").astype(int)
        return f"+{days}d"
    return str(value)

def _panel_title(da, sdim, step_value, all_steps):
    """Match panel_plot_variable: '<start> until <end>' from time + step.

    ``all_steps`` is the variable's native step axis; the per-panel window
    width comes from its spacing, so a panel for any step value shows the
    axis's own window ending at that step's valid time.

    Falls back to '<sdim>=<step>' when the dataset lacks the coords needed
    to construct a date range (no `time` coord, or `step` not a timedelta).
    """
    import numpy as np

    fallback = f"{sdim}={_format_step(step_value)}"
    if "time" not in da.coords:
        return fallback
    step_arr = np.asarray(all_steps)
    if step_arr.dtype.kind != "m":
        return fallback
    try:
        time_val = np.asarray(da["time"].values)
        first = step_arr[0]
        if step_value == first:
            start = time_val
            end = time_val + np.asarray(step_value)
        else:
            dt = step_arr[1] - step_arr[0] if step_arr.size > 1 else np.asarray(step_value) - first
            end = time_val + np.asarray(step_value)
            start = end - dt
        return f"{str(start)[:16]} until {str(end)[:16]}"
    except Exception:  # noqa: BLE001 -- best-effort time-range label; fall back on any failure
        return fallback

def _heatmap(
    da,
    lat_dim,
    lon_dim,
    cmap,
    extent,
    cities,
    title,
    fontsize,
    wrap_lon=True,
    native_step_dim=None,
    native_steps=None,
):
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt
    import numpy as np

    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)

    # Re-wrap a 0..360 global grid into [-180, 180]. Skipped when the caller has
    # already placed the data in a contiguous shifted frame (a wrapped bbox
    # spanning the antimeridian, whose lon coords intentionally exceed 180);
    # re-wrapping there would split the country back into edge slivers.
    if wrap_lon:
        lon_vals = np.asarray(da[lon_dim].values)
        if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
            da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)

    sdim = _step_dim(da)
    if sdim is None or da.sizes.get(sdim, 1) == 1:
        if sdim and sdim in da.dims:
            da = da.squeeze(sdim, drop=True)
        steps = [None]
        sdim = None
    else:
        steps = list(da[sdim].values)

    # Panel-title windows come from the native step axis (captured by the
    # caller before any --index selection) so a non-contiguous selection
    # still titles each panel with the axis's own window width.
    if native_steps is not None and native_step_dim == sdim:
        title_steps = native_steps
    else:
        title_steps = steps

    num_steps = len(steps)
    ncols = min(4, num_steps)
    nrows = int(np.ceil(num_steps / ncols))

    if extent is None:
        # Default extent expands cell-center min/max by half the mean grid
        # spacing on each side, so the view matches what pcolormesh actually
        # draws (it treats coords as cell centers and extends ±½ spacing).
        lat_vals = np.asarray(da[lat_dim].values)
        lon_vals = np.asarray(da[lon_dim].values)
        dlat = float(np.abs(np.diff(np.sort(lat_vals))).mean()) if lat_vals.size > 1 else 0.0
        dlon = float(np.abs(np.diff(np.sort(lon_vals))).mean()) if lon_vals.size > 1 else 0.0
        extent = [
            float(lon_vals.min()) - dlon / 2,
            float(lon_vals.max()) + dlon / 2,
            float(lat_vals.min()) - dlat / 2,
            float(lat_vals.max()) + dlat / 2,
        ]

    vmax = float(da.max(skipna=True).values)
    vmin = float(da.min(skipna=True).values)
    if vmax > 0 and vmin < 0:
        m = max(abs(vmax), abs(vmin))
        vmin, vmax = -m, m

    sw, sh = _figsize_from_extent(*extent)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(sw * ncols, sh * nrows),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()

    contour = None
    for i, s in enumerate(steps):
        ax = axes[i]
        # Select the panel slab by position, not by coordinate value: a
        # duplicate coordinate value on the panel axis would match multiple
        # positions under label selection, keeping the panel dim on the slab.
        # The coordinate value `s` is still used for the panel title.
        slab = da if sdim is None else da.isel({sdim: i})
        # Order the 2D field as (lat, lon) so its shape matches the C array
        # pcolormesh expects given the (lon, lat) X/Y coordinate vectors.
        # Sources like IMERG store the variable as (..., lon, lat), which
        # would otherwise feed a transposed array and raise a shape mismatch.
        # Mirrors plot-compare's _grid_panel transpose.
        slab = slab.transpose(lat_dim, lon_dim)
        contour = ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            transform=ccrs.PlateCarree(),
        )
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            # Wrapped-bbox frame: the x-range spans the antimeridian (e.g. up to
            # ~190). set_extent would normalize that back into [-180, 180] and
            # re-split the country; setting the axes limits directly in
            # PlateCarree projection coordinates (degrees) keeps the contiguous
            # frame. Data and features are still drawn via transform=PlateCarree.
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        ax.add_feature(cfeature.COASTLINE, edgecolor="black")
        ax.add_feature(cfeature.BORDERS, linestyle=":", alpha=0.7)
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        for city, (lat, lon) in cities.items():
            ax.plot(
                lon,
                lat,
                marker="o",
                color="k",
                markersize=6,
                transform=ccrs.PlateCarree(),
            )
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                fontsize=10,
                transform=ccrs.PlateCarree(),
            )
        if s is not None:
            ax.set_title(
                _panel_title(da, sdim, s, title_steps),
                fontsize=int(fontsize * 0.8),
            )

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title, fontsize=fontsize)
    fig.tight_layout(rect=[0, 0, 1, 0.94] if title else None)
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar = fig.colorbar(contour, cax=cbar_ax, orientation="horizontal", fraction=5)
    units = da.attrs.get("units", "")
    label = da.attrs.get("long_name") or da.attrs.get("GRIB_name") or da.name or "value"
    if units:
        label = f"{label} [{units}]"
    cbar.set_label(label, fontsize=fontsize)

    return fig

@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
    inputs=["any"],
    outputs=["figure"]
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--style", choices=["heatmap", "timeseries"], default="heatmap")
@weather_skill.argument("--colormap", default="viridis")
@weather_skill.argument(
            "--index",
            default=None,
            help="Slice spec like 'step=3,number=0' (heatmap only). A dim may take "
            "several comma-separated positions ('step=0,1,2'), which keeps the dim "
            "and, for --style heatmap, yields one panel per selected position. "
            "Negative positions count from the end (Python-style). "
            "Syntax-checked, then ignored with a warning for --style timeseries.",
        )
@weather_skill.argument(
            "--extent",
            default=None,
            help="Map extent as 'lon_min,lon_max,lat_min,lat_max' (heatmap only).",
        )
@weather_skill.argument(
            "--cities",
            default=None,
            help='City overlay JSON (heatmap only). Inline {"name": [lat, lon]} or path to a JSON file.',
        )
@weather_skill.argument("--fontsize", type=int, default=16)
@weather_skill.argument("--title", default=None, help="Optional plot title.")
@weather_skill.argument(
            "--mask-geojson",
            default=None,
            help="Path to a GeoJSON boundary polygon (heatmap only). Gridded cells "
            "outside the polygon are set to NaN before plotting. Use resolve-region's "
            "--geojson output to produce a country polygon.",
        )
def plot(
    ds,
    bbox,
    variable,
    style,
    colormap,
    title,
    index,
    extent,
    cities,
    fontsize,
    mask_geojson,
    output,
    **kwargs,
):
    """Render a heatmap or timeseries PNG from a weather-skills envelope Zarr.

    The heatmap mode produces a CartoPy panel layout: one subplot per step
    (up to 4 columns), shared color scale, country/coastline boundaries, and a
    horizontal colorbar at the bottom spanning all panels.
    """
    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter
    import xarray as xr

    variable = variable or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(f"no usable variable. Available: {list(ds.data_vars)}")
    ds = to_standard_units(ds, variables=[variable])
    da = ds[variable]
    try:
        overrides = _parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    # The decorator parses --bbox to an (N, W, S, E) float tuple (or None).
    bbox_nwse = bbox

    if bbox_nwse is not None and style != "heatmap":
        r_n, r_w, r_s, r_e = bbox_nwse
        print(
            f"Warning: --bbox {r_n}/{r_w}/{r_s}/{r_e} is a heatmap-only option; "
            f"ignored for --style {style}.",
            file=sys.stderr,
        )

    if mask_geojson and style != "heatmap":
        print(
            f"Warning: --mask-geojson is a heatmap-only option; ignored for --style {style}.",
            file=sys.stderr,
        )

    if extent and style != "heatmap":
        print(
            f"Warning: --extent {extent!r} is a heatmap-only option; ignored for --style {style}.",
            file=sys.stderr,
        )

    if cities and style != "heatmap":
        print(
            f"Warning: --cities is a heatmap-only option; ignored for --style {style}.",
            file=sys.stderr,
        )

    if overrides and style != "heatmap":
        print(
            f"Warning: --index {index!r} is a heatmap-only option; ignored for --style {style}.",
            file=sys.stderr,
        )

    if style == "heatmap":
        lat_dim = cf_dim(da, "latitude")
        lon_dim = cf_dim(da, "longitude")
        if lat_dim is None or lon_dim is None:
            raise UsageError(f"heatmap requires lat/lon coords; got {list(da.dims)}.")
        # Station-schema data carries lat/lon as per-point coordinates on a
        # station dimension rather than as dimensions of their own, so there
        # is no 2D grid for pcolormesh to draw.
        if lat_dim not in da.dims or lon_dim not in da.dims:
            raise UsageError(
                f"heatmap needs lat/lon as dimensions, but {lat_dim!r}/"
                f"{lon_dim!r} are non-dimension coordinates here (dims: "
                f"{list(da.dims)}); station data has no 2D grid to plot."
            )
        # Capture the native step-axis values before any --index selection so
        # panel titles can use the axis's own spacing.
        native_step_dim = _step_dim(da)
        native_steps = list(da[native_step_dim].values) if native_step_dim else None
        # Structural checks come first, so an unknown dim name or a list on a
        # non-panel dim is reported before any positional (out-of-range/alias)
        # validation of the same spec.
        for dim, idx in overrides.items():
            if dim not in da.dims:
                raise UsageError(
                    f"--index dimension {dim!r} is not in the data (dims: {list(da.dims)})"
                )
            if isinstance(idx, list) and dim != native_step_dim:
                panel_desc = repr(native_step_dim) if native_step_dim else "step/time"
                raise UsageError(
                    f"--index list selection on {dim!r} is only supported "
                    f"on the panel dimension ({panel_desc}); give a single position"
                )
        for dim, idx in overrides.items():
            size = da.sizes[dim]
            positions = idx if isinstance(idx, list) else [idx]
            # Normalize negative positions against the dim size so the
            # uniqueness check sees the element each position addresses
            # (0 and -size alias the same element).
            seen = {}
            for pos in positions:
                if not -size <= pos < size:
                    raise UsageError(
                        f"--index position {pos} is out of range for "
                        f"dimension {dim!r} (size {size})"
                    )
                norm = pos % size
                if norm in seen:
                    raise UsageError(
                        f"--index positions {seen[norm]} and {pos} address "
                        f"the same element of dimension {dim!r} (size {size})"
                    )
                seen[norm] = pos
            da = da.isel({dim: idx}, drop=True)
        # After the overrides the array must be (panel?, lat, lon) — plus an
        # ensemble 'number' dim, which _heatmap averages. Catch selections
        # that break that contract here, before the bbox/mask block operates
        # on the spatial dims by name.
        for spatial in (lat_dim, lon_dim):
            if spatial in overrides and spatial not in da.dims:
                raise UsageError(
                    f"--index removed the {spatial!r} dimension; heatmap needs a 2D lat/lon grid"
                )
        panel_dim = _step_dim(da)
        # Any dim still present beyond the panel dim, the ensemble 'number'
        # dim, and lat/lon would reach the per-panel transpose with too many
        # axes; require the user to select a position from it.
        for dim in da.dims:
            if dim not in (panel_dim, "number", lat_dim, lon_dim):
                panel_desc = repr(panel_dim) if panel_dim else "step/time"
                raise UsageError(
                    f"dimension {dim!r} remains after selection; heatmap "
                    f"panels only the {panel_desc} dimension — select a position "
                    f"from {dim!r} with --index"
                )
        if panel_dim is not None and da.sizes[panel_dim] == 0:
            raise UsageError(f"dimension {panel_dim!r} has size 0; nothing to plot.")
        extent_vals = _parse_extent(extent)
        cities_map = _parse_cities(cities)
        cmap = _parse_colormap(colormap)
        region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
        wrapped_bbox = bbox_nwse is not None and bbox_nwse[1] > bbox_nwse[3]
        if bbox_nwse is not None or region_polygon is not None:
            import numpy as _np_local

            # Wrap 0..360 lons to [-180, 180] before slicing/masking so a global
            # grid still intersects a negative-lon bbox (e.g. Senegal) and the
            # polygon coords (which live in [-180, 180]) align with the grid.
            # Mirror plot-compare's pre-slice wrap.
            lon_vals_pre = _np_local.asarray(da[lon_dim].values)
            if lon_vals_pre.size and float(_np_local.nanmax(lon_vals_pre)) > 180.0:
                da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
            if bbox_nwse is not None:
                r_n, r_w, r_s, r_e = bbox_nwse
                lat_vals_pre = da[lat_dim].values
                da = da.sel({lat_dim: lat_slice(lat_vals_pre, r_n, r_s)})
                # A bbox with west > east is an RFC 7946 antimeridian-crossing
                # box (e.g. Russia from resolve-region): select the two lon bands
                # lon >= r_w OR lon <= r_e rather than the empty slice(r_w, r_e).
                # The wrapped-bbox lon shift happens after the polygon mask below
                # so the mask still operates in the [-180, 180] frame.
                if r_w > r_e:
                    da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
                else:
                    da = da.sel({lon_dim: slice(r_w, r_e)})
            # Polygon-mask in the [-180, 180] frame: cells whose centers fall
            # outside the boundary become NaN. Done before any wrapped-bbox lon
            # shift so the polygon coords match the grid. Mirrors plot-compare.
            if region_polygon is not None:
                import shapely

                post_lat = da[lat_dim].values
                post_lon = da[lon_dim].values
                lon_grid, lat_grid = _np_local.meshgrid(post_lon, post_lat)
                mask = shapely.contains_xy(region_polygon, lon_grid, lat_grid)
                if not bool(mask.any()):
                    print(
                        "Warning: --mask-geojson polygon does not intersect the grid; "
                        "the map will be entirely empty.",
                        file=sys.stderr,
                    )
                mask_da = xr.DataArray(mask, dims=(lat_dim, lon_dim))
                da = da.where(mask_da)
            if bbox_nwse is not None:
                if r_w > r_e:
                    # The selected data now lives in two disjoint bands (lon >= r_w
                    # and lon <= r_e). Remap longitudes so the two bands form one
                    # contiguous block: values below r_w wrap up by 360, giving an
                    # axis spanning [r_w, r_e + 360]. Sort so the axis is monotonic.
                    # Coastlines/borders are drawn in PlateCarree, so a shifted axis
                    # spanning >180 is the standard way to render an antimeridian-
                    # crossing region.
                    shifted = ((da[lon_dim] - r_w) % 360.0) + r_w
                    da = da.assign_coords({lon_dim: shifted}).sortby(lon_dim)
                if extent_vals is None:
                    # Frame the country. For a wrapped bbox the x-range spans
                    # [r_w, r_e + 360] (a valid x0 < x1) so the contiguous shifted
                    # block is framed, not the empty complementary band.
                    if r_w > r_e:
                        extent_vals = [float(r_w), float(r_e) + 360.0, float(r_s), float(r_n)]
                    else:
                        extent_vals = [float(r_w), float(r_e), float(r_s), float(r_n)]
        # Guard at the first point where a zero-size grid is knowable (after
        # --index/--bbox selection, before any min/max reduction): disjoint
        # selections would otherwise hit reductions of an empty array.
        if da.sizes[lat_dim] == 0 or da.sizes[lon_dim] == 0:
            raise UsageError(
                "selection produced an empty grid (no cells remain after "
                "--index/--bbox selection); nothing to plot."
            )
        fig = _heatmap(
            da,
            lat_dim,
            lon_dim,
            cmap,
            extent_vals,
            cities_map,
            title,
            fontsize,
            wrap_lon=not wrapped_bbox,
            native_step_dim=native_step_dim,
            native_steps=native_steps,
        )
    else:
        fig, ax = plt.subplots(figsize=(10, 6))
        sdim = "step" if "step" in da.dims else cf_dim(da, "time")
        if sdim is None:
            raise UsageError(f"timeseries needs 'step' or 'time'; got {list(da.dims)}.")
        reduce_dims = [d for d in da.dims if d != sdim]
        da.mean(reduce_dims).plot(ax=ax)
        ax.set_xlabel(sdim)
        ax.set_title(title or f"{variable} ({style})")
        fig.tight_layout()

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output

if __name__ == "__main__":
    plot()
