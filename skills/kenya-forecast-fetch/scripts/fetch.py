# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "xarray",
#   "zarr>=3",
#   "fsspec",
#   "aiohttp",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a Kenya forecasts archive Zarr grid and write a weather-skills standard dataset."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset
from weather_skills_core.units import to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BUCKET = "kenya-forecasting-data"
_GCS_API = f"https://storage.googleapis.com/storage/v1/b/{_BUCKET}/o"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"
_HTTP_TIMEOUT = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_DATASET = "precip"

# Short ids → object key under <date>/data/ (may include date in the basename).
_DATASETS: dict[str, str] = {
    "precip": "ECMWF_s2s_precip_{date}.zarr",
    "daily_vars": "ECMWF_s2s_daily_vars_{date}.zarr",
    "Tminmax": "ECMWF_s2s_Tminmax_{date}.zarr",
    "10wind": "ECMWF_s2s_10wind_{date}.zarr",
    "500wind": "ECMWF_s2s_500wind_{date}.zarr",
    "700wind": "ECMWF_s2s_700wind_{date}.zarr",
    "medium_range_precip": "medium_range_precip.zarr",
    "gefs": "gefs/gefs_kenya.zarr",
}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise DataError(
            f"GCS listing failed for {url!r}: HTTP {exc.code} {exc.reason}"
        ) from None
    except urllib.error.URLError as exc:
        raise DataError(f"GCS listing failed for {url!r}: {exc.reason}") from None


def _list_init_dates() -> list[str]:
    dates: list[str] = []
    token = None
    while True:
        params = {"delimiter": "/", "maxResults": "1000"}
        if token:
            params["pageToken"] = token
        url = f"{_GCS_API}?{urllib.parse.urlencode(params)}"
        payload = _get_json(url)
        for prefix in payload.get("prefixes", []):
            name = prefix.strip("/")
            if _DATE_RE.fullmatch(name):
                dates.append(name)
        token = payload.get("nextPageToken")
        if not token:
            break
    dates.sort()
    return dates


def _store_key(dataset: str, iso: str) -> str:
    template = _DATASETS[dataset]
    return f"{iso}/data/{template.format(date=iso)}"


def _store_exists(key: str) -> bool:
    """True if the Zarr root metadata object is present."""
    meta_key = f"{key.rstrip('/')}/zarr.json"
    url = f"{_GCS_MEDIA}/{urllib.parse.quote(meta_key, safe='/')}"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise DataError(
            f"GCS HEAD failed for {meta_key!r}: HTTP {exc.code} {exc.reason}"
        ) from None
    except urllib.error.URLError as exc:
        raise DataError(f"GCS HEAD failed for {meta_key!r}: {exc.reason}") from None


def _resolve_date(date, dataset: str) -> str:
    if date is not None:
        iso = date.isoformat()
        key = _store_key(dataset, iso)
        if not _store_exists(key):
            raise DataError(
                f"no {dataset!r} Zarr under init date {iso} in gs://{_BUCKET}/ "
                f"(expected gs://{_BUCKET}/{key}). Older folders may only have "
                "GRIB/NetCDF under data/ — pick a more recent init, or use "
                "ecmwf-fetch / dynamical-fetch for live grids. Browse "
                "https://kenya-forecasts.sheerwater.rhizaresearch.org/files/ "
                "or omit --date to take the latest available."
            )
        return iso

    dates = _list_init_dates()
    if not dates:
        raise DataError(
            f"no YYYY-MM-DD folders found in gs://{_BUCKET}/; the Kenya forecasts "
            "archive may be empty or unreachable."
        )
    for iso in reversed(dates):
        if _store_exists(_store_key(dataset, iso)):
            return iso
    raise DataError(
        f"no {dataset!r} Zarr found under any init-date data/ folder in "
        f"gs://{_BUCKET}/. Available --dataset ids: {', '.join(_DATASETS)}."
    )


def _open_remote(key: str):
    import xarray as xr

    url = f"{_GCS_MEDIA}/{key}"
    try:
        return xr.open_zarr(url, consolidated=True)
    except Exception as exc:  # noqa: BLE001 — surface remote open failures cleanly
        raise DataError(
            f"failed to open remote Zarr gs://{_BUCKET}/{key} ({exc})."
        ) from None


@weather_skill(
    name="kenya-forecast-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default=_DEFAULT_DATASET,
    choices=list(_DATASETS),
    help=(
        "Archive data product under <date>/data/ "
        f"(default {_DEFAULT_DATASET})."
    ),
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
def fetch(dataset, date, bbox, variable, output, **kwargs):
    """Fetch a Kenya forecasts archive Zarr and write a weather-skills standard dataset.

    Opens a consolidated Zarr under ``gs://kenya-forecasting-data/<date>/data/``
    over HTTPS (credential-free), optionally subsets by ``--bbox`` and
    ``--variable``, normalizes units/CF attrs, and returns a Dataset for the
    decorator to write. Compose with ``plot``, ``plot-timeseries``, ``reduce``,
    etc. for flexible figures — this skill does not render PNGs.
    """
    if dataset not in _DATASETS:
        raise UsageError(
            f"unknown --dataset {dataset!r}; choose one of: {', '.join(_DATASETS)}"
        )

    iso = _resolve_date(date, dataset)
    key = _store_key(dataset, iso)
    print(f"Resolved init date: {iso}", file=sys.stderr)
    print(f"Opening gs://{_BUCKET}/{key}", file=sys.stderr)

    ds = _open_remote(key)

    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[variable]

    if bbox is not None:
        ds = bbox_subset(ds, bbox)

    # Materialize while the remote store is open so to_zarr does not re-fetch.
    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"kenya-forecasting-data:{key}",
    )
    stamp_cf_attrs(ds)
    return to_standard_units(ds)


if __name__ == "__main__":
    fetch()
