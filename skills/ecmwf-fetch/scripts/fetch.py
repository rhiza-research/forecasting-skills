# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "cfgrib",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch ECMWF S2S precipitation (cf + pf) and write a weather-skills envelope Zarr."""

import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

from weather_skills_core import DataError, UsageError, WroteSummary, weather_skill
from weather_skills_core.envelope import parse_bbox, stamp_cf_attrs
from weather_skills_core.util import require_env

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"

# How far back from today the `latest` init probe looks. ECMWF S2S real-time
# data is access-restricted (embargoed) for a recent window whose width is
# variable: ECMWF documents restrictions on recent real-time data without a
# fixed public width, and the window has been observed as small as ~2 days in
# practice. 28 days is deliberate headroom over that variable embargo plus init
# cycles and production lag — not a measured 3-week constant. The probe must be
# able to step past the embargo to reach an init the user can actually
# retrieve. Exhausting the probe without a usable init exits non-zero.
_LATEST_LOOKBACK_DAYS = 28

# Bounded poll for a single `latest` probe day. ECDS retrievals run minutes to
# ~hour, so each probe job is polled every _PROBE_POLL_SECONDS up to a
# _PROBE_POLL_MAX_SECONDS wall-clock cap. A job still not results-ready at the
# cap is treated as stuck (not as "this init is unavailable"): the probe aborts
# with a clear message rather than looping forever or silently stepping back to
# an older init, since stepping back on a stuck-but-possibly-valid job would
# report a misleadingly old `latest`. See SKILL.md.
_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

# Global wall-clock budget for the whole `latest` discovery loop, across all
# probed init days. Each probe day is separately capped by
# _PROBE_POLL_MAX_SECONDS, but a sequence of slow probes could still run for
# many hours across the full lookback; this caps total discovery time so a
# degraded ECDS queue fails fast with a clear message instead of probing every
# lookback day at maximum per-day cost. Checked between days; it does not cut a
# probe off mid-poll.
_DISCOVERY_MAX_SECONDS = 3600.0

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
    """Concatenate per-area decoded datasets along longitude into one envelope.

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

# Cap on how much of the original exception text the embargo step-back line
# echoes. MARS error payloads can be long, and the step-back line prints once
# per embargoed probe day.
_EMBARGO_DETAIL_MAX_CHARS = 200


def _is_s2s_embargo_error(exc: BaseException) -> bool:
    """True if `exc` is the ECMWF S2S real-time embargo (access-restriction) failure.

    The most recent S2S real-time data are access-restricted (a window of
    variable width); probing such an init makes the ECDS/MARS job fail with a
    message containing "Restricted access to S2S". Such an init is not
    retrievable *yet* but is also not a genuine transport/auth/HTTP problem —
    during `latest` discovery it should be skipped (step back), not treated as
    fatal.

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


def _print_embargo_step_back(day: dt.date, exc: BaseException) -> None:
    """Print the stderr line used everywhere the probe steps past an embargoed init."""
    detail = str(exc)
    if len(detail) > _EMBARGO_DETAIL_MAX_CHARS:
        detail = detail[:_EMBARGO_DETAIL_MAX_CHARS] + "..."
    print(
        f"  {day.isoformat()} not accessible (S2S real-time embargo); stepping back ({detail})",
        file=sys.stderr,
    )


