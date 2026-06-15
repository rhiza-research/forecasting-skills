# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
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

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.9"

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

# --- Relative-date value grammar (duplicated per CONVENTIONS.md; no shared module) ---
#
# A --date value is one of:
#   YYYY-MM-DD                  absolute date
#   now | today                 current UTC date
#   latest                      newest accessible forecast init (discovered)
#   now-<int>{d|w}              now minus N days   (w = 7 days)
#   latest-<int>{d|w}           latest minus N days
# Anything else (months/years, future "+", junk) is rejected pre-network.
_REL_OFFSET_RE = re.compile(r"^(?P<base>now|latest)-(?P<n>\d+)(?P<unit>[dw])$")

# Strict absolute-date shape. dt.date.fromisoformat on 3.12 also accepts compact
# (20260501) and ISO-week (2026-W18-1) forms; the documented grammar is exactly
# YYYY-MM-DD, so we gate on this regex first and reject the looser forms.
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Upper bound on a relative offset's resolved day count. 36525 days (~100 years)
# is far beyond any real value yet small enough that the date arithmetic cannot
# raise OverflowError. Rejecting above this cap keeps the failure pre-network.
_MAX_OFFSET_DAYS = 36525


def _parse_token(value: str) -> tuple:
    """Parse a --date value into a structured token.

    Returns one of:
      ("abs", date)                              absolute YYYY-MM-DD
      ("base", "now")                            current UTC date
      ("base", "latest")                         newest accessible init (resolved later)
      ("offset", "now", n_days, unit_phrase)     now minus n_days
      ("offset", "latest", n_days, unit_phrase)  latest minus n_days

    `unit_phrase` describes the offset in its requested units for the log line
    (e.g. "3-week", "7-day"). Raises ValueError for anything else (months/years,
    future "+", malformed), so the failure happens before any network call.
    "today" is accepted as an alias for "now".
    """
    if value in ("now", "today"):
        return ("base", "now")
    if value == "latest":
        return ("base", "latest")
    m = _REL_OFFSET_RE.match(value)
    if m is not None:
        n = int(m.group("n"))
        if n < 1:
            raise ValueError(
                f"invalid date value {value!r}: offset must be >= 1 (e.g. now-1d, latest-3w)"
            )
        unit = m.group("unit")
        n_days = n * 7 if unit == "w" else n
        if n_days > _MAX_OFFSET_DAYS:
            raise ValueError(
                f"invalid date value {value!r}: offset resolves to {n_days} days, "
                f"above the maximum of {_MAX_OFFSET_DAYS} days (~100 years)"
            )
        unit_phrase = f"{n}-{'week' if unit == 'w' else 'day'}"
        return ("offset", m.group("base"), n_days, unit_phrase)
    if _ABS_DATE_RE.match(value):
        try:
            return ("abs", dt.date.fromisoformat(value))
        except ValueError:
            pass
    raise ValueError(
        f"invalid date value {value!r}: expected an absolute date YYYY-MM-DD, "
        "'now'/'today', 'latest', or an offset 'now-<int>{d|w}' / "
        "'latest-<int>{d|w}'"
    )


def _resolve_date(value: str, latest_fn) -> tuple:
    """Resolve a single --date value to a concrete date.

    Applies the value grammar; both ends being inclusive is moot for a single
    date. Returns (resolved_date, log_line) where log_line is a stderr message
    to print before fetching when a relative token is used, else None. Exits 2
    (pre-network) on a malformed token. `latest_fn` is called only when the
    token references `latest`, and at most once (the caller memoizes).
    """
    try:
        tok = _parse_token(value)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    if tok[0] == "abs":
        return tok[1], None

    now = dt.datetime.now(dt.UTC).date()
    base = tok[1]
    base_date = now if base == "now" else latest_fn()
    resolved = base_date if tok[0] == "base" else base_date - dt.timedelta(days=tok[2])
    log_line = f'resolved "{value}" -> {resolved.isoformat()} (single forecast init date)'
    return resolved, log_line


LEADTIME_HOURS = ["0", "168", "240", "336", "480", "504", "672", "720", "840", "960", "1008"]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"


