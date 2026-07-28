# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "pandas",
#   "cftime",
#   "cf_xarray",
#   "cf_units",
# ]
# ///
"""Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a weather-skills envelope Zarr."""

import calendar
import sys
from datetime import UTC, date, datetime

# Third-party imports are at module top so a missing inline dependency fails the
# script immediately, before any argument parsing or network access.
import cf_units  # noqa: F401  (loaded lazily by core's udunits_error at write time)
import cf_xarray  # noqa: F401  (registers the .cf accessor used below)
import gcsfs
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, types, weather_skill
from weather_skills_core.envelope import normalize_longitude, udunits_error
from weather_skills_core.provenance import make_completeness_probe

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

# Public, credential-free Pangeo CMIP6 collection on Google Cloud. The catalog
# CSV maps facet combinations to a Zarr store path (`zstore`); data is read
# anonymously.
_CATALOG_URL = "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv"

# CF Conventions version this fetcher stamps on the output. CMIP6 source stores
# carry an older inherited value (e.g. "CF-1.7 CMIP-6.0 UGRID-1.0"); the envelope
# transform repairs the dataset to the current CF release and re-stamps it.
_CF_CONVENTIONS = "CF-1.13"

# --- Source -> output transforms ---
#
# Everything not listed here is passed through from the raw CMIP6 source
# verbatim. The transforms this fetcher applies are:
#
#   VARIABLE/COORD RENAMES:
#     - `lat` -> `latitude`
#     - `lon` -> `longitude`
#     (the data variable keeps its CMIP6 name, e.g. `tas`, `pr`)
#
#   LONGITUDE NORMALIZATION:
#     - The source 0..360 longitude axis is mapped onto [-180, 180) and the
#       dataset is re-sorted ascending by longitude (via weather_skills_core's
#       `normalize_longitude`, called in the pipeline).
#
#   UNITS:
#     - PASS THROUGH VERBATIM. The source CF units string on the data variable is
#       forwarded unchanged; it is only validated as udunits-parseable
#       (cf_units.Unit) before write, never remapped or converted.
#
#   GLOBAL ATTRS:
#     - `Conventions` is OVERWRITTEN to the CF release above (_CF_CONVENTIONS).
#     - All other source CMIP6 global attrs are PRESERVED.
#     - `history` has one line APPENDED describing this subset.
#     - `weather_skills_source` and `weather_skills_history` keys are ADDED.
#
#   BOUNDS (structural):
#     - Every `*_bnds` / `*_bounds` cell-bounds variable is DROPPED, the orphaned
#       bounds index dim is removed, and each variable's now-dangling `bounds`
#       attr is STRIPPED (the weather-skills envelope carries no cell bounds).
#
#   standard_name / long_name:
#     - PRESERVED from source; this skill does not assign them. Coord CF attrs
#       (standard_name/units/axis on latitude/longitude/time) are only filled
#       via setdefault when absent after the rename, leaving source values intact.


def _resolve_zstore(model, experiment, variable, member, table, grid) -> tuple:
    """Resolve the facet flags against the CMIP6 catalog to exactly one zstore.

    Returns (zstore, grid_label, version). Exits 2 with diagnostics on zero
    matches or an ambiguous grid; exits 1 with an actionable message if the
    catalog CSV cannot be downloaded.
    """
    try:
        df = pd.read_csv(_CATALOG_URL)
    except Exception as exc:  # noqa: BLE001 (network/parse failures vary by backend)
        raise DataError(
            f"failed to download or parse the CMIP6 catalog from {_CATALOG_URL} "
            f"({type(exc).__name__}: {exc}). Check network access to "
            "storage.googleapis.com, then retry."
        ) from None
    mask = (
        (df.source_id == model)
        & (df.experiment_id == experiment)
        & (df.variable_id == variable)
        & (df.member_id == member)
        & (df.table_id == table)
    )
    sub = df[mask]
    if grid:
        sub = sub[sub.grid_label == grid]
    if sub.empty:
        # Diagnose against the model alone so the message points at the real
        # available facet values rather than a blank "not found".
        for_model = df[df.source_id == model]
        if for_model.empty:
            hint = f"unknown --model {model!r}; sample models: {sorted(df.source_id.unique())[:10]}"
        else:
            hint = (
                f"for model {model!r}: "
                f"experiments={sorted(for_model.experiment_id.unique())[:12]}; "
                f"variables(table {table})="
                f"{sorted(for_model[for_model.table_id == table].variable_id.unique())[:15]}; "
                f"members={sorted(for_model.member_id.unique())[:8]}"
            )
        raise UsageError(
            f"no CMIP6 dataset matches model={model} experiment={experiment} "
            f"variable={variable} member={member} table={table}"
            + (f" grid={grid}" if grid else "")
            + f".\n{hint}"
        )
    grids = sorted(sub.grid_label.unique())
    if len(grids) > 1:
        raise UsageError(f"multiple grid_labels match: {grids}. Pass --grid to choose one.")
    # Several versions may remain for the same facets; take the latest.
    row = sub.sort_values("version").iloc[-1]
    # Withdrawn/retracted catalog entries can carry a NaN or empty zstore. Passing
    # that to get_mapper fails opaquely; validate it here and point at the facets.
    zstore = row["zstore"]
    if not isinstance(zstore, str) or not zstore.strip():
        raise UsageError(
            f"the matched CMIP6 entry (model={model} experiment={experiment} "
            f"variable={variable} member={member} table={table} "
            f"grid={grids[0]} version={row['version']}) has no zstore path "
            "(the catalog row is empty/withdrawn). Try different facets or another "
            "--grid/--member."
        )
    return zstore, grids[0], str(row["version"])