def _discover_latest(client, area: list[float]) -> tuple[dt.date, object]:
    """`latest` resolver for ECMWF S2S: newest accessible forecast init on or
    before today (UTC), found by probing init dates backward.

    For each candidate day back from today, submits a control-forecast retrieval
    over the requested area and polls it (bounded by ``_PROBE_POLL_MAX_SECONDS``)
    until results-ready. Two non-fatal cases step back one day: a job ECDS marks
    failed/rejected/dismissed because that init is not published, and a failure
    (on submit or on poll) that matches the S2S real-time embargo signature
    ("Restricted access to S2S" in the error text or its exception chain),
    because the most recent S2S real-time data are access-restricted. Since
    `latest` means "the newest init you can actually get", an embargoed init is
    skipped, not fatal — the probe steps past the embargo to the newest
    accessible init. The signature is the dividing line: ONLY an
    embargo-signature failure steps back. A credential/transport/HTTP failure
    does not match it, is surfaced, and the run exits non-zero — as does a job
    still not ready at the poll cap — so a stuck job or a credential problem is
    never silently misreported as a missing init.

    Returns ``(day, remote)`` for the winning init — the completed control
    retrieval — so the caller reuses it as the control leg rather than
    re-submitting it. This is the slow/async case (each probe is a real ECDS
    submit) — acceptable because it is opt-in. Bounded three ways: per-day
    polling by ``_PROBE_POLL_MAX_SECONDS``; the whole loop by
    ``_DISCOVERY_MAX_SECONDS`` (exceeding it exits 1); and the lookback by
    ``_LATEST_LOOKBACK_DAYS``. Exhausting the lookback without a usable init
    exits 2 — with an access-restriction message when every probed init was
    embargo-classified, else with the generic not-published message.

    Existence cost: ECDS exposes no metadata-only "does this init exist" query
    in the submit/poll model — a non-published init surfaces only as a
    ProcessingFailedError on poll, and a published init is confirmed only when
    its job reaches results_ready. So confirming the `latest` init's existence
    unavoidably polls one control retrieval to completion. When the resolved
    --date IS that probed init (bare `latest`), the fetch body reuses this
    completed retrieval as the control leg. When the resolved --date is an
    OFFSET off latest (`latest-Nd|w`), the probed init differs from the init
    the body fetches, so this completed control retrieval cannot be reused and
    is spent solely to confirm `latest` existed — one unavoidable probe
    retrieval. The offset init is then submitted exactly once in the body; the
    probed `latest` init is never re-submitted (it is only ever reused, never
    resubmitted), so no init is ever submitted twice.
    """
    from ecmwf.datastores.processing import ProcessingFailedError

    today = dt.datetime.now(dt.UTC).date()
    started = time.monotonic()
    probed = 0
    embargo_step_backs = 0
    for offset in range(_LATEST_LOOKBACK_DAYS + 1):
        if time.monotonic() - started > _DISCOVERY_MAX_SECONDS:
            raise DataError(
                f"latest discovery exceeded its time budget "
                f"({_DISCOVERY_MAX_SECONDS:.0f}s) after probing {probed} init(s); "
                "aborting. Re-run, or pass an explicit init date."
            )
        day = today - dt.timedelta(days=offset)
        req = _build_request(day.isoformat(), area, "control_forecast")
        print(f"Probing ECMWF init {day.isoformat()} for 'latest'...", file=sys.stderr)
        probed += 1
        # _submit handles the licence-not-accepted case (exits). A submit
        # failure matching the S2S embargo signature steps back one day, like
        # the poll-side embargo cases below. Any other submit failure
        # (transport/auth/HTTP) is a real error, not a missing init: surface it
        # and exit rather than stepping back.
        try:
            remote = _submit(client, req)
        except DataError:
            raise
        except Exception as exc:  # noqa: BLE001 -- classify; do not misreport as missing init
            if _is_s2s_embargo_error(exc):
                _print_embargo_step_back(day, exc)
                embargo_step_backs += 1
                continue
            raise DataError(
                f"ECDS submit failed while probing init {day.isoformat()} ({exc}); "
                "this is a transport/auth problem, not a not-yet-published init."
            ) from None

        waited = 0
        while True:
            try:
                if remote.results_ready:
                    return day, remote
            except ProcessingFailedError as exc:
                # ECDS marked the job failed/rejected/dismissed. Two non-fatal
                # cases both step back one day:
                #   - the S2S real-time embargo (access restriction), whose detail
                #     surfaces in the ProcessingFailedError message, and
                #   - a not-yet-published init.
                if _is_s2s_embargo_error(exc):
                    _print_embargo_step_back(day, exc)
                    embargo_step_backs += 1
                else:
                    print(
                        f"  {day.isoformat()} not published ({exc}); stepping back",
                        file=sys.stderr,
                    )
                break
            except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't step back
                # The S2S real-time embargo can also surface as a non-
                # ProcessingFailedError (e.g. a MARS access error / MarsRuntimeError
                # whose text or exception chain carries "Restricted access to
                # S2S"). That init is access-restricted, not a real
                # transport/auth problem, so step back like the not-published case
                # rather than exiting. Genuine transport/auth/HTTP errors still exit.
                if _is_s2s_embargo_error(exc):
                    _print_embargo_step_back(day, exc)
                    embargo_step_backs += 1
                    break
                raise DataError(
                    f"polling ECDS job for init {day.isoformat()} failed ({exc}); "
                    "this is a transport/auth problem, not a not-yet-published init."
                ) from None
            if waited >= _PROBE_POLL_MAX_SECONDS:
                raise DataError(
                    f"ECDS job for init {day.isoformat()} was still not ready after "
                    f"{_PROBE_POLL_MAX_SECONDS}s; the job is stuck. Aborting rather than "
                    "stepping back to an older init (which would report a misleadingly old "
                    "'latest'). Re-run, or pass an explicit init date."
                )
            time.sleep(_PROBE_POLL_SECONDS)
            waited += _PROBE_POLL_SECONDS
    if embargo_step_backs == probed:
        # Every probed day failed with the embargo signature: the lookback never
        # reached an accessible init, which points at the account's S2S access
        # rather than at publication lag.
        raise UsageError(
            f"every probed init in the last {_LATEST_LOOKBACK_DAYS + 1} days was "
            "access-restricted (S2S real-time embargo); cannot resolve 'latest'. "
            f"Check your S2S access and licence terms ({S2S_LICENCE_URL})."
        )
    raise UsageError(
        f"no ECMWF S2S init available in the last {_LATEST_LOOKBACK_DAYS + 1} days "
        f"(probed back to {(dt.datetime.now(dt.UTC).date() - dt.timedelta(days=_LATEST_LOOKBACK_DAYS)).isoformat()}); "
        "cannot resolve 'latest'."
    )


