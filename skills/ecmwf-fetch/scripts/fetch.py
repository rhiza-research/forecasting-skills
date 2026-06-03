# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "cfgrib",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch ECMWF S2S precipitation (cf + pf) and write a Rhiza Envelope Zarr."""

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
_RHIZA_SKILL_VERSION = "0.1.1"

# How far back from today the `latest` init probe looks. ECMWF S2S runs init on
# fixed days; 14 days covers several init cycles plus production lag. Exhausting
# the probe without a usable init exits non-zero.
_LATEST_LOOKBACK_DAYS = 14

# Bounded poll for a single `latest` probe day. ECDS retrievals run minutes to
# ~hour, so each probe job is polled every _PROBE_POLL_SECONDS up to a
# _PROBE_POLL_MAX_SECONDS wall-clock cap. A job still not results-ready at the
# cap is treated as stuck (not as "this init is unavailable"): the probe aborts
# with a clear message rather than looping forever or silently stepping back to
# an older init, since stepping back on a stuck-but-possibly-valid job would
# report a misleadingly old `latest`. See SKILL.md.
_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

# --- Relative-date value grammar (duplicated per CONVENTIONS.md; no shared module) ---
#
# A --date value is one of:
#   YYYY-MM-DD                  absolute date
#   now | today                 current UTC date
#   latest                      newest available forecast init (discovered)
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
      ("base", "latest")                         newest available init (resolved later)
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


REGIONS = {
    "africa": [23.0, -20.0, -37.0, 59.0],
    "kenya": [7.0, 32.0, -6.0, 43.0],
    "ghana": [12.0, -4.0, 4.0, 2.0],
    "senegal": [17.0, -17.5, 12.0, -11.0],
    "ethiopia": [16.0, 32.0, 2.0, 49.0],
    "namibia": [-15.0, 10.0, -31.0, 27.0],
    "botswana": [-15.0, 18.0, -28.0, 31.0],
    "zambia": [-6.0, 20.0, -20.0, 35.0],
    "madagascar": [-10.0, 42.0, -27.0, 52.0],
    "angola": [-5.0, 12.0, -18.0, 24.0],
}

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
            raw = ds.attrs.get("rhiza_history")
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
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
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


def _discover_latest(client, area: list[float]) -> tuple[dt.date, object]:
    """`latest` resolver for ECMWF S2S: newest available forecast init on or
    before today (UTC), found by probing init dates backward.

    For each candidate day back from today, submits a control-forecast retrieval
    over the requested area and polls it (bounded by ``_PROBE_POLL_MAX_SECONDS``)
    until results-ready. A job that ECDS marks failed/rejected/dismissed means
    that init is not published — step back one day. A submit/transport/auth
    error, or a job still not ready at the poll cap, is NOT "this init is
    unavailable": it is surfaced and the run exits non-zero, so a stuck job or a
    credential problem is never silently misreported as a missing init.

    Returns ``(day, remote)`` for the winning init — the completed control
    retrieval — so the caller reuses it as the control leg rather than
    re-submitting it. This is the slow/async case (each probe is a real ECDS
    submit) — acceptable because it is opt-in. Bounded by
    ``_LATEST_LOOKBACK_DAYS``; exhausting it without a usable init exits 2.
    """
    from ecmwf.datastores.processing import ProcessingFailedError

    today = dt.datetime.now(dt.UTC).date()
    for offset in range(_LATEST_LOOKBACK_DAYS + 1):
        day = today - dt.timedelta(days=offset)
        req = _build_request(day.isoformat(), area, "control_forecast")
        print(f"Probing ECMWF init {day.isoformat()} for 'latest'...", file=sys.stderr)
        # _submit handles the licence-not-accepted case (exits). Any other
        # submit failure (transport/auth/HTTP) is a real error, not a missing
        # init: surface it and exit rather than stepping back.
        try:
            remote = _submit(client, req)
        except Exception as exc:  # noqa: BLE001 -- surface; do not misreport as missing init
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
                # ECDS marked the job failed/rejected/dismissed: this init is not
                # published. Step back one day.
                print(f"  {day.isoformat()} not published ({exc}); stepping back", file=sys.stderr)
                break
            except Exception as exc:  # noqa: BLE001 -- surface transport/auth, don't step back
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
    print(
        f"Error: no ECMWF S2S init available in the last {_LATEST_LOOKBACK_DAYS} days "
        f"(probed back to {(today - dt.timedelta(days=_LATEST_LOOKBACK_DAYS)).isoformat()}); "
        "cannot resolve 'latest'.",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date",
        required=True,
        help=(
            "Forecast init date. Either YYYY-MM-DD, 'now'/'today', 'latest', or "
            "an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "'latest' probes init dates backward via ECDS submits (slow)."
        ),
    )
    p.add_argument("--region", choices=sorted(REGIONS))
    p.add_argument("--bbox", help="N/W/S/E bbox overriding --region")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    if not args.bbox and not args.region:
        print("Error: one of --region or --bbox is required.", file=sys.stderr)
        sys.exit(2)
    if args.bbox:
        area = [float(x) for x in args.bbox.split("/")]
    else:
        area = REGIONS[args.region]

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
            day, cf_remote = _discover_latest(_client_cache["client"], area)
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
        "version": _RHIZA_SKILL_VERSION,
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

    print(f"Fetching ECMWF S2S for area={area} date={date_iso}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        cf_grib = tmp / "cf.grib"
        pf_grib = tmp / "pf.grib"

        # Reuse the probe's Client when `latest` discovery already built one,
        # else create it now.
        client = _client_cache.get("client") or Client()
        pf_req = _build_request(date_iso, area, "perturbed_forecast")

        # If `latest` discovery already submitted and completed a control-forecast
        # retrieval for THIS exact init (i.e. the resolved date is the probed
        # `latest` day, not an offset off it), reuse that completed retrieval as
        # the control leg instead of re-submitting it. Otherwise submit cf now.
        # The perturbed leg is always submitted here. ECDS retrievals are
        # typically minutes to ~hour.
        cf_remote = None
        if resolved_date == _client_cache.get("v"):
            cf_remote = _client_cache.get("cf_remote")
        if cf_remote is not None:
            print("Reusing probe's control retrieval; submitting pf...", file=sys.stderr)
            pf_remote = _submit(client, pf_req)
        else:
            print("Submitting cf and pf retrievals...", file=sys.stderr)
            cf_req = _build_request(date_iso, area, "control_forecast")
            cf_remote = _submit(client, cf_req)
            pf_remote = _submit(client, pf_req)
        remotes = [cf_remote, pf_remote]
        while not all(r.results_ready for r in remotes):
            time.sleep(30)

        print("Downloading cf...", file=sys.stderr)
        cf_remote.download(str(cf_grib))
        print("Downloading pf...", file=sys.stderr)
        pf_remote.download(str(pf_grib))

        print("Decoding GRIB and writing Zarr...", file=sys.stderr)
        cf = xr.open_dataset(cf_grib, engine="cfgrib").assign_coords(number=0)
        pf = xr.open_dataset(pf_grib, engine="cfgrib")
        ds = xr.concat([pf, cf], dim="number").sortby("number")
        ds.attrs.update(
            rhiza_source="ecmwf-s2s",
            rhiza_history=json.dumps([entry], sort_keys=True),
        )
        _stamp_cf_attrs(ds)
        # Stamp explicit units on tp so downstream consumers don't have to reverse-engineer
        # them from value ranges. GRIB carries `kg m**-2` (numerically equivalent to mm depth
        # over the accumulation period); we forward that exact string rather than convert.
        ds["tp"].attrs["units"] = "kg m**-2"
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
