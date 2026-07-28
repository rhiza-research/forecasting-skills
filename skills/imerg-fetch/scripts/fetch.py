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
"""Fetch IMERG live precipitation and write a weather-skills envelope Zarr."""

import sys
import tempfile
from datetime import UTC, date, datetime, timedelta

from weather_skills_core import EntryOverride, UsageError, types, weather_skill
from weather_skills_core.envelope import stamp_cf_attrs

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


def _latest(args) -> date:
    """`latest` resolver hook: log in, then discover the newest granule.

    The decorator memoizes the resolver (a window referencing `latest` at both
    ends discovers once) and invokes it lazily on first use, so an all-absolute
    or now-only window performs no discovery login. The main fetch calls
    earthaccess.login() again — login is idempotent, and on a non-`latest` run
    it is the first and only login.
    """
    import earthaccess

    earthaccess.login()
    return _discover_latest(SHORTNAMES[args.version])


@weather_skill(
    "imerg-fetch",
    _SKILL_VERSION,
    output_type=types.GRIDDED,
    source="imerg",
    start_time=True,
    end_time=True,
    extra_args=[("--version", {"default": "late", "choices": list(SHORTNAMES)})],
    latest_resolver=_latest,
    streaming=True,
)
def fetch(args):
    """Fetch IMERG live precipitation and write a weather-skills envelope Zarr."""
    start_time, end_time, version = args["start_time"], args["end_time"], args["version"]
    import earthaccess
    import xarray as xr

    shortname = SHORTNAMES[version]
    start = start_time.isoformat()
    end = end_time.isoformat()

    print(
        f"Fetching IMERG {version} ({shortname}) {start} -> {end}",
        file=sys.stderr,
    )

    # Fetch over the EXACT resolved [start, end]. If a `latest` token already
    # logged in during discovery, login() is idempotent; otherwise this is the
    # first and only login.
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

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(
            files,
            engine="h5netcdf",
            combine="by_coords",
        )
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        # The IMERG source names its spatial dims `lat`/`lon`; normalize to the
        # canonical `latitude`/`longitude` used across the other fetchers and
        # ENVELOPE.md. Conditional so the rename only touches dims present.
        spatial_rename = {
            src: dst
            for src, dst in {"lat": "latitude", "lon": "longitude"}.items()
            if src in ds.dims
        }
        if spatial_rename:
            ds = ds.rename(spatial_rename)
        # CMR's temporal filter is overlap-based and can return granules just
        # outside [start, end]; trim to exact requested bounds to match the
        # prior sheerwater @timeseries() post-process.
        ds = ds.sel(time=slice(start, end))

        # Short-window protection (mirrors chirps-fetch's tail-vs-mid-gap
        # handling). IMERG late runs a few days behind realtime, so a window
        # whose end is at or near today (e.g. `--end now`) can resolve to a span
        # whose trailing days are not yet published. The present-day set is
        # derived from the WRITTEN dataset's own time axis (after the
        # sel(time=slice) trim above), NOT from CMR BeginningDateTime metadata,
        # so the cache stamp matches the data actually written.
        import numpy as np

        # np.datetime64(t, "D") truncates each ns timestamp to its calendar day;
        # .item() on a datetime64[D] yields a datetime.date.
        present_days = sorted({np.datetime64(t, "D").item() for t in ds["time"].values})
        # ds.sel already restricts to [start, end]; defensively re-bound in case
        # any boundary granule slipped through the slice.
        present_days = [d for d in present_days if start_time <= d <= end_time]
        covered_days = len(present_days)
        if covered_days == 0:
            # The granules search returned overlap granules just outside the
            # bounds, but no granule day falls inside [start, end]: nothing
            # in-window to write.
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
                # Interior hole: a missing day precedes a later present day. This
                # is a server/data gap, not realtime lag; refuse to silently cache
                # a window with a hole in the middle (mirrors chirps).
                raise UsageError(
                    f"non-trailing missing day(s) "
                    f"{', '.join(d.isoformat() for d in missing_days)} within "
                    f"{start}..{end} — server/data gap, not lag. Refusing to write "
                    "a partial zarr with a hole in the middle."
                )
            # Contiguous trailing gap: warn and stamp the EFFECTIVE end (last day
            # actually present) so a later run re-fetches the now-published tail
            # instead of short-circuiting on a cache hit against a partial window.
            effective_end_iso = last_present.isoformat()
            print(
                f"WARNING: requested {requested_span} days ({start}..{end}) but only "
                f"{covered_days} distinct day(s) are present in that span; writing the "
                f"available days through {effective_end_iso} (trailing days not yet "
                "published, or near the dataset start). Caching the effective window "
                "so a later request for the full window re-fetches the missing tail.",
                file=sys.stderr,
            )
            yield EntryOverride({"end": effective_end_iso})

        ds = ds.drop_attrs()
        ds.attrs.update(Conventions="CF-1.13")
        ds["precip"].attrs.update(
            units="mm day-1",
            standard_name="lwe_precipitation_rate",
            long_name="IMERG daily precipitation",
        )
        stamp_cf_attrs(ds)

        # The decorator's to_zarr streams the lazy open_mfdataset one
        # granule-chunk at a time (open_mfdataset chunks per file = per day), so
        # peak resident memory is bounded to ~one granule rather than the whole
        # window. The dataset is yielded (and therefore written) inside the
        # with-block so the downloaded source netCDFs are still on disk while
        # the streamed write pulls each chunk.
        yield ds


if __name__ == "__main__":
    fetch()
