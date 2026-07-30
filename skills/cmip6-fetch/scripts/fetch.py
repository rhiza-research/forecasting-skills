# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "pandas",
#   "cftime",
#   "cf_xarray",
#   "cf_units",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a WeatherSkills standard dataset."""

import calendar
import sys
from datetime import UTC, date, datetime

import cf_units  # noqa: F401
import cf_xarray  # noqa: F401
import gcsfs
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, EntryOverride, Types, UsageError, weather_skill
from weather_skills_core.dataset import bbox_subset, normalize_longitude, udunits_error

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

_STATE: dict = {}

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


def _ensure_catalog(model, experiment, variable, member, table, grid) -> None:
    """Resolve the catalog facets to a zstore, at most once per run."""
    if "zstore" not in _STATE:
        zstore, grid_label, version = _resolve_zstore(
            model, experiment, variable, member, table, grid
        )
        _STATE.update(zstore=zstore, grid_label=grid_label, version=version)


def _open_remote(model, experiment, variable, member, table, grid) -> dict:
    """Resolve the catalog and open the matched store, at most once per run."""
    if "ds" not in _STATE:
        _ensure_catalog(model, experiment, variable, member, table, grid)
        grid_label = _STATE["grid_label"]
        fs = gcsfs.GCSFileSystem(token="anon")
        mapper = fs.get_mapper(_STATE["zstore"])
        try:
            time_coder = xr.coders.CFDatetimeCoder(use_cftime=True)
            ds = xr.open_zarr(mapper, consolidated=True, decode_times=time_coder)
        except AttributeError:
            ds = xr.open_zarr(mapper, consolidated=True, use_cftime=True)

        source_calendar = ds["time"].encoding.get("calendar")
        source_time_units = ds["time"].encoding.get("units")
        if source_calendar is None:
            raise DataError(
                "could not determine the source time calendar from the CMIP6 store; "
                "refusing to write a store whose calendar cannot be verified."
            )

        if "lat" not in ds.dims or "lon" not in ds.dims:
            raise UsageError(
                f"{model}/{variable} ({grid_label}) is not on a regular 1-D "
                f"lat/lon grid (dims {tuple(ds.dims)}); this fetcher handles only regular "
                "lat/lon grids. Reprojecting a curvilinear grid is a separate grid transform."
            )
        _STATE.update(
            ds=ds,
            source_calendar=source_calendar,
            source_time_units=source_time_units,
        )
    return _STATE


def _latest(model, experiment, variable, member, table, grid) -> date:
    """Newest date with data, from the opened remote dataset's time axis."""
    state = _open_remote(model, experiment, variable, member, table, grid)
    t = state["ds"]["time"].values.max()
    last_day = calendar.monthrange(t.year, t.month)[1]
    return date(t.year, t.month, min(t.day, last_day))


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


@weather_skill(
    name="cmip6-fetch",
    version=_SKILL_VERSION,
    outputs=[Types.GRIDDED],
    required_args=("start_time", "end_time", "variable"),
    optional_args=("bbox",),
    check_cache=True,
)
@weather_skill.argument("--model", required=True, help="CMIP6 source_id (e.g. GFDL-CM4).")
@weather_skill.argument(
    "--experiment", required=True, help="CMIP6 experiment_id (e.g. historical, ssp245)."
)
@weather_skill.argument("--member", default="r1i1p1f1", help="CMIP6 member_id (default r1i1p1f1).")
@weather_skill.argument("--table", default="Amon", help="CMIP6 table_id (default Amon).")
@weather_skill.argument(
    "--grid",
    help="CMIP6 grid_label; required only when more than one matches the other facets.",
)
def fetch(start_time, end_time, bbox, model, experiment, variable, member, table, grid):
    """Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a WeatherSkills standard dataset."""
    if not variable or len(variable) != 1:
        raise UsageError("cmip6-fetch requires exactly one --variable / -v (one variable_id).")
    var = variable[0]

    def _resolve(d):
        return (
            _latest(model, experiment, var, member, table, grid) if d == "latest" else d
        )

    start_time, end_time = _resolve(start_time), _resolve(end_time)
    state = _open_remote(model, experiment, var, member, table, grid)
    ds = state["ds"]
    grid_label = state["grid_label"]
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    if var not in ds.data_vars:
        raise UsageError(
            f"variable {var!r} not present in the dataset; available: {sorted(ds.data_vars)}"
        )

    source_globals = dict(ds.attrs)
    fills = {v: ds[v].encoding.get("_FillValue") for v in [var]}

    ds = ds[[var]]
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    ds = _drop_bounds(ds)
    ds = normalize_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    ds = ds.sel(time=slice(start_iso, end_iso))
    if ds.sizes.get("time", 0) == 0:
        raise DataError(
            f"{model}/{experiment}/{var} has no data in "
            f"{start_iso}..{end_iso} (dataset time range may not cover the window)."
        )

    print(
        f"Fetching cmip6 {model}/{experiment}/{member}/{table}/"
        f"{var}/{grid_label} {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    bbox_raw = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox else None
    history_line = (
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} cmip6-fetch: "
        f"subset {var} to {start_iso}..{end_iso}"
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
        f"cmip6:{model}/{experiment}/{member}/{table}/{var}/{grid_label}"
    )
    ds.attrs = new_globals

    _ensure_coord_cf_attrs(ds)

    units = ds[var].attrs.get("units")
    if units is None:
        raise DataError(
            f"variable {var!r} has no `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    if not str(units).strip():
        raise DataError(
            f"variable {var!r} has a blank `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    units_exc = udunits_error(units)
    if units_exc is not None:
        raise DataError(
            f"variable {var!r} has units {units!r}, which is not a valid "
            f"udunits string ({units_exc}); refusing to write it under a CF-1.13 claim."
        )

    _verify_cf_decode(ds, var)

    if state["source_time_units"] is not None:
        ds["time"].encoding["units"] = state["source_time_units"]
    ds["time"].encoding["calendar"] = state["source_calendar"]
    for v, fill in fills.items():
        if fill is not None:
            ds[v].encoding["_FillValue"] = fill

    return ds, EntryOverride(
        args={"start_time": start_iso, "end_time": end_iso, "variable": [var]}
    )


if __name__ == "__main__":
    fetch()
