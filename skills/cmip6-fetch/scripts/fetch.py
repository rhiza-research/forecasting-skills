# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "pandas",
#   "cftime",
# ]
# ///
"""Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

# Public, credential-free Pangeo CMIP6 collection on Google Cloud. The catalog
# CSV maps facet combinations to a Zarr store path (`zstore`); data is read
# anonymously.
_CATALOG_URL = "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv"

# --- Relative-date value grammar (duplicated per CONVENTIONS.md; no shared module) ---
#
# A --start/--end value is one of:
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
# is far beyond any real window yet small enough that the date arithmetic cannot
# raise OverflowError. Rejecting above this cap keeps the failure pre-network.
_MAX_OFFSET_DAYS = 36525


def _parse_token(value: str) -> tuple:
    """Parse a --start/--end value into a structured token.

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

    `now` is the current UTC date. `latest_fn` is a zero-arg callable returning
    the newest available date; invoked only when a token references `latest`.
    """
    kind = tok[0]
    if kind == "abs":
        return tok[1]
    base = tok[1]
    base_date = now if base == "now" else latest_fn()
    if kind == "base":
        return base_date
    return base_date - timedelta(days=tok[2])


def _resolve_window(start_value: str, end_value: str, latest_fn) -> tuple:
    """Resolve --start/--end values to concrete inclusive (start, end) dates.

    Applies the value grammar and the boundary rules:
      - absolute endpoints and ordinary relative ranges are inclusive both ends;
      - the DURATION IDIOM (start is `B-<int>{d|w}` and end is exactly the same
        base token `B`, both `now` or both `latest`) yields an N-day window
        inclusive of the base, with the far edge shifted in by one.

    Returns (start_date, end_date, log_line) where log_line is a stderr message
    to print before fetching when any relative token is present, else None.
    Exits 2 (pre-network) on a malformed token or a reversed range.
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


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
    except (FileNotFoundError, KeyError, ValueError):
        # A not-yet-existing or unreadable output during a cache check is a miss.
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
    if "latitude" in ds.coords:
        ds["latitude"].attrs.setdefault("standard_name", "latitude")
        ds["latitude"].attrs.setdefault("units", "degrees_north")
        ds["latitude"].attrs.setdefault("axis", "Y")
    if "longitude" in ds.coords:
        ds["longitude"].attrs.setdefault("standard_name", "longitude")
        ds["longitude"].attrs.setdefault("units", "degrees_east")
        ds["longitude"].attrs.setdefault("axis", "X")
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def _normalize_longitude(ds):
    """Map a 0..360 longitude onto [-180, 180) and sort ascending, so an N/W/S/E
    bbox with negative west/east values selects correctly."""
    lon = ((ds["longitude"] + 180) % 360) - 180
    ds = ds.assign_coords(longitude=lon)
    return ds.sortby("longitude")


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order, so the same
    bbox works regardless of how the dataset stores latitude.
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


