# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Fetch a pre-rendered Kenya forecasts archive PNG from the public GCS bucket."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Public GCS bucket behind https://kenya-forecasts.sheerwater.rhizaresearch.org/files/
_BUCKET = "kenya-forecasting-data"
_GCS_API = f"https://storage.googleapis.com/storage/v1/b/{_BUCKET}/o"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"
_HTTP_TIMEOUT = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIODS = ("weekly", "dekadal", "monthly")
_DEFAULT_PERIOD = "weekly"
_DEFAULT_PRODUCT = "weekly_precip.png"


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
    """Return sorted YYYY-MM-DD folder names at the bucket root."""
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


def _normalize_product(product: str) -> str:
    """Reject path escape and leading slashes; keep nested paths like ``t2m/t2m.png``."""
    cleaned = product.strip().lstrip("/")
    parts = Path(cleaned).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise UsageError(
            f"invalid --product {product!r}; pass a relative path under the period "
            f"folder (e.g. {_DEFAULT_PRODUCT!r} or 't2m/t2m.png'), without '..'."
        )
    if not cleaned.lower().endswith(".png"):
        raise UsageError(f"--product must be a .png path; got {product!r}.")
    return "/".join(parts)


def _object_exists(key: str) -> bool:
    url = f"{_GCS_MEDIA}/{urllib.parse.quote(key, safe='/')}"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise DataError(f"GCS HEAD failed for {key!r}: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise DataError(f"GCS HEAD failed for {key!r}: {exc.reason}") from None


def _resolve_date(date, period: str, product: str) -> str:
    """Pick an init-date folder that contains ``<period>/<product>``."""
    if date is not None:
        iso = date.isoformat()
        key = f"{iso}/{period}/{product}"
        if not _object_exists(key):
            raise DataError(
                f"no {period}/{product!r} under init date {iso} in gs://{_BUCKET}/ "
                f"(expected object {key!r}). Browse "
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
        if _object_exists(f"{iso}/{period}/{product}"):
            return iso
    raise DataError(f"no {period}/{product!r} found under any init-date folder in gs://{_BUCKET}/.")


def _download(key: str, dest: Path) -> None:
    url = f"{_GCS_MEDIA}/{urllib.parse.quote(key, safe='/')}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise DataError(f"download failed for {key!r}: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise DataError(f"download failed for {key!r}: {exc.reason}") from None

    if not data:
        raise DataError(f"download of {key!r} returned an empty body.")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise DataError(
            f"downloaded {key!r} is not a PNG (content-type={content_type!r}, size={len(data)})."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


@weather_skill(
    name="kenya-forecast-png",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--period",
    default=_DEFAULT_PERIOD,
    choices=list(_PERIODS),
    help=f"Archive period folder under <date>/ (default {_DEFAULT_PERIOD}).",
)
@weather_skill.argument(
    "--product",
    default=_DEFAULT_PRODUCT,
    help=(
        "PNG path relative to <date>/<period>/ "
        f"(default {_DEFAULT_PRODUCT}; nested ok e.g. t2m/t2m.png)."
    ),
)
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
def fetch(date, period, product, output, **kwargs):
    """Fetch a pre-rendered Kenya forecasts archive PNG from the public store.

    Lists init-date folders in the public ``kenya-forecasting-data`` GCS bucket
    (the store behind
    https://kenya-forecasts.sheerwater.rhizaresearch.org/files/), picks the most
    recent folder that contains ``<period>/<product>`` (or the folder for
    ``--date``), and writes that PNG to ``--output``. Credential-free.
    """
    if period not in _PERIODS:
        raise UsageError(f"unknown --period {period!r}; choose one of: {', '.join(_PERIODS)}")
    product = _normalize_product(product)
    if kwargs.get("probe_latest") is not None:
        print(_resolve_date(None, period, product))
        return
    output = Path(output)
    iso = _resolve_date(date, period, product)
    key = f"{iso}/{period}/{product}"
    print(f"Resolved init date: {iso}", file=sys.stderr)
    print(f"Fetching gs://{_BUCKET}/{key}", file=sys.stderr)
    _download(key, output)
    # Decorator stamps weather_skills_history (+ official mark) on the returned Path.
    return output


if __name__ == "__main__":
    fetch()