def _ensure_catalog(state, model, experiment, variable, member, table, grid) -> None:
    """Resolve the catalog facets to a zstore, at most once per run.

    Populates the run-scoped ``state`` dict (``RunContext.state``, shared by
    the decorator's hooks and the fetch body) with the matched zstore path,
    the resolved grid_label, and the catalog data version -- all known from
    the catalog CSV alone, without opening any store.
    """
    if "zstore" not in state:
        zstore, grid_label, version = _resolve_zstore(
            model, experiment, variable, member, table, grid
        )
        state.update(zstore=zstore, grid_label=grid_label, version=version)


def _open_remote(state, model, experiment, variable, member, table, grid) -> dict:
    """Resolve the catalog and open the matched store, at most once per run.

    Populates and returns the run-scoped ``state`` dict: the lazily opened
    dataset (only metadata is read until a subset is written) and the source
    time calendar/units captured from the time encoding before any transform
    so they can be re-asserted on the written store. Shared by the `latest`
    resolver and the fetch body so the catalog is resolved and the store
    opened at most once per run.
    """
    if "ds" not in state:
        _ensure_catalog(state, model, experiment, variable, member, table, grid)
        grid_label = state["grid_label"]
        fs = gcsfs.GCSFileSystem(token="anon")
        mapper = fs.get_mapper(state["zstore"])
        # CMIP6 stores use non-standard calendars (noleap, 360_day), so decode
        # times with cftime. Newer xarray wants a CFDatetimeCoder passed to
        # decode_times; older xarray only accepts the use_cftime kwarg.
        try:
            time_coder = xr.coders.CFDatetimeCoder(use_cftime=True)
            ds = xr.open_zarr(mapper, consolidated=True, decode_times=time_coder)
        except AttributeError:
            ds = xr.open_zarr(mapper, consolidated=True, use_cftime=True)

        # xarray populates encoding["calendar"] and encoding["units"] when
        # decoding times with cftime. units may legitimately be absent; if so it
        # is omitted from the write encoding and xarray regenerates a correct
        # value for the decoded cftime values (no fabricated epoch).
        source_calendar = ds["time"].encoding.get("calendar")
        source_time_units = ds["time"].encoding.get("units")
        if source_calendar is None:
            raise DataError(
                "could not determine the source time calendar from the CMIP6 store; "
                "refusing to write a store whose calendar cannot be verified."
            )

        # This fetcher handles regular 1-D lat/lon grids only. Ocean/curvilinear
        # CMIP6 grids carry 2-D latitude/longitude over (i, j) index dims, which the
        # 1-D-lat/lon weather-skills envelope does not model; reprojecting them is a grid
        # transform for a dedicated skill, not this fetcher.
        if "lat" not in ds.dims or "lon" not in ds.dims:
            raise UsageError(
                f"{model}/{variable} ({grid_label}) is not on a regular 1-D "
                f"lat/lon grid (dims {tuple(ds.dims)}); this fetcher handles only regular "
                "lat/lon grids. Reprojecting a curvilinear grid is a separate grid transform."
            )
        state.update(
            ds=ds,
            source_calendar=source_calendar,
            source_time_units=source_time_units,
        )
    return state


