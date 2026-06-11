# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cftime",
#   "dynamical-catalog==0.5.0",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch a dynamical.org open-catalog dataset and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.5"

# Coords dynamical attaches that are not part of the Rhiza Envelope: forecast
# bookkeeping (valid_time, *_forecast_length) and the CRS scalar (spatial_ref).
# Dropped on the way out so the output carries only envelope coords.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

# --- Relative-date value grammar (duplicated per CONVENTIONS.md; no shared module) ---
#
# A --date/--start/--end value is one of:
#   YYYY-MM-DD                  absolute date
#   now | today                 current UTC date
#   latest                      newest date with available data (per-source)
#   now-<int>{d|w}              now minus N days   (w = 7 days)
#   latest-<int>{d|w}           latest minus N days
# Anything else (months/years, future "+", junk) is rejected pre-network.
_REL_OFFSET_RE = re.compile(r"^(?P<base>now|latest)-(?P<n>\d+)(?P<unit>[dw])$")

# Strict absolute-date shape. date.fromisoformat on 3.11+ also accepts compact
# (20260501) and ISO-week (2026-W18-1) forms; the documented grammar is exactly
# YYYY-MM-DD, so we gate on this regex first and reject the looser forms.
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Upper bound on a relative offset's resolved day count. 36525 days (~100 years)
# is far beyond any real value yet small enough that the date arithmetic cannot
# raise OverflowError. Rejecting above this cap keeps the failure pre-network.
_MAX_OFFSET_DAYS = 36525


def _parse_token(value: str) -> tuple:
    """Parse a --date/--start/--end value into a structured token.

    Returns one of:
      ("abs", date)                              absolute YYYY-MM-DD
      ("base", "now")                            current UTC date
      ("base", "latest")                         newest available date (resolved later)
      ("offset", "now", n_days, unit_phrase)     now minus n_days
      ("offset", "latest", n_days, unit_phrase)  latest minus n_days

    `unit_phrase` describes the offset in its requested units for the log line
    (e.g. "3-week", "7-day"). Raises ValueError for anything else (months/years,
    future "+", malformed), so the failure happens before any network call.
    "today" is accepted as an alias for "now".
    """
    if value in ("now", "today"):
        return ("base", "now")
    if value == "latest":
        return ("base", "latest")
    m = _REL_OFFSET_RE.match(value)
    if m is not None:
        n = int(m.group("n"))
        if n < 1:
            raise ValueError(
                f"invalid date value {value!r}: offset must be >= 1 (e.g. now-1d, latest-3w)"
            )
        unit = m.group("unit")
        n_days = n * 7 if unit == "w" else n
        if n_days > _MAX_OFFSET_DAYS:
            raise ValueError(
                f"invalid date value {value!r}: offset resolves to {n_days} days, "
                f"above the maximum of {_MAX_OFFSET_DAYS} days (~100 years)"
            )
        unit_phrase = f"{n}-{'week' if unit == 'w' else 'day'}"
        return ("offset", m.group("base"), n_days, unit_phrase)
    if _ABS_DATE_RE.match(value):
        try:
            return ("abs", date.fromisoformat(value))
        except ValueError:
            pass
    raise ValueError(
        f"invalid date value {value!r}: expected an absolute date YYYY-MM-DD, "
        "'now'/'today', 'latest', or an offset 'now-<int>{d|w}' / "
        "'latest-<int>{d|w}'"
    )


def _token_base_date(tok: tuple, now: date, latest_fn) -> date:
    """Resolve a parsed token's base date.

    `now` is the current UTC date. `latest_fn` is a zero-arg callable that
    discovers the newest available date for this dataset; it is invoked at most
    once per process (the caller memoizes) and only when a token references
    `latest`.
    """
    kind = tok[0]
    if kind == "abs":
        return tok[1]
    base = tok[1]
    base_date = now if base == "now" else latest_fn()
    if kind == "base":
        return base_date
    return base_date - timedelta(days=tok[2])


def _resolve_single(value: str, latest_fn) -> tuple:
    """Resolve a single --date value to a concrete date.

    Returns (resolved_date, log_line) where log_line is a stderr message to
    print before fetching when a relative token is used, else None. Exits 2
    (pre-network) on a malformed token. `latest_fn` is called only when the
    token references `latest`, and at most once (the caller memoizes).
    """
    try:
        tok = _parse_token(value)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    now = datetime.now(UTC).date()
    resolved = _token_base_date(tok, now, latest_fn)
    log_line = None
    if tok[0] != "abs":
        log_line = f'resolved "{value}" -> {resolved.isoformat()} (forecast init date)'
    return resolved, log_line


