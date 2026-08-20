# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime",
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "cfgrib",
#   # eccodeslib carries the native libeccodes that cfgrib loads on macOS/Linux;
#   # eccodes drops this as a transitive dep via PyPI metadata (ecmwf/eccodes-python#150),
#   # so it is declared directly. Windows bundles the library in eccodes itself.
#   "eccodeslib",
#   "zarr",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch ECMWF S2S ensemble fields (cf + pf) and write a weather-skills standard dataset Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.probe import PROBE_LATEST_KWARGS
from weather_skills_core.standard_utils import require_env
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

LEADTIME_HOURS = ["0", "168", "240", "336", "480", "504", "672", "720", "840", "960", "1008"]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"

# Embargo detection: match this phrase on the exception chain (MarsRuntimeError
# is not reliably importable from ecmwf.datastores). Keep narrow so generic
# access/auth failures do not classify as embargo.
_S2S_EMBARGO_SIGNATURES = ("restricted access to s2s",)
_EMBARGO_CHAIN_MAX_DEPTH = 8

# Keep dim-like coords; drop GRIB filter scalars that collide across parameters.
_KEEP_COORDS = frozenset(
    {"time", "step", "latitude", "longitude", "number", "valid_time", "lat", "lon"}
)


class _Var(NamedTuple):
    ecds: str
    family: str  # instant = integer hours; daily = 24 h averaging windows


# Most-used first. `-v` / Zarr / metadata.variables tokens — not ARCO or ECDS spellings.
VARIABLES: dict[str, _Var] = {
    "tp": _Var("total_precipitation", "instant"),
    "t2m": _Var("2_m_temperature", "daily"),
    "d2m": _Var("2_m_dewpoint_temperature", "daily"),
    "mx2t6": _Var("maximum_2_m_temperature_in_the_last_6_hours", "instant"),
    "mn2t6": _Var("minimum_2_m_temperature_in_the_last_6_hours", "instant"),
    "u10": _Var("10_m_u_component_of_wind", "instant"),
    "v10": _Var("10_m_v_component_of_wind", "instant"),
    "msl": _Var("mean_sea_level_pressure", "instant"),
    "cape": _Var("convective_available_potential_energy", "daily"),
    "tcw": _Var("total_column_water", "daily"),
}
DEFAULT_VARIABLES = ["tp"]

# cfgrib shortName / cfVarName → canonical `-v` name.
_GRIB_ALIASES = {
    "tp": "tp",
    "t2m": "t2m",
    "2t": "t2m",
    "d2m": "d2m",
    "2d": "d2m",
    "mx2t6": "mx2t6",
    "mn2t6": "mn2t6",
    "u10": "u10",
    "10u": "u10",
    "v10": "v10",
    "10v": "v10",
    "msl": "msl",
    "cape": "cape",
    "tcw": "tcw",
}


def _canonical_name(token: str) -> str | None:
    if token in VARIABLES:
        return token
    for short, spec in VARIABLES.items():
        if token == spec.ecds:
            return short
    return None


def _resolve_variables(raw: list[str] | None) -> list[str]:
    tokens = raw or list(DEFAULT_VARIABLES)
    unknown = [token for token in tokens if _canonical_name(token) is None]
    if unknown:
        raise UsageError(
            f"unknown variable(s): {', '.join(unknown)}.\n"
            f"Available (most used first): {', '.join(VARIABLES)}"
        )
    resolved: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        name = _canonical_name(token)
        if name not in seen:
            seen.add(name)
            resolved.append(name)
    return resolved


