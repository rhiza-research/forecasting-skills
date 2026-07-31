# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
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
#   "cf-units>=3.3",
# ]
# ///
"""Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs.

Top row is dataset A, bottom row is dataset B. When exactly one of A/B
is a point_obs Zarr, it is placed on the top row to match the
canonical "stations vs. satellite" presentation. Each row can draw its
own variable (--variable-a/-b). The color scale is shared across both
rows when they are the same variable with matching units, and per-row
independent otherwise (--shared-scale / --independent-scale override).
"""

import sys
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.dataset import auto_variable, cf_dim, lat_slice, polygon_from_geojson
from weather_skills_core.units import to_standard_units, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.16"

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

    A cftime right edge (a non-standard model calendar, where ``t`` is an
    object with a ``.calendar`` attribute and ``bin_width`` is a
    ``datetime.timedelta``) is rendered the same way using calendar-aware
    ``timedelta`` arithmetic, so a ``noleap``/``360_day`` bin gets a correct
    ``YYYY-MM-DD to YYYY-MM-DD`` label instead of degrading to ``str(t)``.
    """
    import datetime as _dt

    import pandas as pd

    if hasattr(t, "calendar"):
        # cftime right edge.
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
    except Exception as exc:  # noqa: BLE001 -- optional admin overlay; warn and skip if unavailable
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
            except Exception:  # noqa: BLE001 -- fall back to shapely intersection when .clip is unavailable
                # Older geopandas may not expose .clip cleanly; fall back
                # to a shapely intersection on each geometry.
                gdf = gdf.copy()
                gdf["geometry"] = gdf.geometry.intersection(clip_geom)
            gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
        except Exception as exc:  # noqa: BLE001 -- optional bbox clip; warn and draw unclipped on failure
            print(
                f"Warning: could not clip admin boundaries to bbox ({exc}); "
                "drawing unclipped overlay.",
                file=sys.stderr,
            )
    return gdf

def _median_bin_width(time_values):
    """Return median spacing of a 1-D time coord.

    For a datetime64/timedelta64 axis the result is a ``pandas.Timedelta``.
    For an object-dtype cftime axis (a non-standard model calendar) the
    result is a ``datetime.timedelta``, computed from the cftime objects'
    own differences so a ``noleap``/``360_day`` axis yields a correct
    spacing instead of ``None``. Returns ``None`` when the array has fewer
    than two values or cannot be coerced to datetimes.
    """
    import numpy as np
    import pandas as pd

    arr = np.asarray(time_values)
    if arr.size < 2:
        return None
    if arr.dtype.kind == "O" and hasattr(arr.flat[0], "calendar"):
        # cftime axis: subtracting two cftime objects yields a
        # datetime.timedelta; take the median of the positive day-deltas.
        ordered = np.sort(arr)
        deltas = [
            abs((ordered[i + 1] - ordered[i]).total_seconds()) for i in range(ordered.size - 1)
        ]
        if not deltas:
            return None
        import datetime as _dt

        return _dt.timedelta(seconds=float(np.median(deltas)))
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
    lat_dim = cf_dim(sel, "latitude")
    lon_dim = cf_dim(sel, "longitude")
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
        lat_dim = cf_dim(ds[variable], "latitude")
        lon_dim = cf_dim(ds[variable], "longitude")
        lons = ds[lon_dim].values
        lats = ds[lat_dim].values
    import numpy as np

    return (
        float(np.nanmin(lons)),
        float(np.nanmax(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lats)),
    )

def _dataset_label(ds, fallback):
    src = ds.attrs.get("weather_skills_source")
    if isinstance(src, str) and src.strip():
        return Path(src).stem
    return fallback

@weather_skill(
    name="plot-compare",
    version=_SKILL_VERSION,
    inputs=["any", "any"],
    outputs=["figure"]
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
            "--variable-a",
            default=None,
            help="Variable for row A. Overrides --variable for that row. "
            "Default: --variable, else first real data var of input A.",
        )
@weather_skill.argument(
            "--variable-b",
            default=None,
            help="Variable for row B. Overrides --variable for that row. "
            "Default: --variable, else first real data var of input B.",
        )
@weather_skill.argument(
            "--colormap",
            default=None,
            help="matplotlib colormap name. In shared-scale mode, when omitted the "
            "categorical precipitation colormap with BoundaryNorm is used. In "
            "independent-scale mode it is the per-row default (falls back to "
            "'viridis'); --colormap-a/-b override it per row.",
        )
@weather_skill.argument(
            "--colormap-a",
            default=None,
            help="matplotlib colormap for row A in independent-scale mode "
            "(precedence: --colormap-a, then --colormap, then 'viridis').",
        )
@weather_skill.argument(
            "--colormap-b",
            default=None,
            help="matplotlib colormap for row B in independent-scale mode "
            "(precedence: --colormap-b, then --colormap, then 'viridis').",
        )
@weather_skill.argument(
            "--shared-scale",
            action="store_true",
            help="Force one shared color scale across both rows. Default: shared "
            "when both rows resolve to the same variable AND matching units, else "
            "independent per-row scales.",
        )
@weather_skill.argument(
            "--independent-scale",
            action="store_true",
            help="Force per-row color scales (each row its own vmin/vmax/colorbar). "
            "Default: independent unless both rows are the same variable + units.",
        )
@weather_skill.argument("--panels", type=int, default=3)
@weather_skill.argument("--time-dim", default=None, help="Override the time axis. Defaults to time, else step.")
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
            "--mask-geojson",
            default=None,
            help="Path to a GeoJSON boundary polygon. Gridded cells outside the "
            "polygon are set to NaN before plotting. Use resolve-region's --geojson "
            "output to produce a country polygon.",
        )
def plot_compare(
    ds_a,
    ds_b,
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
    """Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs.

    Top row is dataset A, bottom row is dataset B. When exactly one of A/B
    is a point_obs Zarr, it is placed on the top row to match the
    canonical "stations vs. satellite" presentation. Each row can draw its
    own variable (--variable-a/-b). The color scale is shared across both
    rows when they are the same variable with matching units, and per-row
    independent otherwise (--shared-scale / --independent-scale override).
    """
    if shared_scale and independent_scale:
        raise UsageError("--shared-scale and --independent-scale are mutually exclusive.")

    label_a = _dataset_label(ds_a, "A")
    label_b = _dataset_label(ds_b, "B")

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
    from matplotlib.gridspec import GridSpec

    # Per-row variable resolution: explicit per-row flag, then the shared
    # --variable, then that input's own first real (non-CRS) data var.
    var_a = variable_a or variable or auto_variable(ds_a)
    var_b = variable_b or variable or auto_variable(ds_b)
    for side, var, ds in (("A", var_a, ds_a), ("B", var_b, ds_b)):
        if var is None or var not in ds:
            # Same real-data-var criterion core's auto_variable uses: skip CF
            # grid-mapping (CRS) container vars so the hint lists only real vars.
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

    ds_a = to_standard_units(ds_a, variables=[var_a])
    ds_b = to_standard_units(ds_b, variables=[var_b])

    td_a = _pick_time_dim(ds_a, time_dim)
    td_b = _pick_time_dim(ds_b, time_dim)
    if td_a is None or td_b is None:
        raise UsageError(
            f"both inputs need a time/step dim. A: {list(ds_a.dims)}  B: {list(ds_b.dims)}"
        )

    # Shared-bin panel selection. The two rows share one colormap and render the
    # same time window per panel, so the inputs must be at the same time
    # resolution and we compare them on the bins they have in COMMON (their
    # intersection), not positionally. This tolerates a reporting-latency offset
    # — e.g. a station whose trailing bin is empty and dropped — which leaves the
    # inputs the same resolution but at a different last label; that just yields
    # one fewer common bin instead of a misalignment error.
    raw_a = ds_a[td_a].values
    raw_b = ds_b[td_b].values

    def _is_cftime_axis(values):
        """True when ``values`` is an object array of cftime datetimes.

        A valid CF Zarr whose time axis uses a non-standard model calendar
        (`noleap`, `360_day`) decodes to object-dtype cftime datetimes, which
        carry a `.calendar` attribute. Such an axis is a calendar/datetime
        axis even though its dtype kind is `O`, not `M`.
        """
        return (
            getattr(values.dtype, "kind", None) == "O"
            and values.size > 0
            and hasattr(np.asarray(values).flat[0], "calendar")
        )

    def _axis_kind(values):
        """Classify a time axis as 'datetime', 'timedelta', or None.

        A forecast `step` axis is `timedelta64`; a calendar time axis is
        `datetime64` (standard calendar) or object-dtype cftime (a non-standard
        model calendar such as `noleap`/`360_day`). The datetime and timedelta
        kinds are distinct and must not be cross-cast: viewing a `timedelta64`
        array as `datetime64` reinterprets the same integer counts against the
        Unix epoch and would let mismatched step axes compare as equal. None
        means the axis is neither kind.
        """
        kind = getattr(values.dtype, "kind", None)
        if kind == "M":
            return "datetime"
        if kind == "m":
            return "timedelta"
        if _is_cftime_axis(values):
            return "datetime"
        return None

    kind_a = _axis_kind(raw_a)
    kind_b = _axis_kind(raw_b)
    if kind_a is None or kind_b is None or kind_a != kind_b:
        raise DataError(
            "the two inputs have different time resolutions "
            f"('{td_a}' dtype={raw_a.dtype}, '{td_b}' dtype={raw_b.dtype}); "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill."
        )

    # Calendar-compatibility guards, before any cross-axis encoding. A cftime
    # (model-calendar) axis and a datetime64 (standard-calendar) axis cannot be
    # placed on a shared numeric line, and two different model calendars do not
    # agree on which dates exist; either case would produce silently-wrong
    # overlap math. Require the user to put both inputs on a common calendar
    # first with the convert-calendar skill.
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
    if a_is_cftime and b_is_cftime:
        cal_a = raw_a.flat[0].calendar
        cal_b = raw_b.flat[0].calendar
        if cal_a != cal_b:
            raise DataError(
                "the two inputs use different model calendars "
                f"('{td_a}' calendar={cal_a!r} vs '{td_b}' calendar={cal_b!r}); "
                "convert both to a common calendar first with the "
                "convert-calendar skill."
            )

    # Encode each axis to a 1-D numeric array in a single consistent unit, never
    # cross-casting between the datetime and timedelta kinds. For a cftime
    # calendar axis, encode to float DAYS-since-epoch with that axis's own
    # calendar (`astype("datetime64[ns]")` cannot represent a noleap/360_day
    # date), and carry a tolerance and back-mapping in days. For datetime64 /
    # timedelta64 axes, keep the exact integer-nanoseconds encoding.
    cftime_axes = _is_cftime_axis(raw_a) and _is_cftime_axis(raw_b)
    if cftime_axes:
        import cftime

        _epoch = "days since 1970-01-01"
        enc_a = np.asarray(
            cftime.date2num(raw_a, units=_epoch, calendar=raw_a.flat[0].calendar),
            dtype="float64",
        )
        enc_b = np.asarray(
            cftime.date2num(raw_b, units=_epoch, calendar=raw_b.flat[0].calendar),
            dtype="float64",
        )
        # 1 second expressed in the float-days unit, matching the datetime64
        # path's 1-second match/selection tolerance.
        tol_enc = 1.0 / 86400.0
    else:
        # `datetime64[ns]` for a calendar axis, `timedelta64[ns]` for a step
        # axis; both then view as int64 ns counts for arithmetic.
        ns_dtype = "datetime64[ns]" if kind_a == "datetime" else "timedelta64[ns]"
        enc_a = raw_a.astype(ns_dtype).astype("int64")
        enc_b = raw_b.astype(ns_dtype).astype("int64")
        tol_enc = 1_000_000_000

    def _median_spacing(enc_values):
        if enc_values.size < 2:
            return None
        # Use absolute diffs so the spacing is independent of storage order: a
        # descending axis would otherwise yield a negative median and make an
        # ascending vs descending pair of the same resolution look different.
        return float(np.median(np.abs(np.diff(enc_values))))

    width_a = _median_spacing(enc_a)
    width_b = _median_spacing(enc_b)

    # Resolution check: the two axes must share a median bin width. When an axis
    # has a single value its spacing is unknown, so the check only runs when both
    # axes have a measurable spacing; a single-bin overlapping input is matched on
    # its label alone below.
    known_widths = [w for w in (width_a, width_b) if w is not None]
    if len(known_widths) == 2:
        rel = abs(width_a - width_b) / max(width_a, width_b, 1.0)
        if rel > 1e-3:
            # cftime widths are float days; datetime64/timedelta64 widths are
            # integer nanosecond counts. Format each in its own unit so the ns
            # path prints whole counts rather than scientific notation.
            if cftime_axes:
                wa_str = f"{width_a:.4g} days"
                wb_str = f"{width_b:.4g} days"
            else:
                wa_str = f"{width_a:.0f} ns"
                wb_str = f"{width_b:.0f} ns"
            raise DataError(
                "the two inputs have different time resolutions "
                f"(median bin width '{td_a}'≈{wa_str} vs "
                f"'{td_b}'≈{wb_str}); aggregate both inputs to a common "
                "resolution first, e.g. with the aggregate-temporal skill."
            )

    # Overlap: intersect the two axes' labels, matched within a 1-second
    # tolerance to absorb storage/rounding; bins on a shared anchor are exactly
    # equal. The same tolerance is reused for the later `.sel`, so the match and
    # the selection cannot disagree. Build the common labels in chronological
    # order, taken from input A's own labels so a later `.sel` is exact. Track
    # the source index in A's (sorted) axis so a cftime axis can map back to the
    # native cftime object — its encoded float is not a `.sel` label.
    order_a = np.argsort(enc_a, kind="stable")
    sorted_enc_a = enc_a[order_a]
    sorted_enc_b = np.sort(enc_b)
    common_enc = []
    common_src = []
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
    # Take the last `n` common labels (chronological). Map each back to a native
    # axis label drawn from input A's coord so per-row `.sel` is exact for A; for
    # B we select with method="nearest" within the same tolerance.
    src_last = common_src[-n:]
    if cftime_axes:
        # Native cftime objects from A's axis; `.sel` with method="nearest" and a
        # datetime.timedelta tolerance works on a cftime index.
        import datetime as _dt

        common_labels = np.asarray(raw_a, dtype=object)[src_last]
        common_tol = _dt.timedelta(seconds=1)
    else:
        common_labels = np.asarray(common_enc[-n:], dtype="int64").astype(ns_dtype)
        common_tol = np.timedelta64(1, "s")

    da_a = ds_a[var_a]
    da_b = ds_b[var_b]

    # Scale-mode decision. SHARED draws both rows on one colormap/norm/vmin/vmax;
    # INDEPENDENT gives each row its own scale and colorbar. Default to SHARED
    # only when the two rows are the same quantity: same variable name AND
    # matching (stripped) units. Either of --shared-scale / --independent-scale
    # forces the mode (argparse already made them mutually exclusive).
    units_a = da_a.attrs.get("units")
    units_b = da_b.attrs.get("units")
    units_match = (
        isinstance(units_a, str)
        and isinstance(units_b, str)
        and units_equal(units_a, units_b)
    )
    if shared_scale:
        use_shared_scale = True
    elif independent_scale:
        use_shared_scale = False
    else:
        use_shared_scale = var_a == var_b and units_match

    # Input-units check (SHARED mode only). When both rows share one colormap and
    # normalization but hold their variable in different units, the panels are
    # colored on a single scale that does not correspond to either input's
    # quantity, making the comparison misleading. This only affects the
    # rendering, so warn and proceed. Compare only when both inputs carry a
    # string `units` attr; a missing or non-string value can't be checked. Units
    # are compared after stripping surrounding whitespace so a trailing space is
    # not read as a real difference. In INDEPENDENT mode each row has its own
    # scale and colorbar, so there is no cross-row units mismatch to warn about.
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
    # The decorator parses --bbox to an (N, W, S, E) float tuple (or None).
    region_bbox = bbox
    # When ``--mask-geojson`` is set: polygon-clip gridded inputs (cells outside
    # the polygon render as NaN/white), matching upstream sheerwater's
    # clip_region which polygon-clips IMERG/CHIRPS before plotting.
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None

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
                            f"Warning: 0 stations inside --bbox {r_n}/{r_w}/{r_s}/{r_e} "
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
                lat_dim = cf_dim(da, "latitude")
                lon_dim = cf_dim(da, "longitude")
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
                        lat_sl = lat_slice(lat_vals, r_n, r_s)
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
                        if not bool(mask.any()):
                            print(
                                f"Warning: --mask-geojson polygon does not intersect "
                                f"input '{ds_label}'; its panel will be entirely empty.",
                                file=sys.stderr,
                            )
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

    # Per-row scale parameters. SHARED mode computes one (cmap, norm, vmin, vmax)
    # used by both rows; INDEPENDENT mode computes them per row from that row's
    # own data, with its own colormap (precedence: --colormap-X, --colormap,
    # "viridis") and a continuous norm. Each row also keeps its variable/units so
    # the colorbar can be labeled "{file} {var} [{units}]" in INDEPENDENT mode.
    def _row_units(da):
        u = da.attrs.get("units")
        return u if isinstance(u, str) else None

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
        scale_a = (shared_cmap, shared_norm, shared_vmin, shared_vmax)
        scale_b = (shared_cmap, shared_norm, shared_vmin, shared_vmax)
    else:
        cmap_a = colormap_a or colormap or "viridis"
        cmap_b = colormap_b or colormap or "viridis"
        scale_a = (
            cmap_a,
            None,
            float(da_a.min().values),
            float(da_a.max().values),
        )
        scale_b = (
            cmap_b,
            None,
            float(da_b.min().values),
            float(da_b.max().values),
        )

    # Per-side render bundles: (ds, da, td, label, variable, units, scale).
    side_a = (ds_a, da_a, td_a, label_a, var_a, _row_units(da_a), scale_a)
    side_b = (ds_b, da_b, td_b, label_b, var_b, _row_units(da_b), scale_b)

    # Decide row layout: when exactly one is point_obs, put it on top.
    if b_station and not a_station:
        top = side_b
        bottom = side_a
    else:
        top = side_a
        bottom = side_b

    fig = plt.figure(figsize=(22, 10))
    gs = GridSpec(2, n, figure=fig, wspace=0.08, hspace=0.15)
    top_axes = [fig.add_subplot(gs[0, i]) for i in range(n)]
    bottom_axes = [fig.add_subplot(gs[1, i]) for i in range(n)]
    if title:
        fig.suptitle(title)

    # The "gridded base" determines both the admin-polygon clip bbox and
    # the shared spatial extent across both rows. Pick whichever of A/B
    # is NOT point_obs; if neither is point_obs, default to A.
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
        ds, da, td, label, _var, _units, scale = row
        cmap, norm, vmin, vmax = scale
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

    def _cbar_label(row):
        # row = (ds, da, td, label, variable, units, scale). In SHARED mode the
        # label stays "{file} {var}" (current behavior); in INDEPENDENT mode it
        # also carries that row's own units as "{file} {var} [{units}]" so each
        # colorbar identifies its own quantity.
        _ds, _da, _td, label, var, units, _scale = row
        if not use_shared_scale and units:
            return f"{label} {var} [{units}]"
        return f"{label} {var}"

    fig.colorbar(
        sc_top,
        ax=top_axes,
        label=_cbar_label(top),
        shrink=0.6,
        fraction=0.02,
        pad=0.02,
    )
    fig.colorbar(
        im_bottom,
        ax=bottom_axes,
        label=_cbar_label(bottom),
        shrink=0.6,
        fraction=0.02,
        pad=0.02,
    )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output

if __name__ == "__main__":
    plot_compare()