def _resolve_window(start_value: str, end_value: str, latest_fn) -> tuple:
    """Resolve --start/--end values to concrete inclusive (start, end) dates.

    Applies the value grammar and the boundary rules:
      - absolute endpoints and ordinary relative ranges are inclusive both ends;
      - the DURATION IDIOM (start is `B-<int>{d|w}` and end is exactly the same
        base token `B`, both `now` or both `latest`) yields an N-day window
        inclusive of the base, with the far edge shifted in by one.

    Returns (start_date, end_date, log_line) where log_line is a stderr message
    to print before fetching when any relative token is present, else None.
    Exits 2 (pre-network) on a malformed token or a reversed range. `latest_fn`
    is called only if a token references `latest`, and at most once.
    """
    try:
        start_tok = _parse_token(start_value)
        end_tok = _parse_token(end_value)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    relative_used = start_tok[0] != "abs" or end_tok[0] != "abs"
    now = datetime.now(UTC).date()

    # Duration idiom: start is an offset off base B, end is exactly base B.
    duration = start_tok[0] == "offset" and end_tok[0] == "base" and start_tok[1] == end_tok[1]

    start_date = _token_base_date(start_tok, now, latest_fn)
    end_date = _token_base_date(end_tok, now, latest_fn)

    if duration:
        # Window is exactly N days, inclusive of the base end, far edge shifted
        # in by one: start moves forward one day so [end-(N-1), end] spans N days.
        n_days = start_tok[2]
        start_date = end_date - timedelta(days=n_days - 1)
        reason = f"duration mode: {start_tok[3]} window inclusive of {start_tok[1]}"
    else:
        reason = "inclusive both ends"

    if start_date > end_date:
        print(
            f"Error: resolved --start {start_date.isoformat()} is after resolved "
            f"--end {end_date.isoformat()}; the range is reversed.",
            file=sys.stderr,
        )
        sys.exit(2)

    log_line = None
    if relative_used:
        span = (end_date - start_date).days + 1
        log_line = (
            f'resolved "{start_value}".."{end_value}" -> '
            f"{start_date.isoformat()}..{end_date.isoformat()} "
            f"({span} days; {reason})"
        )
    return start_date, end_date, log_line


def _np_to_date(value) -> date:
    """Convert a numpy datetime64 to a calendar date (truncating any time-of-day)."""
    return date.fromisoformat(np.datetime_as_string(value, unit="D"))


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