def _latest(args, context) -> date:
    """Newest date with data, from the opened remote dataset's time axis.

    The time axis is cftime under a possibly non-standard CF calendar
    (360_day, noleap, all_leap, julian), where a day value can be invalid
    for the stdlib (e.g. Feb 30 on 360_day). Clamp the day to the
    stdlib-valid maximum for that year/month so date() never raises. This
    value only seeds the relative-date grammar window; the real selection
    is string slicing on the cftime index (ds.sel(time=slice(...))), so a
    day clamped by a day or two is acceptable here.
    """
    state = _open_remote(
        context.state,
        args.model,
        args.experiment,
        args.variable,
        args.member,
        args.table,
        args.grid,
    )
    t = state["ds"]["time"].values.max()
    last_day = calendar.monthrange(t.year, t.month)[1]
    return date(t.year, t.month, min(t.day, last_day))


def _canonicalize_entry(raw: dict, context) -> dict:
    """Resolve the catalog before the cache comparison and fold the discovered
    values into the compared entry args.

    The recorded provenance args carry the resolved grid_label and the catalog
    data version, not the raw --grid flag -- both are known from the catalog
    CSV alone, before any store is opened. Resolving here, in the decorator's
    entry-canonicalization hook, puts them into the entry the cache check
    compares: a rerun against an unchanged catalog matches the stamped entry
    and hits without opening the source store or rewriting the output, while a
    catalog change (a new data version, a different grid resolution) changes
    the compared entry and forces a refetch. The cost is one lightweight
    catalog CSV fetch before the cache check.
    """
    state = context.state
    _ensure_catalog(
        state,
        raw["model"],
        raw["experiment"],
        raw["variable"],
        raw["member"],
        raw["table"],
        raw["grid"],
    )
    return {**raw, "grid": state["grid_label"], "data_version": state["version"]}


# Cache-hit completeness probe, keyed to the requested --variable: the
# variable must be present and its corner cell must decode, so a store whose
# `weather_skills_history` attr survived an interrupted write is a cache miss
# rather than a broken output handed to the caller.
_store_is_complete = make_completeness_probe(lambda context: context.args.variable)


def _ensure_coord_cf_attrs(ds):
    """Ensure latitude/longitude/time coords carry CF standard_name/units/axis.

    CMIP6 source coords already carry these; this fills only any that are absent
    after the rename so the rest of the source metadata survives untouched.
    """
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


def _drop_bounds(ds):
    """Drop every `*_bnds` bounds variable and the dangling `bounds` attr it leaves.

    The weather-skills envelope does not carry cell bounds. Removing the bounds variables
    without also clearing the coords' `bounds` attrs would leave each coord
    pointing at an absent variable, which is a CF section 7.1 violation
    (cf-xarray's bounds resolution and any CF checker would flag it). This drops
    the bounds variables and the now-orphaned `bnds` index dim, then strips the
    `bounds` attr from every variable so no dangling reference remains.
    """
    bounds_vars = [
        v for v in ds.variables if str(v).endswith("_bnds") or str(v).endswith("_bounds")
    ]
    if bounds_vars:
        ds = ds.drop_vars(bounds_vars)
    # The bounds dim ("bnds") becomes an orphan index coord once its bounds
    # variables are gone; drop it too if it is a coord with no remaining users.
    for dim_coord in ("bnds", "bounds", "nv"):
        if dim_coord in ds.coords and dim_coord not in ds.dims:
            ds = ds.drop_vars(dim_coord)
    # Remove every `bounds` attr; each one now points at a removed variable.
    for v in ds.variables:
        if "bounds" in ds[v].attrs:
            del ds[v].attrs["bounds"]
    return ds


def _verify_cf_decode(ds, variable: str) -> None:
    """Confirm cf-xarray can resolve the X/Y/T axes before writing.

    A write-side guard: if the coord attrs do not let cf-xarray identify the
    longitude (X), latitude (Y), and time (T) axes, the output would not be the
    CF-navigable store the envelope promises. Fail with an actionable message.
    """
    axes = ds.cf.axes
    missing = [name for name in ("X", "Y", "T") if name not in axes]
    if missing:
        raise DataError(
            f"cf-xarray cannot resolve axes {missing} on the output "
            f"(resolved: {sorted(axes)}); the coord CF attrs are incomplete."
        )


