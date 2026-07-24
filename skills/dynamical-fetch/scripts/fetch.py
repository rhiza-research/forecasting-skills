# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "dynamical-catalog==0.5.0",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch a dynamical.org open-catalog dataset and write a weather-skills envelope Zarr."""

import sys
from datetime import date

from weather_skills_core import DataError, UsageError, WroteSummary, weather_skill
from weather_skills_core.dates import np_to_date, parse_token, resolve_date, resolve_window
from weather_skills_core.envelope import stamp_cf_attrs

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"

# Coords dynamical attaches that are not part of the weather-skills envelope: forecast
# bookkeeping (valid_time, *_forecast_length) and the CRS scalar (spatial_ref).
# Dropped on the way out so the output carries only envelope coords.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)


def _open_dataset(state, dataset) -> dict:
    """Validate the dataset id, open it, and detect its shape, at most once per run.

    ``state`` is the run-scoped ``RunContext.state`` dict, shared by the
    `latest` resolution and the fetch body so the dataset is opened at most
    once per run. Lazy, icechunk-backed open: this reads only metadata, so
    shape detection and `latest` resolution run before any array bytes are
    pulled.
    """
    if "ds" not in state:
        import dynamical_catalog

        catalog = dynamical_catalog.list()
        if dataset not in catalog:
            raise UsageError(
                f"unknown dataset {dataset!r}. Available datasets:\n  " + "\n  ".join(catalog)
            )
        ds = dynamical_catalog.open(dataset)

        # Projected grids (e.g. NOAA HRRR on a Lambert Conformal Conic grid) expose
        # 1-D `y`/`x` in meters with 2-D latitude(y,x)/longitude(y,x) and a CRS in
        # `spatial_ref`, not 1-D latitude/longitude dims. A lat/lon bbox on such a
        # grid needs masking over the 2-D coordinate arrays, and a faithful subset
        # stays curvilinear — which the 1-D-lat/lon weather-skills envelope does not model.
        # Converting it to a regular lat/lon grid is a reprojection (a grid
        # transform), which belongs in a dedicated reprojection skill, not in this
        # faithful-I/O fetcher.
        if "latitude" not in ds.dims or "longitude" not in ds.dims:
            raise UsageError(
                f"{dataset} is on a projected grid (dims {tuple(ds.dims)}); this "
                "fetcher only handles regular 1-D latitude/longitude grids. Reprojecting a "
                "projected grid to lat/lon is a grid transform for a dedicated reprojection "
                "skill, not this fetcher."
            )

        # Shape is detected from the dims present, not a hardcoded per-dataset table.
        if "ensemble_member" in ds.dims:
            shape = "ensemble"
        elif "lead_time" in ds.dims:
            shape = "forecast"
        elif "time" in ds.dims:
            shape = "analysis"
        else:
            raise UsageError(
                f"{dataset} has an unrecognized shape (dims {tuple(ds.dims)}); "
                "expected an ensemble/deterministic forecast (lead_time) or an analysis (time)."
            )
        state["ds"] = ds
        state["shape"] = shape
    return state


def _latest_from_dataset(state, dataset) -> date:
    """Newest available date, read cheaply from the opened dataset's own coords.

    Max init for forecasts, max time for analysis.
    """
    state = _open_dataset(state, dataset)
    ds = state["ds"]
    is_forecast = state["shape"] in ("ensemble", "forecast")
    coord = "init_time" if is_forecast else "time"
    vals = ds[coord].values
    if is_forecast:
        # --date selects the 00 UTC init, so `latest` must be the newest
        # date that HAS one. Filter to midnight inits before taking the
        # max so a later same-day cycle (e.g. GFS 18 UTC) doesn't resolve
        # `latest` to a date whose 00 UTC init isn't published yet.
        midnight = vals[vals == vals.astype("datetime64[D]")]
        if midnight.size:
            vals = midnight
    return np_to_date(vals.max())


def _validate_and_resolve(args, context) -> None:
    """Pre-cache-check date resolution onto the argparse namespace.

    The time flags are optional strings here (which of them applies depends on
    the dataset's shape, discovered only after the catalog open), so the date
    grammar is applied through the core resolvers rather than the standard
    toggles. Each provided value is resolved to a concrete ISO date in place,
    so the cache key records resolved dates, never relative tokens. A
    malformed token exits 2 before any network call; `latest` opens the
    catalog dataset lazily, at most once. Which flags the dataset's shape
    actually requires is validated in the body, after the open.
    """

    def latest_fn():
        return _latest_from_dataset(context.state, args.dataset)

    if args.date:
        resolved, log_line = resolve_date(args.date, latest_fn, context="forecast init date")
        if log_line is not None:
            print(log_line, file=sys.stderr)
        args.date = resolved.isoformat()
    if args.start and args.end:
        start_date, end_date, log_line = resolve_window(args.start, args.end, latest_fn)
        if log_line is not None:
            print(log_line, file=sys.stderr)
        args.start = start_date.isoformat()
        args.end = end_date.isoformat()
    elif args.start or args.end:
        # A lone --start or --end is a shape mismatch reported in the body;
        # its token syntax is still rejected pre-network here.
        parse_token(args.start or args.end)


