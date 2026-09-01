# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "fsspec",
#   "aiohttp",
#   "numpy",
#   "pint-xarray>=0.6",
#   "netcdf4",
# ]
# ///
"""Fetch a Kenya forecasts archive grid and write a weather-skills standard dataset."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    stamp_precip_amounts,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_BUCKET = "kenya-forecasting-data"
_GCS_API = f"https://storage.googleapis.com/storage/v1/b/{_BUCKET}/o"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"
_HTTP_TIMEOUT = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_DATASET = "precip"

# Per-step amounts already (not cumulative-since-init). Convert by dividing
# by the step interval; do not deaccumulate.
_ALREADY_PERIOD_PRECIP = frozenset({"gefs", "medium_range_precip", "precip_downscaled"})
# Interval fields whose archive ticks start at +1 native period (or +1 week).
_SHIFT_STEP_TO_ZERO = frozenset({"gefs", "daily_vars", "medium_range_precip", "precip_downscaled"})

# Short ids → object key under <date>/data/ (may include date in the basename).
_DATASETS: dict[str, str] = {
    "precip": "ECMWF_s2s_precip_{date}.zarr",
    "precip_downscaled": "data_weekly_Kenya_downscaled.nc",
    "daily_vars": "ECMWF_s2s_daily_vars_{date}.zarr",
    "Tminmax": "ECMWF_s2s_Tminmax_{date}.zarr",
    "10wind": "ECMWF_s2s_10wind_{date}.zarr",
    "500wind": "ECMWF_s2s_500wind_{date}.zarr",
    "700wind": "ECMWF_s2s_700wind_{date}.zarr",
    "medium_range_precip": "medium_range_precip.zarr",
    "gefs": "gefs/gefs_kenya.zarr",
}
_NETCDF_SUFFIXES = (".nc", ".nc4")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise DataError(f"GCS listing failed for {url!r}: HTTP {exc.code} {exc.reason}") from None
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
    """True if the Zarr root metadata object (or a NetCDF file) is present."""
    meta_key = key if key.lower().endswith(_NETCDF_SUFFIXES) else f"{key.rstrip('/')}/zarr.json"
    url = f"{_GCS_MEDIA}/{urllib.parse.quote(meta_key, safe='/')}"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise DataError(f"GCS HEAD failed for {meta_key!r}: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise DataError(f"GCS HEAD failed for {meta_key!r}: {exc.reason}") from None


def _resolve_date(date, dataset: str) -> str:
    if date is not None:
        iso = date.isoformat()
        key = _store_key(dataset, iso)
        if not _store_exists(key):
            raise DataError(
                f"no {dataset!r} store under init date {iso} in gs://{_BUCKET}/ "
                f"(expected gs://{_BUCKET}/{key}). Older folders may only have "
                "legacy GRIB/NetCDF under data/ — pick a more recent init, or "
                "use ecmwf-fetch / dynamical-fetch for live grids. Browse "
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
        f"no {dataset!r} store found under any init-date data/ folder in "
        f"gs://{_BUCKET}/. Available --dataset ids: {', '.join(_DATASETS)}."
    )


def _open_remote(key: str):
    import xarray as xr

    url = f"{_GCS_MEDIA}/{key}"
    try:
        if key.lower().endswith(_NETCDF_SUFFIXES):
            return _open_remote_netcdf(url)
        return xr.open_zarr(url, consolidated=True)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface remote open failures cleanly
        raise DataError(f"failed to open remote store gs://{_BUCKET}/{key} ({exc}).") from None


def _open_remote_netcdf(url: str):
    """Download a public NetCDF to a temp file and return an in-memory Dataset."""
    import tempfile

    import xarray as xr

    with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
        urllib.request.urlretrieve(url, tmp.name)
        with xr.open_dataset(tmp.name) as ds:
            return ds.load()


def _prepare_downscaled(ds):
    """CHIRPS-grid weekly downscale: lon-first NetCDF → (step, lat, lon) cube."""
    drop = [name for name in ("rank", "year", "surface") if name in ds.variables]
    if drop:
        ds = ds.drop_vars(drop)
    spatial = [d for d in ("step", "number", "latitude", "longitude") if d in ds.dims]
    others = [d for d in ds.dims if d not in spatial]
    if spatial:
        ds = ds.transpose(*others, *spatial)
    return ds


def _step_interval_stamp(ds) -> str:
    """Pint duration for the native step spacing (or the first lead if singleton)."""
    import numpy as np

    steps = np.asarray(ds["step"].values)
    if steps.size == 0:
        return "1 day"
    delta = steps[1] - steps[0] if steps.size >= 2 else steps[0]
    days = float(delta / np.timedelta64(1, "D"))
    ndays = max(1, int(round(days)))
    return f"{ndays} day"


def _shift_step_origin_to_zero(ds):
    """Relabel ``step`` so the first tick is lead 0 (period start)."""
    import numpy as np

    if "step" not in ds.dims or ds.sizes["step"] == 0:
        return ds
    steps = np.asarray(ds["step"].values)
    first = steps[0]
    zero = np.asarray(0).astype(steps.dtype)
    if first == zero:
        return ds
    new_step = steps - first
    attrs = dict(ds["step"].attrs)
    out = ds.assign_coords(step=("step", new_step))
    out["step"].attrs.update(attrs)
    if "valid_time" in out.variables:
        out = out.drop_vars("valid_time")
        if "time" in out.coords and getattr(out["time"], "ndim", 1) == 0:
            try:
                out = out.assign_coords(valid_time=("step", out["time"].values + new_step))
            except (TypeError, ValueError):
                pass
    return out


@weather_skill(
    name="kenya-forecast-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default=_DEFAULT_DATASET,
    choices=list(_DATASETS),
    help=(
        f"Archive product under <date>/data/ (default {_DEFAULT_DATASET}; "
        "precip_downscaled is the CHIRPS-resolution weekly precip)."
    ),
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
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
def fetch(dataset, date, bbox, variable, output, **kwargs):
    """Fetch a Kenya forecasts archive grid and write a weather-skills standard dataset.

    Opens a store under ``gs://kenya-forecasting-data/<date>/data/`` over HTTPS
    (credential-free): native S2S Zarr grids, or the CHIRPS-resolution weekly
    downscaled precip NetCDF. Optionally subsets by ``--bbox`` and ``--variable``,
    normalizes units/CF attrs, and returns a Dataset for the decorator to write.
    """
    if kwargs.get("probe_latest") is not None:
        dsid = kwargs["probe_latest"] or dataset
        if dsid not in _DATASETS:
            raise UsageError(f"unknown --dataset {dsid!r}; choose one of: {', '.join(_DATASETS)}")
        print(_resolve_date(None, dsid))
        return
    if dataset not in _DATASETS:
        raise UsageError(f"unknown --dataset {dataset!r}; choose one of: {', '.join(_DATASETS)}")

    iso = _resolve_date(date, dataset)
    key = _store_key(dataset, iso)
    print(f"Resolved init date: {iso}", file=sys.stderr)
    print(f"Opening gs://{_BUCKET}/{key}", file=sys.stderr)

    ds = _open_remote(key)
    if dataset == "precip_downscaled":
        ds = _prepare_downscaled(ds)

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
    else:
        ds = ensure_normalized_longitude(ds)

    # Materialize while the remote store is open so to_zarr does not re-fetch.
    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"kenya-forecasting-data:{key}",
    )
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    if dataset in _ALREADY_PERIOD_PRECIP:
        ds = precip_amounts_to_rates(ds, interval=_step_interval_stamp(ds), deaccumulate=False)
    else:
        ds = precip_amounts_to_rates(ds)
    if dataset in _SHIFT_STEP_TO_ZERO:
        ds = _shift_step_origin_to_zero(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()