def _cache_hit(out: Path, entry: dict) -> bool:
    """Return True if the zarr at `out` was produced by this same entry."""
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    existing_entry = history[0]
    return (
        existing_entry.get("skill") == entry["skill"]
        and existing_entry.get("version") == entry["version"]
        and existing_entry.get("args") == entry["args"]
        and existing_entry.get("input") == entry["input"]
    )


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
    for name in ("latitude", "lat", "y"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "latitude")
            ds[name].attrs.setdefault("units", "degrees_north")
            ds[name].attrs.setdefault("axis", "Y")
            break
    for name in ("longitude", "lon", "x"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "longitude")
            ds[name].attrs.setdefault("units", "degrees_east")
            ds[name].attrs.setdefault("axis", "X")
            break
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order (latitude is
    descending on these GRIB-derived stores, longitude ascending), so the same
    bbox works whether a dataset stores latitude north-to-south or the reverse.
    """
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
    ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        print(
            f"Error: --bbox {bbox} selects no grid cells; check the extent and N/W/S/E order.",
            file=sys.stderr,
        )
        sys.exit(1)
    return ds


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
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
    p.add_argument(
        "--dataset",
        required=True,
        help="Catalog dataset id (validated against dynamical_catalog.list()).",
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument(
        "--date",
        help=(
            "Forecast init date (forecast datasets). Either YYYY-MM-DD, 'now'/'today', "
            "'latest', or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "Selects the 00 UTC initialization of the resolved date."
        ),
    )
    p.add_argument(
        "--start",
        help="Range start, inclusive (analysis datasets). Same date grammar as --date.",
    )
    p.add_argument(
        "--end",
        help="Range end, inclusive (analysis datasets). Same date grammar as --date.",
    )
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        help="Restrict to this data variable. Repeat once per variable; omit for all.",
    )
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args(_attach_bbox_value(sys.argv[1:]))

    import dynamical_catalog

    catalog = dynamical_catalog.list()
    if args.dataset not in catalog:
        print(
            f"Error: unknown dataset {args.dataset!r}. Available datasets:\n  "
            + "\n  ".join(catalog),
            file=sys.stderr,
        )
        sys.exit(2)

    # Lazy, icechunk-backed open: this reads only metadata, so shape detection,
    # `latest` resolution, and the cache check all run before any array bytes
    # are pulled.
    ds = dynamical_catalog.open(args.dataset)

    # Projected grids (e.g. NOAA HRRR on a Lambert Conformal Conic grid) expose
    # 1-D `y`/`x` in meters with 2-D latitude(y,x)/longitude(y,x) and a CRS in
    # `spatial_ref`, not 1-D latitude/longitude dims. A lat/lon bbox on such a
    # grid needs masking over the 2-D coordinate arrays, and a faithful subset
    # stays curvilinear — which the 1-D-lat/lon Rhiza Envelope does not model.
    # Converting it to a regular lat/lon grid is a reprojection (a grid
    # transform), which belongs in a dedicated reprojection skill, not in this
    # faithful-I/O fetcher.
    if "latitude" not in ds.dims or "longitude" not in ds.dims:
        print(
            f"Error: {args.dataset} is on a projected grid (dims {tuple(ds.dims)}); this "
            "fetcher only handles regular 1-D latitude/longitude grids. Reprojecting a "
            "projected grid to lat/lon is a grid transform for a dedicated reprojection "
            "skill, not this fetcher.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Shape is detected from the dims present, not a hardcoded per-dataset table.
    if "ensemble_member" in ds.dims:
        shape = "ensemble"
    elif "lead_time" in ds.dims:
        shape = "forecast"
    elif "time" in ds.dims:
        shape = "analysis"
    else:
        print(
            f"Error: {args.dataset} has an unrecognized shape (dims {tuple(ds.dims)}); "
            "expected an ensemble/deterministic forecast (lead_time) or an analysis (time).",
            file=sys.stderr,
        )
        sys.exit(2)

    is_forecast = shape in ("ensemble", "forecast")

    # Time flags are bound to the dataset shape: forecasts take a single --date,
    # analyses take a --start/--end range. Mismatches exit 2 before any fetch.
    if is_forecast:
        if not args.date:
            print(
                f"Error: {args.dataset} is a forecast dataset; --date is required.", file=sys.stderr
            )
            sys.exit(2)
        if args.start or args.end:
            print(
                f"Error: {args.dataset} is a forecast dataset; use --date, not --start/--end.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        if not (args.start and args.end):
            print(
                f"Error: {args.dataset} is an analysis dataset; --start and --end are required.",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.date:
            print(
                f"Error: {args.dataset} is an analysis dataset; use --start/--end, not --date.",
                file=sys.stderr,
            )
            sys.exit(2)

    # `latest` resolves cheaply from the opened dataset's own coords (max init
    # for forecasts, max time for analysis); memoized so it is read at most once.
    _latest_coord = "init_time" if is_forecast else "time"
    _latest_cache: dict = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            vals = ds[_latest_coord].values
            if is_forecast:
                # --date selects the 00 UTC init, so `latest` must be the newest
                # date that HAS one. Filter to midnight inits before taking the
                # max so a later same-day cycle (e.g. GFS 18 UTC) doesn't resolve
                # `latest` to a date whose 00 UTC init isn't published yet.
                midnight = vals[vals == vals.astype("datetime64[D]")]
                if midnight.size:
                    vals = midnight
            _latest_cache["v"] = _np_to_date(vals.max())
        return _latest_cache["v"]

    if is_forecast:
        resolved_date, log_line = _resolve_single(args.date, _latest)
        date_iso = resolved_date.isoformat()
        if log_line is not None:
            print(log_line, file=sys.stderr)
    else:
        start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        if log_line is not None:
            print(log_line, file=sys.stderr)

    # Cache key: the argparse namespace minus the output path, with the resolved
    # concrete date(s) substituted for any relative token.
    args_dict = {k: v for k, v in vars(args).items() if k != "output"}
    if is_forecast:
        args_dict["date"] = date_iso
    else:
        args_dict["start"] = start_iso
        args_dict["end"] = end_iso
    entry = {
        "skill": "dynamical-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": args_dict,
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    if args.bbox:
        ds = _bbox_subset(ds, args.bbox)

    # Temporal selection + dimension mapping onto the envelope.
    if is_forecast:
        inits = ds["init_time"].values
        # Build the target in the index's own dtype so the membership test and the
        # .sel() label lookup compare like-for-like (a [s]-vs-[ns] mismatch could
        # otherwise let the check pass and .sel() still raise KeyError).
        init_target = np.datetime64(f"{date_iso}T00:00:00").astype(inits.dtype)

        def _no_init() -> None:
            print(
                f"Error: {args.dataset} has no {date_iso} 00 UTC init; available init range is "
                f"{_np_to_date(inits.min()).isoformat()}..{_np_to_date(inits.max()).isoformat()}.",
                file=sys.stderr,
            )
            sys.exit(1)

        if init_target not in inits:
            _no_init()
        try:
            ds = ds.sel(init_time=init_target)
        except KeyError:
            _no_init()
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
            print(
                f"Error: {args.dataset} has no data in {start_iso}..{end_iso}.",
                file=sys.stderr,
            )
            sys.exit(1)
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    if args.variable:
        missing = [v for v in args.variable if v not in ds.data_vars]
        if missing:
            print(
                f"Error: variable(s) not in {args.dataset}: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[args.variable]

    print(f"Fetching dynamical:{args.dataset} (shape={shape})", file=sys.stderr)

    # Write lazily: to_zarr streams the selection chunk-by-chunk, so a no-bbox
    # full-grid fetch does not pull the whole archive slice into memory at once.
    # Source variable units are forwarded verbatim (dynamical stamps them); this
    # fetcher does not convert or relabel them.
    ds.attrs.update(
        rhiza_source=f"dynamical:{args.dataset}",
        rhiza_history=json.dumps([entry], sort_keys=True),
        Conventions="CF-1.13",
    )
    _stamp_cf_attrs(ds)
    # Per-variable encoding is not part of the envelope contract; clear it so the
    # output is written with this skill's own codecs.
    for v in ds.variables:
        ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