def _resolve_zstore(args) -> tuple:
    """Resolve the facet flags against the CMIP6 catalog to exactly one zstore.

    Returns (zstore, grid_label, version). Exits 2 with diagnostics on zero
    matches or an ambiguous grid.
    """
    import pandas as pd

    df = pd.read_csv(_CATALOG_URL)
    mask = (
        (df.source_id == args.model)
        & (df.experiment_id == args.experiment)
        & (df.variable_id == args.variable)
        & (df.member_id == args.member)
        & (df.table_id == args.table)
    )
    sub = df[mask]
    if args.grid:
        sub = sub[sub.grid_label == args.grid]
    if sub.empty:
        # Diagnose against the model alone so the message points at the real
        # available facet values rather than a blank "not found".
        for_model = df[df.source_id == args.model]
        if for_model.empty:
            hint = f"unknown --model {args.model!r}; sample models: {sorted(df.source_id.unique())[:10]}"
        else:
            hint = (
                f"for model {args.model!r}: "
                f"experiments={sorted(for_model.experiment_id.unique())[:12]}; "
                f"variables(table {args.table})="
                f"{sorted(for_model[for_model.table_id == args.table].variable_id.unique())[:15]}; "
                f"members={sorted(for_model.member_id.unique())[:8]}"
            )
        print(
            f"Error: no CMIP6 dataset matches model={args.model} experiment={args.experiment} "
            f"variable={args.variable} member={args.member} table={args.table}"
            + (f" grid={args.grid}" if args.grid else "")
            + f".\n{hint}",
            file=sys.stderr,
        )
        sys.exit(2)
    grids = sorted(sub.grid_label.unique())
    if len(grids) > 1:
        print(
            f"Error: multiple grid_labels match: {grids}. Pass --grid to choose one.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Several versions may remain for the same facets; take the latest.
    row = sub.sort_values("version").iloc[-1]
    return row["zstore"], grids[0], str(row["version"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, help="CMIP6 source_id (e.g. GFDL-CM4).")
    p.add_argument(
        "--experiment", required=True, help="CMIP6 experiment_id (e.g. historical, ssp245)."
    )
    p.add_argument(
        "--variable",
        "-v",
        required=True,
        help="CMIP6 variable_id (one variable per dataset, e.g. tas, pr).",
    )
    p.add_argument("--member", default="r1i1p1f1", help="CMIP6 member_id (default r1i1p1f1).")
    p.add_argument("--table", default="Amon", help="CMIP6 table_id (default Amon).")
    p.add_argument(
        "--grid",
        help="CMIP6 grid_label; required only when more than one matches the other facets.",
    )
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Range start, inclusive. Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "Absolute future dates are allowed (scenario experiments run to 2100)."
        ),
    )
    p.add_argument(
        "--end", required=True, help="Range end, inclusive. Same date grammar as --start."
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    import gcsfs
    import xarray as xr

    zstore, grid_label, version = _resolve_zstore(args)

    fs = gcsfs.GCSFileSystem(token="anon")
    mapper = fs.get_mapper(zstore)
    # CMIP6 stores use non-standard calendars (noleap, 360_day), so decode times
    # with cftime. Newer xarray wants a CFDatetimeCoder passed to decode_times;
    # older xarray only accepts the use_cftime kwarg. Open is lazy: only metadata
    # is read until a subset is written.
    try:
        time_coder = xr.coders.CFDatetimeCoder(use_cftime=True)
        ds = xr.open_zarr(mapper, consolidated=True, decode_times=time_coder)
    except AttributeError:
        ds = xr.open_zarr(mapper, consolidated=True, use_cftime=True)

    # This fetcher handles regular 1-D lat/lon grids only. Ocean/curvilinear
    # CMIP6 grids carry 2-D latitude/longitude over (i, j) index dims, which the
    # 1-D-lat/lon Rhiza Envelope does not model; reprojecting them is a grid
    # transform for a dedicated skill, not this fetcher.
    if "lat" not in ds.dims or "lon" not in ds.dims:
        print(
            f"Error: {args.model}/{args.variable} ({grid_label}) is not on a regular 1-D "
            f"lat/lon grid (dims {tuple(ds.dims)}); this fetcher handles only regular "
            "lat/lon grids. Reprojecting a curvilinear grid is a separate grid transform.",
            file=sys.stderr,
        )
        sys.exit(2)

    def _latest() -> date:
        t = ds["time"].values.max()
        return date(t.year, t.month, t.day)

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "cmip6-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {
            "model": args.model,
            "experiment": args.experiment,
            "variable": args.variable,
            "member": args.member,
            "table": args.table,
            "grid": grid_label,
            "data_version": version,
            "bbox": args.bbox,
            "start": start_iso,
            "end": end_iso,
        },
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    # Keep only the requested variable (this drops *_bnds bounds variables and
    # their dims), then map coords onto the envelope.
    if args.variable not in ds.data_vars:
        print(
            f"Error: variable {args.variable!r} not present in the dataset; "
            f"available: {sorted(ds.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)
    ds = ds[[args.variable]]
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    ds = _normalize_longitude(ds)
    if args.bbox:
        ds = _bbox_subset(ds, args.bbox)

    ds = ds.sel(time=slice(start_iso, end_iso))
    if ds.sizes.get("time", 0) == 0:
        print(
            f"Error: {args.model}/{args.experiment}/{args.variable} has no data in "
            f"{start_iso}..{end_iso} (dataset time range may not cover the window).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Fetching cmip6 {args.model}/{args.experiment}/{args.member}/{args.table}/"
        f"{args.variable}/{grid_label} {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    ds.attrs.update(
        rhiza_source=(
            f"cmip6:{args.model}/{args.experiment}/{args.member}/"
            f"{args.table}/{args.variable}/{grid_label}"
        ),
        rhiza_history=json.dumps([entry], sort_keys=True),
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

    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