def _latest_probe(area: list[float], state: dict) -> dt.date:
    """Discover the newest accessible init, memoized in the run state.

    `latest` discovery probes ECDS, which needs credentials and a Client.
    Build them lazily and memoize in ``state`` (``RunContext.state``, shared
    with the fetch body) so the probe runs at most once per run and only when
    a `latest` token is present. An absolute or now-based --date performs no
    ECDS call and no import here.
    """
    if "v" not in state:
        require_env("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")
        from ecmwf.datastores import Client

        state["client"] = Client()
        # _discover_latest returns (day, completed control retrieval). Cache
        # the remote so the fetch body reuses the probe's control leg for the
        # winning init instead of re-submitting it.
        #
        # The probe only confirms whether an init exists; the longitude band
        # does not change which init dates are published. For a wrapped
        # (west > east) bbox, probe with a single MARS-valid sub-area (the
        # western band) so the existence probe submits a legal request; the
        # full wrapped band is fetched as two split legs in the body and the
        # probe retrieval is not reused as a leg in that case.
        probe_area = _split_wrapped_area(area)[0]
        day, cf_remote = _discover_latest(state["client"], probe_area)
        state["v"] = day
        state["cf_remote"] = cf_remote
    return state["v"]


def _latest(args, context) -> dt.date:
    """`latest` resolver for the standard --date toggle.

    Parses the required bbox into a MARS area and runs the ECDS
    probe-submit-poll discovery for the newest accessible S2S init, memoized in
    the run state (shared with the fetch body so the winning control retrieval
    is reused rather than re-submitted). The decorator invokes this lazily, only
    when the --date token references `latest`, and after the bbox has already
    been parsed and validated (a malformed bbox exits 2 before any probe).
    """
    area = list(parse_bbox(args.bbox))
    return _latest_probe(area, context.state)