def _normalize_calendar(cal: str) -> str:
    """Fold CF calendar aliases to a canonical name for comparison.

    CF/cftime treat three pairs of names as aliases of the same calendar, so a
    source labeled with one name and a written store labeled with its alias (or
    vice versa) are the same calendar and must compare equal:

    - `gregorian` -> `standard`
    - `365_day` -> `noleap`
    - `366_day` -> `all_leap`

    `proleptic_gregorian` is a genuinely DISTINCT calendar (it extrapolates the
    Gregorian rule before 1582), not an alias of `standard`, and xarray
    round-trips it verbatim, so it is left unmapped here — a coercion between it
    and `standard` is a real change to flag.
    """
    aliases = {
        "gregorian": "standard",
        "365_day": "noleap",
        "366_day": "all_leap",
    }
    return aliases.get(cal, cal)


def _verify_written_calendar(out, source_calendar: str) -> None:
    """Re-open the written store and confirm the time calendar was not coerced.

    xarray can silently coerce a non-standard CMIP6 calendar (noleap, 360_day)
    to a proleptic-gregorian "standard" calendar on write if the time encoding is
    not preserved, which would corrupt the date axis. Read the calendar back off
    the written store and fail if it does not match the source, folding the
    CF `gregorian`/`standard` alias so a correct store is not falsely rejected.
    """
    with xr.open_zarr(out, consolidated=True, decode_times=False) as ds:
        written = ds["time"].attrs.get("calendar") or ds["time"].encoding.get("calendar")
        units = ds["time"].attrs.get("units") or ds["time"].encoding.get("units")
    if written is None:
        raise DataError(
            "the written store has no `calendar` on its time axis; the source "
            f"calendar {source_calendar!r} was not preserved."
        )
    if _normalize_calendar(str(written)) != _normalize_calendar(str(source_calendar)):
        raise DataError(
            f"time calendar was coerced to {written!r} on write but the source "
            f"calendar is {source_calendar!r}; refusing to emit a corrupted date axis."
        )
    if units is None:
        raise DataError("the written store has no udunits `units` on its time axis.")


def _set_write_encoding(ds, context) -> None:
    """Controlled write encodings, applied after the decorator's encoding clear.

    The time axis's source `calendar` (and `units` when the source carried
    them) must be carried into the write encoding so the non-standard CMIP6
    calendar is not coerced. A _FillValue, if the source carried one, belongs
    in the write encoding, not in attrs, and is restored only on data
    variables -- CF discourages a _FillValue on coordinate variables, so
    coords stay cleared.
    """
    state = context.state
    for v, fill in state.get("fills", {}).items():
        if fill is not None:
            ds[v].encoding["_FillValue"] = fill
    # Omit units when the source did not carry them; xarray then generates a
    # correct udunits string for the decoded cftime values rather than us
    # inventing an epoch.
    if state["source_time_units"] is not None:
        ds["time"].encoding["units"] = state["source_time_units"]
    ds["time"].encoding["calendar"] = state["source_calendar"]


def _verify_calendar(out, context) -> None:
    """Post-write hook: verify on the WRITTEN store that the source calendar
    survived; do not assume the write preserved it.

    Runs after the store is written and before the ``Wrote:`` line, so a
    failed verification exits non-zero without a success line. A cache hit
    skips it (nothing was written). Failures raise :class:`DataError` and map
    to the usual stderr/exit-code convention.
    """
    _verify_written_calendar(out, context.state["source_calendar"])


