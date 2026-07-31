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

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import to_standard_units
from weather_skills_core.standard_utils import require_env


# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.12"

# Bounded poll for retrieval jobs. ECDS retrievals run minutes to ~hour.
_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

LEADTIME_HOURS = ["0", "168", "240", "336", "480", "504", "672", "720", "840", "960", "1008"]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"

def _submit(client, request: dict):
    """Submit an s2s-forecasts retrieval; surface a clean message on the licence-not-accepted case."""
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

def _is_wrapped_area(area: list[float]) -> bool:
    """True if the bbox crosses +-180 (west > east), an RFC 7946 sec 5.2 box.

    ``area`` is [N, W, S, E]. resolve-region emits west > east for a country
    that straddles the antimeridian (Russia, Fiji). MARS ``area`` requires
    west < east west-to-east, so such a box must be split at +-180.
    """
    _, w, _, e = area
    return w > e

def _split_wrapped_area(area: list[float]) -> list[list[float]]:
    """Split a wrapped [N, W, S, E] (W > E) into two MARS-valid areas.

    Returns the western band [N, W, S, 180] and the eastern band [N, -180, S, E],
    each with west < east so MARS accepts it. For a non-wrapped area, returns the
    single area unchanged.
    """
    n, w, s, e = area
    if not _is_wrapped_area(area):
        return [area]
    return [[n, w, s, 180.0], [n, -180.0, s, e]]

def _concat_lon(datasets: list) -> object:
    """Concatenate per-area decoded datasets along longitude into one standard dataset.

    Each dataset covers a disjoint longitude band of a wrapped bbox. Concatenate
    along the longitude dim, then drop any duplicated shared edge (the +-180
    seam) and sort so the result is a single monotonic longitude axis.
    """
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
        # No identifiable longitude axis to concat on; fall back to the first
        # piece rather than guessing.
        return datasets[0]
    # Normalize each piece's longitude to a single [-180, 180) convention before
    # concatenating so the +-180 seam coincides. Without this, a western band
    # ending at 180.0 and an eastern band starting at -180.0 carry the same
    # meridian under two distinct float values; np.unique would treat them as
    # separate and the duplicate seam would survive. ((lon + 180) % 360) - 180
    # maps 180.0 to -180.0, so the two pieces' shared meridian becomes one value.
    normed = [
        d.assign_coords({lon_name: ((d[lon_name] + 180.0) % 360.0) - 180.0}) for d in datasets
    ]
    combined = xr.concat(normed, dim=lon_name)
    # Drop the now-coincident +-180 seam and any other repeated longitude, then
    # sort to a monotonic axis.
    _, unique_idx = np.unique(combined[lon_name].values, return_index=True)
    combined = combined.isel({lon_name: np.sort(unique_idx)})
    return combined.sortby(lon_name)

# Signature of the ECMWF S2S real-time embargo, matched on the failed job's
# error text. When a probed init falls inside the access-restricted window, the
# ECDS/MARS job fails and the failure surfaces with a message containing this
# phrase (e.g. "Restricted access to S2S data ..."). The relevant MARS exception
# type (MarsRuntimeError) is not reliably importable from the ecmwf.datastores
# stack, so detection is a substring match (case-insensitive) on the exception
# text rather than an isinstance check. The signature is deliberately this
# specific: a generic access/auth failure (e.g. one merely mentioning
# "AccessError") must NOT classify as embargo.
_S2S_EMBARGO_SIGNATURES = ("restricted access to s2s",)

# How many links of an exception's __cause__/__context__ chain
# _is_s2s_embargo_error inspects. Real chains here are one to three links; the
# bound guards against pathological or cyclic chains.
_EMBARGO_CHAIN_MAX_DEPTH = 8

def _is_s2s_embargo_error(exc: BaseException) -> bool:
    """True if `exc` is the ECMWF S2S real-time embargo (access-restriction) failure.

    The most recent S2S real-time data are access-restricted (a window of
    variable width); probing such an init makes the ECDS/MARS job fail with a
    message containing "Restricted access to S2S". Such an init is not
    retrievable *yet* but is also not a genuine transport/auth/HTTP problem.

    Matching is defensive: the signature is checked against str() and the
    exception type name of `exc` AND of each exception in its
    __cause__/__context__ chain (bounded by _EMBARGO_CHAIN_MAX_DEPTH), all
    lowercased, so a wrapped or re-raised restriction message still classifies.
    The signature itself is narrow, so a generic access/auth error does not.
    """
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
    outputs=[["forecast", "ensemble_forecast"]]
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
        except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't traceback
            raise DataError(
                f"ECDS submit failed for init {date_iso} ({exc}); this is a transport/auth problem."
            ) from None

        # Bounded poll with the same taxonomy as the probe (reusing its
        # poll-interval/cap constants):
        #   - a failure matching the S2S embargo signature means the requested
        #     init is access-restricted (inside the real-time embargo). For an
        #     explicit --date there is no stepping back: exit non-zero saying so
        #     and pointing at 'latest' / an older init.
        #   - ProcessingFailedError (ECDS marked a leg failed/rejected/dismissed)
        #     otherwise means this init is not retrievable — most often because
        #     the requested --date is not a valid S2S init day. Exit non-zero
        #     with a clear message rather than a traceback.
        #   - a transport/auth error on poll is surfaced and exits non-zero.
        #   - still-not-ready at the wall-clock cap is a stuck job: abort rather
        #     than looping forever.
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
            except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't step
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
        # Decode each leg, then concatenate the sub-area pieces of each forecast
        # type along longitude (a no-op for a non-wrapped, single-sub-area fetch).
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
        # Stamp explicit units on tp so downstream consumers don't have to reverse-engineer
        # them from value ranges. GRIB carries `kg m-2` (numerically equivalent to mm depth
        # over the accumulation period); normalize to standard mm precip amount.
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
        ds = to_standard_units(ds, variables=["tp"])

        # The decoded dataset is lazily backed by the GRIB files in the
        # temporary directory, which is removed when this block exits; the
        # decorator writes the returned dataset after that, so materialize the
        # values while the files are still alive.
        ds = ds.load()

    return ds

if __name__ == "__main__":
    fetch()