def _group_by_family(names: list[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(VARIABLES[name].family, []).append(name)
    return list(groups.items())


def _daily_leadtimes(hours: list[str]) -> list[str]:
    """Map instant lead hours onto ECDS daily-averaged `start_end` windows."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in hours:
        end = int(raw)
        token = "0_24" if end == 0 else f"{end - 24}_{end}"
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _submit(client, request: dict):
    """Submit an s2s-forecasts retrieval; surface a clean message on licence-not-accepted."""
    import requests

    try:
        return client.submit("s2s-forecasts", request)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if (
            resp is not None
            and resp.status_code == 403
            and "licences not accepted" in str(e).lower()
        ):
            raise DataError(
                "ERROR: ECDS retrieval blocked: required licences not accepted on s2s-forecasts.\n"
                f"Action: open {S2S_LICENCE_URL} in a browser, log in to ECDS, "
                "accept the required licences, then re-run this skill.",
                prefix=False,
            ) from None
        raise


def _build_request(
    date_iso: str,
    area: list[float],
    forecast_type: str,
    variables: list[str] | tuple[str, ...] = ("tp",),
) -> dict:
    names = list(variables)
    families = {VARIABLES[name].family for name in names}
    if len(families) != 1:
        raise ValueError("internal: one leadtime family per ECDS request")
    family = next(iter(families))
    leadtimes = LEADTIME_HOURS if family == "instant" else _daily_leadtimes(LEADTIME_HOURS)
    d = dt.date.fromisoformat(date_iso)
    return {
        "origin": "ecmwf",
        "level_type": "single_level",
        "variable": [VARIABLES[name].ecds for name in names],
        "year": [str(d.year)],
        "month": [f"{d.month:02d}"],
        "day": [f"{d.day:02d}"],
        "time": ["00:00"],
        "leadtime_hour": leadtimes,
        "forecast_type": forecast_type,
        "area": area,
        "data_format": "grib",
    }


def _split_wrapped_area(area: list[float]) -> list[list[float]]:
    """Split antimeridian-crossing [N, W, S, E] (W > E) into two MARS-valid areas."""
    n, w, s, e = area
    if w <= e:
        return [area]
    return [[n, w, s, 180.0], [n, -180.0, s, e]]


def _concat_lon(datasets: list) -> object:
    """Concatenate per-area datasets along longitude; drop duplicated +-180 seam."""
    import numpy as np
    import xarray as xr

    if len(datasets) == 1:
        return datasets[0]
    lon_name = None
    for cand in ("longitude", "lon", "x"):
        if cand in datasets[0].coords or cand in datasets[0].dims:
            lon_name = cand
            break
    if lon_name is None:
        return datasets[0]
    normed = [
        d.assign_coords({lon_name: ((d[lon_name] + 180.0) % 360.0) - 180.0}) for d in datasets
    ]
    combined = xr.concat(normed, dim=lon_name)
    _, unique_idx = np.unique(combined[lon_name].values, return_index=True)
    return combined.isel({lon_name: np.sort(unique_idx)}).sortby(lon_name)


def _drop_grib_filters(ds):
    drop = [name for name in ds.coords if name not in ds.dims and name not in _KEEP_COORDS]
    return ds.drop_vars(drop) if drop else ds


def _open_grib(path: Path):
    """Decode a (possibly mixed-parameter) GRIB into one Dataset."""
    import cfgrib
    import xarray as xr

    parts = cfgrib.open_datasets(str(path))
    if not parts:
        raise DataError(f"ECDS GRIB {path.name} contained no messages.")
    cleaned = [_drop_grib_filters(part) for part in parts]
    if len(cleaned) == 1:
        return cleaned[0]
    return xr.merge(cleaned, compat="override", combine_attrs="override")


def _rename_to_short(ds, requested: list[str]):
    """Map cfgrib names onto canonical `-v` tokens and keep only those fields."""
    rename = {}
    for name in ds.data_vars:
        short = _GRIB_ALIASES.get(name)
        if short is not None and short in requested and short != name:
            rename[name] = short
    if rename:
        ds = ds.rename(rename)
    extra = [name for name in ds.data_vars if name not in requested]
    if extra:
        ds = ds.drop_vars(extra)
    missing = [name for name in requested if name not in ds.data_vars]
    if missing:
        have = ", ".join(ds.data_vars) if list(ds.data_vars) else "none"
        raise DataError(
            f"ECDS GRIB did not contain {', '.join(missing)} (decoded: {have})."
        )
    return ds[requested]


def _standardize(ds):
    """CF attrs + standard units. S2S ``tp`` is cumulative; convert to a rate."""
    stamp_cf_attrs(ds)
    if "tp" in ds.data_vars:
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


def _is_s2s_embargo_error(exc: BaseException) -> bool:
    """True if `exc` (or its cause/context chain) matches the S2S real-time embargo."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < _EMBARGO_CHAIN_MAX_DEPTH:
        seen.add(id(cur))
        parts.append(str(cur))
        parts.append(type(cur).__name__)
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    haystack = " ".join(parts).lower()
    return any(sig in haystack for sig in _S2S_EMBARGO_SIGNATURES)


@weather_skill(
    name="ecmwf-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--bbox", required=True)
@weather_skill.argument("--date", required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help=(
        "S2S field to retrieve (repeatable). Most used first: tp, t2m. "
        "Default tp. Names are cfgrib short names, not ARCO 2m_temperature "
        "or ECDS 2_m_temperature."
    ),
)
@weather_skill.argument("--probe-latest", **PROBE_LATEST_KWARGS)
def fetch(bbox, date, variable, **kwargs):
    """Fetch ECMWF S2S ensemble fields (cf + pf) and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        # ECDS has no cheap public date list; 2-day embargo, Mon/Thu before 2023-06-27.
        day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
        if day < dt.date(2023, 6, 27):
            while day.weekday() not in (0, 3):
                day -= dt.timedelta(days=1)
        print(day.isoformat())
        return

    date_iso = date.isoformat()
    area = list(bbox)
    names = _resolve_variables(variable)

    require_env("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")

    import xarray as xr
    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    print(
        f"Fetching ECMWF S2S for area={area} date={date_iso} variables={','.join(names)}",
        file=sys.stderr,
    )
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        sub_areas = _split_wrapped_area(area)
        client = Client()
        groups = _group_by_family(names)

        legs = []
        for family, family_vars in groups:
            for forecast_type, short in (
                ("control_forecast", "cf"),
                ("perturbed_forecast", "pf"),
            ):
                for i, sub in enumerate(sub_areas):
                    legs.append(
                        {
                            "family": family,
                            "family_vars": family_vars,
                            "forecast_type": forecast_type,
                            "short": short,
                            "area": sub,
                            "grib": tmp / f"{short}_{family}_{i}.grib",
                            "remote": None,
                        }
                    )

        print(f"Submitting {len(legs)} retrieval leg(s)...", file=sys.stderr)
        try:
            for leg in legs:
                req = _build_request(
                    date_iso, leg["area"], leg["forecast_type"], leg["family_vars"]
                )
                leg["remote"] = _submit(client, req)
        except DataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"ECDS submit failed for init {date_iso} ({exc}); this is a transport/auth problem."
            ) from None

        remotes = [leg["remote"] for leg in legs]
        waited = 0
        while True:
            try:
                if all(r.results_ready for r in remotes):
                    break
            except ProcessingFailedError as exc:
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the 2-day S2S real-time embargo "
                        f"(access-restricted) ({exc}); use an older init date."
                    ) from None
                raise DataError(
                    f"ECDS reported no data for init {date_iso} ({exc}); "
                    "there may be no published S2S real-time forecast for that date "
                    "(daily since 2023-06-27; Mondays/Thursdays only before then), "
                    "or the init is not yet available — try another date."
                ) from None
            except Exception as exc:  # noqa: BLE001
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the 2-day S2S real-time embargo "
                        f"(access-restricted) ({exc}); use an older init date."
                    ) from None
                raise DataError(
                    f"polling ECDS job for init {date_iso} failed ({exc}); "
                    "this is a transport/auth problem."
                ) from None
            if waited >= _PROBE_POLL_MAX_SECONDS:
                raise DataError(
                    f"ECDS job for init {date_iso} was still not ready after "
                    f"{_PROBE_POLL_MAX_SECONDS}s; the job is stuck. Re-run, or pass a "
                    "different init date."
                )
            time.sleep(_PROBE_POLL_SECONDS)
            waited += _PROBE_POLL_SECONDS

        for leg in legs:
            print(f"Downloading {leg['grib'].name}...", file=sys.stderr)
            leg["remote"].download(str(leg["grib"]))

        print("Decoding GRIB and writing Zarr...", file=sys.stderr)
        family_ensembles = []
        for family, family_vars in groups:
            cf_parts = [
                _open_grib(leg["grib"])
                for leg in legs
                if leg["family"] == family and leg["forecast_type"] == "control_forecast"
            ]
            pf_parts = [
                _open_grib(leg["grib"])
                for leg in legs
                if leg["family"] == family and leg["forecast_type"] == "perturbed_forecast"
            ]
            cf = _concat_lon(cf_parts).assign_coords(number=0)
            pf = _concat_lon(pf_parts)
            ens = xr.concat([pf, cf], dim="number").sortby("number")
            family_ensembles.append(_rename_to_short(ens, family_vars))
        ds = (
            family_ensembles[0]
            if len(family_ensembles) == 1
            else xr.merge(family_ensembles, join="outer", compat="override")
        )
        ds = ds[names]
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-s2s")
        ds = _standardize(ds)
        # Materialize while GRIB files in the temp dir still exist.
        ds = ds.load()

    return ds


if __name__ == "__main__":
    fetch()