@weather_skill(
    "cmip6-fetch",
    _SKILL_VERSION,
    output_type=types.GRIDDED,
    start_time={
        "help": (
            "Range start, inclusive. Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "Absolute future dates are allowed (scenario experiments run to 2100)."
        )
    },
    end_time=True,
    bbox=types.OPTIONAL,
    variable={
        "mode": types.SINGLE,
        "required": True,
        "help": "CMIP6 variable_id (one variable per dataset, e.g. tas, pr).",
    },
    extra_args={
        "model": {"required": True, "help": "CMIP6 source_id (e.g. GFDL-CM4)."},
        "experiment": {"required": True, "help": "CMIP6 experiment_id (e.g. historical, ssp245)."},
        "member": {"default": "r1i1p1f1", "help": "CMIP6 member_id (default r1i1p1f1)."},
        "table": {"default": "Amon", "help": "CMIP6 table_id (default Amon)."},
        "grid": {
            "help": "CMIP6 grid_label; required only when more than one matches the other facets."
        },
    },
    latest_resolver=_latest,
    completeness_probe=_store_is_complete,
    normalize_args=_canonicalize_entry,
    write_encoding=_set_write_encoding,
    post_write=_verify_calendar,
    cache_hit_label="fetch",
)
def fetch(start_time, end_time, bbox, model, experiment, variable, member, table, grid, context):
    """Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a weather-skills envelope Zarr."""
    from weather_skills_core.envelope import bbox_subset

    state = _open_remote(context.state, model, experiment, variable, member, table, grid)
    ds = state["ds"]
    grid_label = state["grid_label"]
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    if variable not in ds.data_vars:
        raise UsageError(
            f"variable {variable!r} not present in the dataset; available: {sorted(ds.data_vars)}"
        )

    # Capture the rich CMIP6 global attrs before any selection so they survive
    # onto the envelope. The transform preserves them, overwrites only
    # `Conventions`, and appends a history line.
    source_globals = dict(ds.attrs)

    # Keep only the requested variable. Selecting ds[[variable]] drops the
    # *_bnds bounds variables; _drop_bounds then clears the now-dangling `bounds`
    # attrs the coords would otherwise still carry.
    ds = ds[[variable]]
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    ds = _drop_bounds(ds)
    ds = normalize_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    ds = ds.sel(time=slice(start_iso, end_iso))
    if ds.sizes.get("time", 0) == 0:
        raise DataError(
            f"{model}/{experiment}/{variable} has no data in "
            f"{start_iso}..{end_iso} (dataset time range may not cover the window)."
        )

    print(
        f"Fetching cmip6 {model}/{experiment}/{member}/{table}/"
        f"{variable}/{grid_label} {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    # Global attrs: preserve the source CMIP6 globals, overwrite Conventions to
    # the current CF release, append a history line, and add the
    # `weather_skills_source` key (its grid_label component is
    # catalog-discovered, so it is set here; the decorator stamps
    # `weather_skills_history`). The appended CF history line records the
    # bbox exactly as given on the CLI.
    bbox_raw = context.args.bbox
    history_line = (
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} cmip6-fetch: "
        f"subset {variable} to {start_iso}..{end_iso}"
        + (f" bbox {bbox_raw}" if bbox_raw else "")
        + f"; mapped onto the weather-skills envelope and re-stamped {_CF_CONVENTIONS}."
    )
    prior_history = source_globals.get("history", "")
    new_globals = dict(source_globals)
    new_globals["Conventions"] = _CF_CONVENTIONS
    new_globals["history"] = (
        (prior_history + "\n" + history_line) if prior_history else history_line
    )
    new_globals["weather_skills_source"] = (
        f"cmip6:{model}/{experiment}/{member}/{table}/{variable}/{grid_label}"
    )
    ds.attrs = new_globals

    _ensure_coord_cf_attrs(ds)

    # The output claims CF-1.13 compliance, so the data variable must carry a
    # udunits-valid `units` string. CMIP6 stores carry CF-correct units, but a
    # missing or malformed value would make the CF claim false; emit an
    # actionable error rather than write it.
    units = ds[variable].attrs.get("units")
    if units is None:
        raise DataError(
            f"variable {variable!r} has no `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    if not str(units).strip():
        # cf_units.Unit("") / all-whitespace parses as an "unknown" unit rather
        # than raising, so udunits_error would let a blank units string through;
        # reject it here, before the parse, the same way a missing value is.
        raise DataError(
            f"variable {variable!r} has a blank `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    units_exc = udunits_error(units)
    if units_exc is not None:
        raise DataError(
            f"variable {variable!r} has units {units!r}, which is not a valid "
            f"udunits string ({units_exc}); refusing to write it under a CF-1.13 claim."
        )

    _verify_cf_decode(ds, variable)

    # Source _FillValues, captured before the decorator's encoding clear so the
    # write-encoding hook can restore them on the data variables.
    context.state["fills"] = {v: ds[v].encoding.get("_FillValue") for v in ds.data_vars}

    return ds


if __name__ == "__main__":
    fetch()