@weather_skill(
    "ecmwf-fetch",
    _SKILL_VERSION,
    output_type="forecast",
    source="ecmwf-s2s",
    bbox="required",
    date={
        "required": True,
        "context": "single forecast init date",
        "help": (
            "Forecast init date. Either YYYY-MM-DD, 'now'/'today', 'latest', or "
            "an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "'latest' probes init dates backward via ECDS submits (slow)."
        ),
    },
    latest_resolver=_latest,
    cache_hit_label="fetch",
)
def fetch(bbox, date, context):
    """Fetch ECMWF S2S precipitation (cf + pf) and write a weather-skills envelope Zarr."""
    resolved_date = date
    date_iso = resolved_date.isoformat()
    area = list(bbox)
    state = context.state

    require_env("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")

    import xarray as xr
    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    print(f"Fetching ECMWF S2S for area={area} date={date_iso}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        # MARS `area` requires west < east. A wrapped (west > east) bbox from
        # resolve-region (Russia, Fiji) is split at +-180 into a western band
        # [N, W, S, 180] and an eastern band [N, -180, S, E]; each forecast type
        # (cf, pf) is then retrieved once per sub-area and the per-area decoded
        # datasets are concatenated along longitude into one envelope. A normal
        # (west <= east) bbox yields a single sub-area, so this is one retrieval
        # per forecast type as before.
        sub_areas = _split_wrapped_area(area)
        wrapped = len(sub_areas) > 1

        # Reuse the probe's Client when `latest` discovery already built one,
        # else create it now.
        client = state.get("client") or Client()

        # A "leg" is one (forecast_type, sub-area) retrieval. Each leg gets its
        # own remote, grib path, and (after decode) dataset. The probe's control
        # retrieval is reused only for a non-wrapped fetch of THIS exact init:
        # in the wrapped case the probe submitted a single sub-area, not the
        # full split, so it cannot stand in for a leg.
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

        reuse_cf = (
            (not wrapped) and resolved_date == state.get("v") and state.get("cf_remote") is not None
        )

        # _submit handles the licence-not-accepted case (exits). Any other submit
        # failure (transport/auth/HTTP) is surfaced here and the run exits
        # non-zero, mirroring the probe's taxonomy so a bad init or a credential
        # problem yields a clear message rather than a raw traceback.
        if reuse_cf:
            print(
                "Reusing probe's control retrieval; submitting remaining legs...", file=sys.stderr
            )
        else:
            print(f"Submitting {len(legs)} retrieval leg(s)...", file=sys.stderr)
        try:
            for leg in legs:
                if (
                    reuse_cf
                    and leg["forecast_type"] == "control_forecast"
                    and leg["remote"] is None
                ):
                    leg["remote"] = state.get("cf_remote")
                    continue
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
                        f"(access-restricted) ({exc}); use --date latest or an older "
                        "init date."
                    ) from None
                raise DataError(
                    f"ECDS reported no data for init {date_iso} ({exc}); "
                    "it may not be a valid S2S init day. ECMWF S2S runs init on "
                    "fixed days, so 'now'/offset dates rarely align — use 'latest' "
                    "or pass a known S2S init date."
                ) from None
            except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't step
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the S2S real-time embargo "
                        f"(access-restricted) ({exc}); use --date latest or an older "
                        "init date."
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
        ds.attrs.update(Conventions="CF-1.13")
        stamp_cf_attrs(ds)
        # Stamp explicit units on tp so downstream consumers don't have to reverse-engineer
        # them from value ranges. GRIB carries `kg m-2` (numerically equivalent to mm depth
        # over the accumulation period); we forward that quantity rather than convert.
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"

        # The decoded dataset is lazily backed by the GRIB files in the
        # temporary directory, which is removed when this block exits; the
        # decorator writes the returned dataset after that, so materialize the
        # values while the files are still alive.
        ds = ds.load()

    return ds, WroteSummary("", replace=True)


if __name__ == "__main__":
    fetch()
