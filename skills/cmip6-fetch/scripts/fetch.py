# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "pandas",
#   "cftime",
#   "cf_xarray",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a weather-skills standard dataset Zarr."""

import sys
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe)
import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
import gcsfs
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs, udunits_error
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    normalize_longitude,
    verify_cf_decode,
)
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_CATALOG_URL = "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv"
_CF_CONVENTIONS = "CF-1.13"


def _resolve_zstore(model, experiment, variable, member, table, grid) -> tuple:
    """Resolve facet flags against the CMIP6 catalog to exactly one zstore."""
    try:
        df = pd.read_csv(_CATALOG_URL)
    except Exception as exc:  # noqa: BLE001
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
    row = sub.sort_values("version").iloc[-1]
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


def _open_remote(state, model, experiment, variable, member, table, grid) -> dict:
    """Resolve the catalog and open the matched store, at most once per run."""
    if "ds" not in state:
        zstore, grid_label, version = _resolve_zstore(
            model, experiment, variable, member, table, grid
        )
        state.update(zstore=zstore, grid_label=grid_label, version=version)
        fs = gcsfs.GCSFileSystem(token="anon")
        mapper = fs.get_mapper(zstore)
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
                "lat/lon grids."
            )
        state.update(
            ds=ds,
            source_calendar=source_calendar,
            source_time_units=source_time_units,
        )
    return state


def _drop_bounds(ds):
    """Drop *_bnds/*_bounds variables and orphaned bounds attrs/dims."""
    bounds_vars = [
        v for v in ds.variables if str(v).endswith("_bnds") or str(v).endswith("_bounds")
    ]
    if bounds_vars:
        ds = ds.drop_vars(bounds_vars)
    for dim_coord in ("bnds", "bounds", "nv"):
        if dim_coord in ds.coords and dim_coord not in ds.dims:
            ds = ds.drop_vars(dim_coord)
    for v in ds.variables:
        if "bounds" in ds[v].attrs:
            del ds[v].attrs["bounds"]
    return ds


@weather_skill(
    name="cmip6-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", required=True)
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
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(start_time, end_time, bbox, model, experiment, variable, member, table, grid, **kwargs):
    """Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        print("none")
        return

    state = _open_remote({}, model, experiment, variable, member, table, grid)
    ds = state["ds"]
    grid_label = state["grid_label"]
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    if variable not in ds.data_vars:
        raise UsageError(
            f"variable {variable!r} not present in the dataset; available: {sorted(ds.data_vars)}"
        )

    source_globals = dict(ds.attrs)
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

    bbox_label = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else None
    history_line = (
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} cmip6-fetch: "
        f"subset {variable} to {start_iso}..{end_iso}"
        + (f" bbox {bbox_label}" if bbox_label else "")
        + f"; mapped onto the weather-skills standard dataset and re-stamped {_CF_CONVENTIONS}."
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

    stamp_cf_attrs(ds)

    units = ds[variable].attrs.get("units")
    if units is None or not str(units).strip():
        raise DataError(
            f"variable {variable!r} has no `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    units_exc = udunits_error(units)
    if units_exc is not None:
        raise DataError(
            f"variable {variable!r} has units {units!r}, which is not a valid "
            f"udunits string ({units_exc}); refusing to write it under a CF-1.13 claim."
        )

    verify_cf_decode(ds)
    fills = {v: ds[v].encoding.get("_FillValue") for v in ds.data_vars}
    apply_write_encoding(
        ds,
        time_units=state["source_time_units"],
        time_calendar=state["source_calendar"],
        fills=fills,
    )
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()
