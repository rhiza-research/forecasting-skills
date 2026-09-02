# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "dynamical-catalog==0.5.0",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a dynamical.org open-catalog dataset and write a weather-skills standard dataset Zarr."""

import re
import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
)
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Forecast bookkeeping / CRS scalar coords dropped from the standard output.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

# Catalog stores selected pressure-level fields as separate 2-D variables
# (`temperature_850hpa`). Stack those onto the ontology `vertical` dim.
_HPA_RE = re.compile(r"^(.+)_(\d+)hpa$")
_HPA_ALIASES = {"t": "temperature", "gh": "geopotential_height"}
# Other fetchers (TAHMO, ARCO, S2S) use these; dynamical.org stores surface precip as
# precipitation_surface (IMERG, GEFS, IFS-ENS, …).
_PRECIP_ALIASES = {
    "precip": "precipitation_surface",
    "precipitation": "precipitation_surface",
    "tp": "precipitation_surface",
    "total_precipitation": "precipitation_surface",
    "pr": "precipitation_surface",
}


def _hpa_by_prefix(data_vars) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append(name)
    return groups


def _resolve_variables(requested, data_vars, dataset: str):
    """Map `-v` tokens onto catalog names.

    Catalog-exact names pass through. A prefix (`temperature`) or short alias
    (`t`, `gh`) expands to every `*_Nhpa` field of that prefix — not to
    height-above-ground companions like `temperature_2m`. Common precip
    nicknames (`precip`, `precipitation`, `tp`, `total_precipitation`) map to
    `precipitation_surface` when that field exists.
    """
    if not requested:
        return None
    name_set = set(data_vars)
    hpa = _hpa_by_prefix(data_vars)
    resolved: list[str] = []
    seen: set[str] = set()
    missing: list[str] = []

    def _take(name: str) -> None:
        if name not in seen:
            resolved.append(name)
            seen.add(name)

    for token in requested:
        prefix = _HPA_ALIASES.get(token, token)
        if token in name_set:
            _take(token)
            continue
        kids = hpa.get(prefix)
        if kids:
            for kid in sorted(kids, key=lambda n: -int(_HPA_RE.match(n).group(2))):
                _take(kid)
            continue
        aliased = _PRECIP_ALIASES.get(token)
        if aliased in name_set:
            _take(aliased)
            continue
        surface = f"{token}_surface"
        if surface in name_set:
            _take(surface)
            continue
        missing.append(token)
    if missing:
        raise UsageError(
            f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(data_vars))}"
        )
    return resolved


def _stack_pressure_levels(ds):
    """Combine `*_Nhpa` variables into one field with a `vertical` (hPa) dim."""
    import xarray as xr

    groups: dict[str, list[tuple[int, str]]] = {}
    for name in ds.data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append((int(match.group(2)), name))
    if not groups:
        return ds

    drop = [name for items in groups.values() for _, name in items]
    pieces = [ds.drop_vars(drop)]
    for prefix, items in groups.items():
        items = sorted(items, key=lambda it: -it[0])
        stacked = xr.concat(
            [ds[name].expand_dims(vertical=[float(hpa)]) for hpa, name in items],
            dim="vertical",
        )
        stacked.name = prefix
        stacked["vertical"].attrs.update(
            units="hPa",
            standard_name="air_pressure",
            long_name="pressure",
            positive="down",
            axis="Z",
        )
        pieces.append(stacked.to_dataset())
    return xr.merge(pieces)


def _open_dataset(state, dataset) -> dict:
    """Validate the dataset id, open it, and detect its shape, at most once per run."""
    if "ds" not in state:
        import dynamical_catalog

        catalog = dynamical_catalog.list()
        if dataset not in catalog:
            raise UsageError(
                f"unknown dataset {dataset!r}. Available datasets:\n  " + "\n  ".join(catalog)
            )
        ds = dynamical_catalog.open(dataset)

        if "latitude" not in ds.dims or "longitude" not in ds.dims:
            raise UsageError(
                f"{dataset} is on a projected grid (dims {tuple(ds.dims)}); this "
                "fetcher only handles regular 1-D latitude/longitude grids."
            )

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


@weather_skill(
    name="dynamical-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--date")
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dataset",
    required=True,
    help="Catalog dataset id (validated against dynamical_catalog.list()).",
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
def fetch(bbox, dataset, date, start_time, end_time, variable, **kwargs):
    """Fetch a dynamical.org open-catalog dataset and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        import numpy as np

        dsid = kwargs["probe_latest"] or dataset
        if not dsid:
            raise UsageError("--dataset is required (or pass it as --probe-latest <id>).")
        state = _open_dataset({}, dsid)
        coord = "init_time" if state["shape"] in ("ensemble", "forecast") else "time"
        print(np_to_date(np.max(state["ds"][coord].values)).isoformat())
        return

    import numpy as np

    state = _open_dataset({}, dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")

    if is_forecast:
        if date is None:
            raise UsageError(f"{dataset} is a forecast dataset; --date is required.")
        if start_time is not None or end_time is not None:
            raise UsageError(
                f"{dataset} is a forecast dataset; use --date, not --start-time/--end-time."
            )
        date_iso = date.isoformat()
    else:
        if start_time is None or end_time is None:
            raise UsageError(
                f"{dataset} is an analysis dataset; --start-time and --end-time are required."
            )
        if date is not None:
            raise UsageError(
                f"{dataset} is an analysis dataset; use --start-time/--end-time, not --date."
            )
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()

    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")

    if is_forecast:
        inits = ds["init_time"].values
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
        ds = ds.assign_coords(time=ds["init_time"]).drop_vars("init_time")
    else:
        ds = ds.sel(time=slice(np.datetime64(start_iso), np.datetime64(end_iso)))
        if ds.sizes.get("time", 0) == 0:
            raise DataError(f"{dataset} has no data in {start_iso}..{end_iso}.")
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    if variable:
        ds = ds[_resolve_variables(variable, ds.data_vars, dataset)]
    ds = _stack_pressure_levels(ds)

    print(f"Fetching dynamical:{dataset} (shape={shape})", file=sys.stderr)

    ds.attrs.update(
        weather_skills_source=f"dynamical:{dataset}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)
    # Catalog datasets mix precip with dimensionless companions (e.g. IMERG
    # precipitation_quality_index_surface, units "1"). Convert each variable
    # independently so one inconvertible field does not abort the fetch.
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    return stamp_data_interval(out)


if __name__ == "__main__":
    fetch()
