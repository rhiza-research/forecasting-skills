# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cartopy",
#   "cf-xarray",
#   "geopandas>=1",
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
import hashlib
import json
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.4"

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


def _hash_zarr(zarr_path: Path) -> str:
    """Stable content hash of a zarr's stored bytes. Walks the zarr dir
    deterministically and hashes relative-path bytes + each file's
    content. Returns sha256 hex digest."""
    h = hashlib.sha256()
    for p in sorted(zarr_path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(zarr_path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


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
    ``YYYY-MM-DD to YYYY-MM-DD`` with ``t`` interpreted as the bin's
    right edge (the right-edge label convention used by
    ``aggregate-temporal`` and ``deaccumulate``). The displayed range
    is inclusive-both-ends in daily granularity: ``start = end -
    bin_width + 1 day``, so a 10-day dekad ending ``2026-05-09``
    renders as ``"2026-04-30 to 2026-05-09"`` (10 days inclusive),
    matching upstream pipelines. For sub-daily ``bin_width`` the +1
    day adjustment is approximate; see deferred-work.md. When
    ``bin_width`` is None, fall back to a single ISO date.
    """
    import pandas as pd

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


def _lat_slice(lat_vals, north, south):
    """Return a ``slice`` for ``ds.sel`` that works for ascending or descending lat."""
    if lat_vals.size and lat_vals[0] > lat_vals[-1]:
        return slice(north, south)
    return slice(south, north)


def _polygon_from_geojson(path):
    """Return the unioned shapely polygon from a GeoJSON file.

    Used to polygon-clip gridded satellite data so cells outside the
    boundary are NaN'd before plotting (matching upstream's country-shaped
    sat rendering). Unions every feature's geometry in the file. Exits
    non-zero if the file is missing or has no usable geometry.
    """
    p = Path(path)
    if not p.exists():
        print(f"Error: --mask-geojson file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: could not read --mask-geojson {path}: {exc}", file=sys.stderr)
        sys.exit(2)

    if data.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in data.get("features", []) if f.get("geometry")]
    elif data.get("type") == "Feature":
        geoms = [data["geometry"]] if data.get("geometry") else []
    else:
        # A bare geometry object.
        geoms = [data]

    if not geoms:
        print(f"Error: --mask-geojson {path} has no usable geometry.", file=sys.stderr)
        sys.exit(2)

    from shapely.geometry import shape
    from shapely.ops import unary_union

    return unary_union([shape(g) for g in geoms])


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


def _attach_bbox_value(argv):
    # argparse rejects a space-separated --bbox value that starts with '-'
    # (a bbox whose North latitude is negative). Rewrite `--bbox VAL` to
    # `--bbox=VAL` so both the space and equals forms parse.
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--bbox" and i + 1 < len(argv):
            out.append(f"--bbox={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


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
        "--bbox",
        help="N/W/S/E decimal degrees. Slices gridded inputs to the bbox, drops "
        "stations outside the bbox, and sets axes to the bbox. Cells inside the "
        "bbox but outside any --mask-geojson polygon are kept (rectangular "
        "slice). Use the resolve-region skill to get a country's bbox.",
    )
    p.add_argument(
        "--mask-geojson",
        help="Path to a GeoJSON boundary polygon. Gridded cells outside the "
        "polygon are set to NaN before plotting. Use resolve-region's --geojson "
        "output to produce a country polygon.",
    )
    args = p.parse_args(_attach_bbox_value(sys.argv[1:]))

    if len(args.input) != 2:
        print(
            f"Error: --input must be passed exactly twice; got {len(args.input)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    path_a, path_b = args.input
    label_a = Path(path_a).name
    label_b = Path(path_b).name

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

    # Shared-bin panel selection. The two rows share one colormap and render the
    # same time window per panel, so the inputs must be at the same time
    # resolution and we compare them on the bins they have in COMMON (their
    # intersection), not positionally. This tolerates a reporting-latency offset
    # — e.g. a station whose trailing bin is empty and dropped — which leaves the
    # inputs the same resolution but at a different last label; that just yields
    # one fewer common bin instead of a misalignment error.
    raw_a = ds_a[td_a].values
    raw_b = ds_b[td_b].values

    def _axis_kind(values):
        """Classify a time axis as 'datetime', 'timedelta', or None.

        A forecast `step` axis is `timedelta64`; a calendar time axis is
        `datetime64`. The two are distinct kinds and must not be cross-cast:
        viewing a `timedelta64` array as `datetime64` reinterprets the same
        integer counts against the Unix epoch and would let mismatched step
        axes compare as equal. None means the axis is neither kind.
        """
        kind = getattr(values.dtype, "kind", None)
        if kind == "M":
            return "datetime"
        if kind == "m":
            return "timedelta"
        return None

    kind_a = _axis_kind(raw_a)
    kind_b = _axis_kind(raw_b)
    if kind_a is None or kind_b is None or kind_a != kind_b:
        print(
            "Error: the two inputs have different time resolutions "
            f"('{td_a}' dtype={raw_a.dtype}, '{td_b}' dtype={raw_b.dtype}); "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Normalize each axis to integer nanoseconds WITHIN its own kind (never
    # cross-cast). `datetime64[ns]` for a calendar axis, `timedelta64[ns]` for a
    # step axis; both then view as int64 ns counts for arithmetic.
    ns_dtype = "datetime64[ns]" if kind_a == "datetime" else "timedelta64[ns]"
    int_a = raw_a.astype(ns_dtype).astype("int64")
    int_b = raw_b.astype(ns_dtype).astype("int64")

    def _median_spacing_ns(int_values):
        if int_values.size < 2:
            return None
        # Use absolute diffs so the spacing is independent of storage order: a
        # descending axis would otherwise yield a negative median and make an
        # ascending vs descending pair of the same resolution look different.
        return float(np.median(np.abs(np.diff(int_values))))

    width_a = _median_spacing_ns(int_a)
    width_b = _median_spacing_ns(int_b)

    # Resolution check: the two axes must share a median bin width. When an axis
    # has a single value its spacing is unknown, so the check only runs when both
    # axes have a measurable spacing; a single-bin overlapping input is matched on
    # its label alone below.
    known_widths = [w for w in (width_a, width_b) if w is not None]
    if len(known_widths) == 2:
        rel = abs(width_a - width_b) / max(width_a, width_b, 1.0)
        if rel > 1e-3:
            print(
                "Error: the two inputs have different time resolutions "
                f"(median bin width '{td_a}'≈{width_a:.0f} ns vs "
                f"'{td_b}'≈{width_b:.0f} ns); aggregate both inputs to a common "
                "resolution first, e.g. with the aggregate-temporal skill.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Overlap: intersect the two axes' labels, matched within a 1-second
    # tolerance to absorb storage/rounding; bins on a shared anchor are exactly
    # equal. The same tolerance is reused for the later `.sel`, so the match and
    # the selection cannot disagree. Build the common labels in chronological
    # order, taken from input A's own integer labels so a later `.sel` is exact.
    tol_ns = 1_000_000_000
    sorted_b = np.sort(int_b)
    common_int = []
    for va in np.sort(int_a):
        idx = np.searchsorted(sorted_b, va)
        nearest = None
        for cand in (idx - 1, idx):
            if 0 <= cand < sorted_b.size:
                d = abs(int(sorted_b[cand]) - int(va))
                if nearest is None or d < nearest:
                    nearest = d
        if nearest is not None and nearest <= tol_ns:
            common_int.append(int(va))

    if not common_int:
        print(
            f"Error: no overlapping time bins between the two inputs on '{td_a}'/'{td_b}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    n = min(args.panels, len(common_int))
    # Take the last `n` common labels (chronological). Map each back to a native
    # axis label drawn from input A's coord so per-row `.sel` is exact for A; for
    # B we select with method="nearest" within the same tolerance.
    common_int_last = common_int[-n:]
    common_labels = np.asarray(common_int_last, dtype="int64").astype(ns_dtype)
    common_tol = np.timedelta64(1, "s")

    da_a = ds_a[variable]
    da_b = ds_b[variable]

    # Input-units check. The two rows share one colormap and normalization, so
    # if the inputs hold the variable in different units the panels are colored
    # on a single scale that does not correspond to either input's quantity,
    # making the comparison misleading. This only affects the rendering, so warn
    # and proceed. Compare only when both inputs carry a string `units` attr; a
    # missing or non-string value can't be checked. Units are compared after
    # stripping surrounding whitespace so a trailing space is not read as a real
    # difference.
    units_a = da_a.attrs.get("units")
    units_b = da_b.attrs.get("units")
    if isinstance(units_a, str) and isinstance(units_b, str) and units_a.strip() != units_b.strip():
        print(
            f"Warning: variable '{variable}' has differing units between the "
            f"inputs ({label_a} units={units_a!r}, {label_b} units={units_b!r}). "
            f"The two rows are drawn on one shared color scale, so values in "
            f"different units are not directly comparable in this figure.",
            file=sys.stderr,
        )

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

    # When ``--bbox`` is set: slice gridded inputs to the rectangular bbox and
    # drop station inputs whose (lon, lat) is outside the bbox. This matches
    # upstream's ``ds.sel(longitude=slice, latitude=slice)`` rendering — the
    # bbox alone is a rectangle, not a country shape.
    region_bbox = None
    if args.bbox:
        try:
            region_bbox = tuple(float(x) for x in args.bbox.split("/"))
            if len(region_bbox) != 4:
                raise ValueError
        except ValueError:
            print("Error: --bbox must be N/W/S/E (decimal degrees).", file=sys.stderr)
            sys.exit(2)
    # When ``--mask-geojson`` is set: polygon-clip gridded inputs (cells outside
    # the polygon render as NaN/white), matching upstream sheerwater's
    # clip_region which polygon-clips IMERG/CHIRPS before plotting.
    region_polygon = _polygon_from_geojson(args.mask_geojson) if args.mask_geojson else None

    if region_bbox is not None or region_polygon is not None:
        r_n, r_w, r_s, r_e = region_bbox if region_bbox is not None else (None, None, None, None)
        for side, ds_label in (("a", label_a), ("b", label_b)):
            ds = ds_a if side == "a" else ds_b
            da = da_a if side == "a" else da_b
            if _is_station(ds):
                if region_bbox is not None:
                    lons = ds["longitude"].values
                    lats = ds["latitude"].values
                    # west > east is an RFC 7946 antimeridian-crossing bbox:
                    # keep stations in either lon band (lon >= r_w OR lon <= r_e).
                    if r_w > r_e:
                        lon_keep = (lons >= r_w) | (lons <= r_e)
                    else:
                        lon_keep = (lons >= r_w) & (lons <= r_e)
                    keep = lon_keep & (lats >= r_s) & (lats <= r_n)
                    keep_ids = ds["station_id"].values[keep]
                    if len(keep_ids) == 0:
                        print(
                            f"Warning: 0 stations inside --bbox {args.bbox} "
                            f"on input '{ds_label}'; scatter will render empty.",
                            file=sys.stderr,
                        )
                    ds = ds.sel(station_id=keep_ids)
                    da = da.sel(station_id=keep_ids)
                    # For a wrapped (west > east) bbox, shift kept stations into
                    # the same contiguous [r_w, r_e + 360] frame the gridded data
                    # is remapped to below, so the scatter lands inside the shared
                    # x-limits instead of being clipped on the eastern band.
                    if r_w > r_e:
                        shifted_lon = ((ds["longitude"].values - r_w) % 360.0) + r_w
                        ds = ds.assign_coords(longitude=("station_id", shifted_lon))
            else:
                lat_dim = _cf_dim(da, "latitude")
                lon_dim = _cf_dim(da, "longitude")
                if lat_dim is not None and lon_dim is not None:
                    # Wrap lon to [-180, 180] before the slice so a 0..360
                    # grid still intersects the bbox. Applied to both ds and
                    # da so downstream _ax_bounds and overlay clip see the
                    # same convention.
                    lon_vals = da[lon_dim].values
                    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
                        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(
                            lon_dim
                        )
                        da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(
                            lon_dim
                        )
                    if region_bbox is not None:
                        lat_vals = da[lat_dim].values
                        lat_sl = _lat_slice(lat_vals, r_n, r_s)
                        ds = ds.sel({lat_dim: lat_sl})
                        da = da.sel({lat_dim: lat_sl})
                        # west > east is an RFC 7946 antimeridian-crossing bbox:
                        # select the two lon bands lon >= r_w OR lon <= r_e
                        # rather than the empty slice(r_w, r_e). The grid is
                        # already in the [-180, 180] frame here, so the polygon
                        # mask below still operates in that frame.
                        if r_w > r_e:
                            ds = ds.where((ds[lon_dim] >= r_w) | (ds[lon_dim] <= r_e), drop=True)
                            da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
                        else:
                            ds = ds.sel({lon_dim: slice(r_w, r_e)})
                            da = da.sel({lon_dim: slice(r_w, r_e)})
                    # Polygon-mask gridded sat data to match upstream's
                    # clip_region-clipped IMERG/CHIRPS (NaN outside polygon).
                    # Done in the [-180, 180] frame, before any wrapped-bbox
                    # lon shift below, so the polygon coords match the grid.
                    if region_polygon is not None:
                        import shapely
                        import xarray as _xr

                        post_lat = da[lat_dim].values
                        post_lon = da[lon_dim].values
                        lon_grid, lat_grid = np.meshgrid(post_lon, post_lat)
                        mask = shapely.contains_xy(region_polygon, lon_grid, lat_grid)
                        mask_da = _xr.DataArray(mask, dims=(lat_dim, lon_dim))
                        da = da.where(mask_da)
                    # For a wrapped (west > east) bbox the two selected lon bands
                    # are disjoint in [-180, 180]. Remap lon to a contiguous frame
                    # spanning [r_w, r_e + 360] (values below r_w wrap up by 360)
                    # and sort, so set_xlim(r_w, r_e + 360) frames the country as
                    # one block instead of an inverted axis over the empty band.
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
                        f"dims; --bbox {args.bbox} slice not applied.",
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

    # Decide row layout: when exactly one is station-schema, put it on top.
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
    wrapped_bbox = region_bbox is not None and region_bbox[1] > region_bbox[3]
    if region_bbox is not None:
        r_n, r_w, r_s, r_e = region_bbox
        # For a wrapped bbox the grid was remapped to a contiguous [r_w, r_e+360]
        # frame above; the x-limits span that same range (a valid x0 < x1).
        g_xmin, g_ymin, g_ymax = r_w, r_s, r_n
        g_xmax = r_e + 360.0 if wrapped_bbox else r_e
    gridded_bbox = (g_xmin, g_ymin, g_xmax, g_ymax)

    # For a wrapped bbox the clip box would live in the contiguous [r_w, r_e+360]
    # frame while the admin geometries are still in [-180, 180], so a bbox clip
    # would drop the eastern band. Load unclipped and rely on the shift + xlim
    # below to frame them; matplotlib clips anything outside the limits.
    boundaries = _load_admin_boundaries(bbox=None if wrapped_bbox else gridded_bbox)
    if boundaries is not None and wrapped_bbox:
        # Admin geometries are in [-180, 180]; shift longitudes below r_w up by
        # 360 so they line up with the contiguous, remapped grid frame. Uses the
        # shapely 2.x vectorized transform: the callback receives an (N, 2) array
        # of coordinates and returns the same shape.
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
        ds, da, td, label = row
        is_station = _is_station(ds)
        # Select this row at the SHARED common labels (same time window per panel
        # across both rows). `common_labels` were derived from input A's axis; for
        # either row select by nearest within the matching tolerance so a sub-bin
        # float offset still lands on the right bin. Both axes passed the
        # resolution + overlap checks, so every common label exists in each row.
        row_sel = da.sel({td: common_labels}, method="nearest", tolerance=common_tol)
        # Render the panel-title range from this row's own time spacing, so a
        # bin coord interpreted as the inclusive right edge shows `start to end`
        # (start = end - bin_width + 1 day). None for a single-step axis.
        bin_width = _median_bin_width(da[td].values)
        last_im = None
        for col in range(n_panels):
            ax = axes[col]
            sel = row_sel.isel({td: col})
            t_val = row_sel[td].values[col]
            title_t = _format_single(t_val, bin_width=bin_width)
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

    src_a = Path(path_a)
    src_b = Path(path_b)
    upstream_a = _load_history(src_a)
    upstream_b = _load_history(src_b)
    shared_args = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    version = _RHIZA_SKILL_VERSION
    plot_compare_entry_a = {
        "skill": "plot-compare",
        "version": version,
        "args": shared_args,
        "input": {"basename": src_a.name, "hash": _hash_zarr(src_a)},
    }
    plot_compare_entry_b = {
        "skill": "plot-compare",
        "version": version,
        "args": shared_args,
        "input": {"basename": src_b.name, "hash": _hash_zarr(src_b)},
    }
    if not upstream_a:
        print(
            f"Warning: no upstream rhiza_history on {src_a.name}; embedding plot-compare step alone.",
            file=sys.stderr,
        )
    if not upstream_b:
        print(
            f"Warning: no upstream rhiza_history on {src_b.name}; embedding plot-compare step alone.",
            file=sys.stderr,
        )
    fig.savefig(
        out,
        dpi=150,
        bbox_inches="tight",
        metadata={
            "rhiza_history_a": json.dumps(upstream_a + [plot_compare_entry_a], sort_keys=True),
            "rhiza_history_b": json.dumps(upstream_b + [plot_compare_entry_b], sort_keys=True),
            "Software": "forecasting-skills",
        },
    )
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