def _require_env() -> None:
    missing = [v for v in ("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY") if not os.environ.get(v)]
    if missing:
        print(f"Error: missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


def _cache_hit(out: Path, entry: dict) -> bool:
    """Return True if the zarr at `out` was produced by this same entry."""
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    existing_entry = history[0]
    return (
        existing_entry.get("skill") == entry["skill"]
        and existing_entry.get("version") == entry["version"]
        and existing_entry.get("args") == entry["args"]
        and existing_entry.get("input") == entry["input"]
    )


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
    for name in ("latitude", "lat", "y"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "latitude")
            ds[name].attrs.setdefault("units", "degrees_north")
            ds[name].attrs.setdefault("axis", "Y")
            break
    for name in ("longitude", "lon", "x"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "longitude")
            ds[name].attrs.setdefault("units", "degrees_east")
            ds[name].attrs.setdefault("axis", "X")
            break
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


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
            print(
                "ERROR: ECDS retrieval blocked: required licences not accepted on s2s-forecasts.\n"
                f"Action: open {S2S_LICENCE_URL} in a browser, log in to ECDS, "
                "accept the required licences, then re-run this skill.",
                file=sys.stderr,
            )
            sys.exit(1)
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
    --date IS that probed init (bare `latest`), main() reuses this completed
    retrieval as the control leg. When the resolved --date is an OFFSET off
    latest (`latest-Nd|w`), the probed init differs from the init main() fetches,
    so this completed control retrieval cannot be reused and is spent solely to
    confirm `latest` existed — one unavoidable probe retrieval. The offset init
    is then submitted exactly once in main(); the probed `latest` init is never
    re-submitted (it is only ever reused, never resubmitted), so no init is ever
    submitted twice.
    """
    from ecmwf.datastores.processing import ProcessingFailedError

    today = dt.datetime.now(dt.UTC).date()
    started = time.monotonic()
    probed = 0
    embargo_step_backs = 0
    for offset in range(_LATEST_LOOKBACK_DAYS + 1):
        if time.monotonic() - started > _DISCOVERY_MAX_SECONDS:
            print(
                f"Error: latest discovery exceeded its time budget "
                f"({_DISCOVERY_MAX_SECONDS:.0f}s) after probing {probed} init(s); "
                "aborting. Re-run, or pass an explicit init date.",
                file=sys.stderr,
            )
            sys.exit(1)
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
        except Exception as exc:  # noqa: BLE001 -- classify; do not misreport as missing init
            if _is_s2s_embargo_error(exc):
                _print_embargo_step_back(day, exc)
                embargo_step_backs += 1
                continue
            print(
                f"Error: ECDS submit failed while probing init {day.isoformat()} ({exc}); "
                "this is a transport/auth problem, not a not-yet-published init.",
                file=sys.stderr,
            )
            sys.exit(1)

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
                print(
                    f"Error: polling ECDS job for init {day.isoformat()} failed ({exc}); "
                    "this is a transport/auth problem, not a not-yet-published init.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if waited >= _PROBE_POLL_MAX_SECONDS:
                print(
                    f"Error: ECDS job for init {day.isoformat()} was still not ready after "
                    f"{_PROBE_POLL_MAX_SECONDS}s; the job is stuck. Aborting rather than "
                    "stepping back to an older init (which would report a misleadingly old "
                    "'latest'). Re-run, or pass an explicit init date.",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(_PROBE_POLL_SECONDS)
            waited += _PROBE_POLL_SECONDS
    if embargo_step_backs == probed:
        # Every probed day failed with the embargo signature: the lookback never
        # reached an accessible init, which points at the account's S2S access
        # rather than at publication lag.
        print(
            f"Error: every probed init in the last {_LATEST_LOOKBACK_DAYS + 1} days was "
            "access-restricted (S2S real-time embargo); cannot resolve 'latest'. "
            f"Check your S2S access and licence terms ({S2S_LICENCE_URL}).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"Error: no ECMWF S2S init available in the last {_LATEST_LOOKBACK_DAYS + 1} days "
        f"(probed back to {(today - dt.timedelta(days=_LATEST_LOOKBACK_DAYS)).isoformat()}); "
        "cannot resolve 'latest'.",
        file=sys.stderr,
    )
    sys.exit(2)


def _attach_bbox_value(argv):
    # argparse rejects a space-separated --bbox value that starts with '-'
    # (a bbox whose North latitude is negative). Rewrite `--bbox VAL` to
    # `--bbox=VAL` so both the space and equals forms parse.
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--bbox" and i + 1 < len(argv):
            out.append(f"--bbox={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument(
        "--date",
        required=True,
        help=(
            "Forecast init date. Either YYYY-MM-DD, 'now'/'today', 'latest', or "
            "an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "'latest' probes init dates backward via ECDS submits (slow)."
        ),
    )
    p.add_argument(
        "--bbox",
        required=True,
        help="N/W/S/E decimal degrees (use the resolve-region skill to get a country's bbox)",
    )
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args(_attach_bbox_value(sys.argv[1:]))

    try:
        area = [float(x) for x in args.bbox.split("/")]
    except ValueError:
        print("Error: --bbox must be N/W/S/E (decimal degrees).", file=sys.stderr)
        sys.exit(2)

    # `latest` discovery probes ECDS, which needs credentials and a Client.
    # Build them lazily and memoize so the probe runs at most once and only when
    # a `latest` token is present. An absolute or now-based --date performs no
    # ECDS call and no import here (absolute behavior is unchanged: env, imports,
    # and the cache check all run in the same order as before).
    _client_cache = {}

    def _latest() -> dt.date:
        if "v" not in _client_cache:
            _require_env()
            from ecmwf.datastores import Client

            _client_cache["client"] = Client()
            # _discover_latest returns (day, completed control retrieval). Cache
            # the remote so main can reuse the probe's control leg for the
            # winning init instead of re-submitting it.
            #
            # The probe only confirms whether an init exists; the longitude band
            # does not change which init dates are published. For a wrapped
            # (west > east) bbox, probe with a single MARS-valid sub-area (the
            # western band) so the existence probe submits a legal request; the
            # full wrapped band is fetched as two split legs below and the probe
            # retrieval is not reused as a leg in that case.
            probe_area = _split_wrapped_area(area)[0]
            day, cf_remote = _discover_latest(_client_cache["client"], probe_area)
            _client_cache["v"] = day
            _client_cache["cf_remote"] = cf_remote
        return _client_cache["v"]

    # Resolve --date to a concrete init date. A malformed token exits 2 before
    # any network call. An absolute YYYY-MM-DD normalizes through
    # dt.date.fromisoformat, so the resolved isoformat is byte-identical to the
    # raw input.
    resolved_date, log_line = _resolve_date(args.date, _latest)
    date_iso = resolved_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    # Build the cache-key args from the argparse namespace minus the path
    # strings, then set the resolved concrete init date by explicit assignment
    # (never the relative token) — matching how imerg/tahmo build their dicts,
    # rather than a vars(args) | {"date": ...} merge-override.
    args_dict = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    args_dict["date"] = date_iso
    entry = {
        "skill": "ecmwf-fetch",
        "version": _SKILL_VERSION,
        "args": args_dict,
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    _require_env()

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
        client = _client_cache.get("client") or Client()

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
            (not wrapped)
            and resolved_date == _client_cache.get("v")
            and _client_cache.get("cf_remote") is not None
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
                    leg["remote"] = _client_cache.get("cf_remote")
                    continue
                req = _build_request(date_iso, leg["area"], leg["forecast_type"])
                leg["remote"] = _submit(client, req)
        except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't traceback
            print(
                f"Error: ECDS submit failed for init {date_iso} ({exc}); "
                "this is a transport/auth problem.",
                file=sys.stderr,
            )
            sys.exit(1)

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
                    print(
                        f"Error: init {date_iso} is inside the S2S real-time embargo "
                        f"(access-restricted) ({exc}); use --date latest or an older "
                        "init date.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print(
                    f"Error: ECDS reported no data for init {date_iso} ({exc}); "
                    "it may not be a valid S2S init day. ECMWF S2S runs init on "
                    "fixed days, so 'now'/offset dates rarely align — use 'latest' "
                    "or pass a known S2S init date.",
                    file=sys.stderr,
                )
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't step
                if _is_s2s_embargo_error(exc):
                    print(
                        f"Error: init {date_iso} is inside the S2S real-time embargo "
                        f"(access-restricted) ({exc}); use --date latest or an older "
                        "init date.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print(
                    f"Error: polling ECDS job for init {date_iso} failed ({exc}); "
                    "this is a transport/auth problem.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if waited >= _PROBE_POLL_MAX_SECONDS:
                print(
                    f"Error: ECDS job for init {date_iso} was still not ready after "
                    f"{_PROBE_POLL_MAX_SECONDS}s; the job is stuck. Re-run, or pass a "
                    "different init date.",
                    file=sys.stderr,
                )
                sys.exit(1)
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
        ds.attrs.update(
            weather_skills_source="ecmwf-s2s",
            weather_skills_history=json.dumps([entry], sort_keys=True),
            Conventions="CF-1.13",
        )
        _stamp_cf_attrs(ds)
        # Stamp explicit units on tp so downstream consumers don't have to reverse-engineer
        # them from value ranges. GRIB carries `kg m-2` (numerically equivalent to mm depth
        # over the accumulation period); we forward that quantity rather than convert.
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
        for v in ds.variables:
            ds[v].encoding = {}

        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