def _bbox_subset(ds, bbox, bbox_raw) -> object:
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order (latitude is
    descending on these GRIB-derived stores, longitude ascending), so the same
    bbox works whether a dataset stores latitude north-to-south or the reverse.
    ``bbox_raw`` is the bbox exactly as given on the CLI, echoed in the
    no-cells error message.
    """
    north, west, south, east = bbox
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
    ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        raise DataError(
            f"--bbox {bbox_raw} selects no grid cells; check the extent and N/W/S/E order."
        )
    return ds


# output_type: the written envelope is `forecast` for forecast datasets and
# `gridded` for analysis datasets; the union declares both, and the returned
# dataset's detected shape is validated against it before the write.
@weather_skill(
    "dynamical-fetch",
    _SKILL_VERSION,
    output_type=("gridded", "forecast"),
    bbox="optional",
    variable={
        "mode": "repeat",
        "help": "Restrict to this data variable. Repeat once per variable; omit for all.",
    },
    extra_args={
        "dataset": {
            "required": True,
            "help": "Catalog dataset id (validated against dynamical_catalog.list()).",
        },
        "date": {
            "help": (
                "Forecast init date (forecast datasets). Either YYYY-MM-DD, 'now'/'today', "
                "'latest', or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
                "Selects the 00 UTC initialization of the resolved date."
            ),
        },
        "start": {
            "help": "Range start, inclusive (analysis datasets). Same date grammar as --date.",
        },
        "end": {
            "help": "Range end, inclusive (analysis datasets). Same date grammar as --date.",
        },
    },
    validate_args=_validate_and_resolve,
    cache_hit_label="fetch",
)
def fetch(bbox, dataset, date, start, end, variable, context):
    """Fetch a dynamical.org open-catalog dataset and write a weather-skills envelope Zarr."""
    import numpy as np

    state = _open_dataset(context.state, dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")

    # Time flags are bound to the dataset shape: forecasts take a single --date,
    # analyses take a --start/--end range. Mismatches exit 2 before any fetch.
    if is_forecast:
        if not date:
            raise UsageError(f"{dataset} is a forecast dataset; --date is required.")
        if start or end:
            raise UsageError(f"{dataset} is a forecast dataset; use --date, not --start/--end.")
        date_iso = date
    else:
        if not (start and end):
            raise UsageError(f"{dataset} is an analysis dataset; --start and --end are required.")
        if date:
            raise UsageError(f"{dataset} is an analysis dataset; use --start/--end, not --date.")
        start_iso, end_iso = start, end

    if bbox:
        ds = _bbox_subset(ds, bbox, context.args.bbox)

    # Temporal selection + dimension mapping onto the envelope.
    if is_forecast:
        inits = ds["init_time"].values
        # Build the target in the index's own dtype so the membership test and the
        # .sel() label lookup compare like-for-like (a [s]-vs-[ns] mismatch could
        # otherwise let the check pass and .sel() still raise KeyError).
        init_target = np.datetime64(f"{date_iso}T00:00:00").astype(inits.dtype)

        def _no_init() -> DataError:
            return DataError(
                f"{dataset} has no {date_iso} 00 UTC init; available init range is "
                f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}."
            )

        if init_target not in inits:
            raise _no_init()
        try:
            ds = ds.sel(init_time=init_target)
        except KeyError:
            raise _no_init() from None
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
        rename = {"lead_time": "step"}
        if shape == "ensemble":
            rename["ensemble_member"] = "number"
        ds = ds.rename(rename)
        # Demote the selected init_time to the scalar `time` coord the envelope
        # uses for a forecast's init date.
        ds = ds.assign_coords(time=ds["init_time"]).drop_vars("init_time")
    else:
        ds = ds.sel(time=slice(np.datetime64(start_iso), np.datetime64(end_iso)))
        if ds.sizes.get("time", 0) == 0:
            raise DataError(f"{dataset} has no data in {start_iso}..{end_iso}.")
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[variable]

    print(f"Fetching dynamical:{dataset} (shape={shape})", file=sys.stderr)

    # Write lazily: to_zarr streams the selection chunk-by-chunk, so a no-bbox
    # full-grid fetch does not pull the whole archive slice into memory at once.
    # Source variable units are forwarded verbatim (dynamical stamps them); this
    # fetcher does not convert or relabel them. `weather_skills_source` embeds
    # the dataset id, so it is set here; the decorator stamps
    # `weather_skills_history`.
    ds.attrs.update(
        weather_skills_source=f"dynamical:{dataset}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)

    return ds, WroteSummary("", replace=True)


if __name__ == "__main__":
    fetch()
