# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
#   "dask",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch IMERG live precipitation and write a WeatherSkills standard dataset."""

import sys
import tempfile
from datetime import UTC, date, datetime, timedelta

from weather_skills_core import EntryOverride, Types, UsageError, weather_skill
from weather_skills_core.dataset import stamp_cf_attrs

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.14"

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}

# How far back from today the `latest`-granule discovery search looks. This is
# independent of the requested window length: it only needs to span the
# product's realistic production lag (IMERG late runs a few days behind; final
# can run months behind), not the window. The actual window is fetched by a
# separate search over the exact resolved [start, end] (see fetch()), so a wide
# discovery lookback never inflates the fetched data.
_DISCOVERY_LOOKBACK_DAYS = 200


class GranuleShapeError(Exception):
    """Raised when a CMR granule result lacks the expected temporal-extent path
    or carries a non-ISO BeginningDateTime, so the failure surfaces as a clear
    message rather than an uncaught KeyError/ValueError traceback after login."""


def _granule_date(result) -> date:
    """Extract the start date of an earthaccess granule's temporal coverage.

    Raises GranuleShapeError if the expected
    ``umm.TemporalExtent.RangeDateTime.BeginningDateTime`` path is missing or
    its value is not a parseable ISO 8601 date, so a malformed CMR result yields
    a clear error rather than an uncaught traceback.
    """
    try:
        iso = result["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    except (KeyError, TypeError) as exc:
        raise GranuleShapeError(
            "CMR granule result missing "
            "umm.TemporalExtent.RangeDateTime.BeginningDateTime; "
            f"cannot determine its date (got: {result!r})"
        ) from exc
    # BeginningDateTime is an ISO 8601 instant (e.g. "2026-05-29T00:00:00.000Z");
    # the leading 10 chars are the calendar date.
    if not isinstance(iso, str):
        raise GranuleShapeError(f"CMR granule BeginningDateTime is not a string: {iso!r}")
    try:
        return date.fromisoformat(iso[:10])
    except ValueError as exc:
        raise GranuleShapeError(
            f"CMR granule BeginningDateTime {iso!r} is not a parseable ISO date"
        ) from exc


def _discover_latest(shortname: str) -> date:
    """Find the newest available granule on or before today (UTC) and return its
    date — the `latest` resolver for IMERG.

    Searches a lookback window ending today whose width is set by the product's
    realistic production lag (``_DISCOVERY_LOOKBACK_DAYS``), not by the requested
    window length. The resolved window is fetched separately over its exact
    ``[start, end]`` (see ``fetch()``), so this discovery search only has to
    surface the most-recent granule. Returns the max granule date ``<= today``.
    Exits 2 if no granule falls in that lookback. Requires earthaccess.login()
    to have been called.
    """
    import earthaccess

    today = datetime.now(UTC).date()
    lookback_start = today - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(lookback_start.isoformat(), today.isoformat()),
    )
    try:
        on_or_before = [r for r in results if _granule_date(r) <= today]
    except GranuleShapeError as exc:
        raise UsageError(str(exc)) from exc
    if not on_or_before:
        raise UsageError(
            f"no IMERG {shortname} granules available in lookback window "
            f"{lookback_start.isoformat()}..{today.isoformat()}; cannot resolve "
            "'latest'."
        )
    return max(_granule_date(r) for r in on_or_before)


def _resolve(d, shortname: str):
    if d != "latest":
        return d
    import earthaccess

    earthaccess.login()
    return _discover_latest(shortname)


@weather_skill(
    name="imerg-fetch",
    version=_SKILL_VERSION,
    outputs=[Types.GRIDDED],
    required_args=("start_time", "end_time"),
    check_cache=True,
    source="imerg",
)
@weather_skill.argument(
    "--version",
    default="late",
    choices=list(SHORTNAMES),
    help="IMERG product version (late or final).",
)
def fetch(start_time, end_time, version):
    """Fetch IMERG live precipitation and write a WeatherSkills standard dataset."""
    import earthaccess
    import numpy as np
    import xarray as xr

    shortname = SHORTNAMES[version]
    start_time, end_time = _resolve(start_time, shortname), _resolve(end_time, shortname)
    start = start_time.isoformat()
    end = end_time.isoformat()

    print(
        f"Fetching IMERG {version} ({shortname}) {start} -> {end}",
        file=sys.stderr,
    )

    earthaccess.login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(start, end),
    )
    if not results:
        raise RuntimeError(f"No IMERG {version} granules found in {start}..{end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    requested_span = (end_time - start_time).days + 1
    effective_end = end

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(
            files,
            engine="h5netcdf",
            combine="by_coords",
        )
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        spatial_rename = {
            src: dst
            for src, dst in {"lat": "latitude", "lon": "longitude"}.items()
            if src in ds.dims
        }
        if spatial_rename:
            ds = ds.rename(spatial_rename)
        ds = ds.sel(time=slice(start, end))

        present_days = sorted({np.datetime64(t, "D").item() for t in ds["time"].values})
        present_days = [d for d in present_days if start_time <= d <= end_time]
        covered_days = len(present_days)
        if covered_days == 0:
            raise UsageError(f"no granule day falls within {start}..{end}")
        if covered_days < requested_span:
            last_present = present_days[-1]
            expected_tail = [
                start_time + timedelta(days=i)
                for i in range(requested_span)
                if start_time + timedelta(days=i) > last_present
            ]
            missing_days = sorted(
                {start_time + timedelta(days=i) for i in range(requested_span)} - set(present_days)
            )
            if missing_days != expected_tail:
                raise UsageError(
                    f"non-trailing missing day(s) "
                    f"{', '.join(d.isoformat() for d in missing_days)} within "
                    f"{start}..{end} — server/data gap, not lag. Refusing to write "
                    "a partial zarr with a hole in the middle."
                )
            effective_end = last_present.isoformat()
            print(
                f"WARNING: requested {requested_span} days ({start}..{end}) but only "
                f"{covered_days} distinct day(s) are present in that span; writing the "
                f"available days through {effective_end} (trailing days not yet "
                "published, or near the dataset start). Caching the effective window "
                "so a later request for the full window re-fetches the missing tail.",
                file=sys.stderr,
            )

        ds = ds.drop_attrs()
        ds.attrs.update(Conventions="CF-1.13")
        ds["precip"].attrs.update(
            units="mm day-1",
            standard_name="lwe_precipitation_rate",
            long_name="IMERG daily precipitation",
        )
        stamp_cf_attrs(ds)
        ds = ds.load()
        return ds, EntryOverride(args={"start_time": start, "end_time": effective_end})


if __name__ == "__main__":
    fetch()
