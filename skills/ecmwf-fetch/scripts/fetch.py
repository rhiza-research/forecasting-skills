# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
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
#   "cf-units>=3.3",
# ]
# ///
"""Fetch ECMWF S2S precipitation (cf + pf) and write a weather-skills standard dataset Zarr."""

import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import require_env
from weather_skills_core.units import to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.12"

_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

LEADTIME_HOURS = ["0", "168", "240", "336", "480", "504", "672", "720", "840", "960", "1008"]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"

# Embargo detection: match this phrase on the exception chain (MarsRuntimeError
# is not reliably importable from ecmwf.datastores). Keep narrow so generic
# access/auth failures do not classify as embargo.
_S2S_EMBARGO_SIGNATURES = ("restricted access to s2s",)
_EMBARGO_CHAIN_MAX_DEPTH = 8


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


def _build_request(date_iso: str, area: list[float], forecast_type: str) -> dict:
    d = dt.date.fromisoformat(date_iso)
    return {
        "origin": "ecmwf",
        "level_type": "single_level",
        "variable": ["total_precipitation"],
        "year": [str(d.year)],
        "month": [f"{d.month:02d}"],
        "day": [f"{d.day:02d}"],
        "time": ["00:00"],
        "leadtime_hour": LEADTIME_HOURS,
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
    outputs=[["forecast", "ensemble_forecast"]],
)
@weather_skill.argument("--bbox", required=True)
@weather_skill.argument("--date", required=True)
def fetch(bbox, date, **kwargs):
    """Fetch ECMWF S2S precipitation (cf + pf) and write a weather-skills standard dataset Zarr."""
    date_iso = date.isoformat()
    area = list(bbox)

    require_env("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")

    import xarray as xr
    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    print(f"Fetching ECMWF S2S for area={area} date={date_iso}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        sub_areas = _split_wrapped_area(area)
        client = Client()

        legs = []
        for forecast_type, short in (("control_forecast", "cf"), ("perturbed_forecast", "pf")):
            for i, sub in enumerate(sub_areas):
                legs.append(
                    {
                        "forecast_type": forecast_type,
                        "short": short,
                        "area": sub,
                        "grib": tmp / f"{short}_{i}.grib",
                        "remote": None,
                    }
                )

        print(f"Submitting {len(legs)} retrieval leg(s)...", file=sys.stderr)
        try:
            for leg in legs:
                req = _build_request(date_iso, leg["area"], leg["forecast_type"])
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
                        f"init {date_iso} is inside the S2S real-time embargo "
                        f"(access-restricted) ({exc}); use an older init date."
                    ) from None
                raise DataError(
                    f"ECDS reported no data for init {date_iso} ({exc}); "
                    "it may not be a valid S2S init day. ECMWF S2S runs init on "
                    "fixed days — pass a known S2S init date."
                ) from None
            except Exception as exc:  # noqa: BLE001
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the S2S real-time embargo "
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
        cf_parts = [
            xr.open_dataset(leg["grib"], engine="cfgrib")
            for leg in legs
            if leg["forecast_type"] == "control_forecast"
        ]
        pf_parts = [
            xr.open_dataset(leg["grib"], engine="cfgrib")
            for leg in legs
            if leg["forecast_type"] == "perturbed_forecast"
        ]
        cf = _concat_lon(cf_parts).assign_coords(number=0)
        pf = _concat_lon(pf_parts)
        ds = xr.concat([pf, cf], dim="number").sortby("number")
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-s2s")
        stamp_cf_attrs(ds)
        # GRIB carries kg m-2 (numerically mm depth); normalize to standard precip amount.
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
        ds = to_standard_units(ds, variables=["tp"])
        # Materialize while GRIB files in the temp dir still exist.
        ds = ds.load()

    return ds


if __name__ == "__main__":
    fetch()
