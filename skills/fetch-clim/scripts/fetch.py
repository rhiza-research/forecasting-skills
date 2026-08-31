# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   "fsspec",
#   "aiohttp",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a cached daily climatology from Sheerwater's public GCS mirror.

The mirror stores one static day-of-year climatology per (dataset, grid,
region, variable) — 1904 dates on the ``time`` dim (a leap year, so it always
covers day-of-year 1..366). This skill expands that static climatology onto
every calendar day in a requested ``--start-time``/``--end-time`` window,
repeating rows across years as needed, so timestamps line up with the rest of
a pipeline's data.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd
import xarray as xr
from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import (
    STANDARD,
    classify_variable,
    convert_dataarray,
    stamp_data_interval,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Public GCS mirror. Object keys: climatologies/<product>_<grid>_<region>_<variable>.zarr
_BUCKET = "sheerwater-public-datalake"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"
_HTTP_TIMEOUT = 60

# --dataset id -> product prefix in the object key.
_DATASETS: dict[str, str] = {
    "imerg": "imerg_final",
    "era5": "era5",
}

_GRIDS = ("global0_25", "global1_5")
_DEFAULT_VARIABLE = "precip"


def _object_key(product: str, grid: str, region: str, variable: str) -> str:
    return f"climatologies/{product}_{grid}_{region}_{variable}.zarr"


def _object_exists(key: str) -> bool:
    """True if the Zarr root metadata object is present (cheap HEAD, no listing).

    The mirror stores climatologies in Zarr v2 layout (``.zattrs``/``.zgroup``/
    ``.zmetadata``), not v3 (``zarr.json``) — check the consolidated-metadata
    marker, matching the ``consolidated=True`` open in ``_open_remote``.
    """
    url = f"{_GCS_MEDIA}/{urllib.parse.quote(key, safe='/')}/.zmetadata"
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


def _resolve_key(dataset: str, grid: str, region: str, variable: str) -> str:
    """Region-specific cache at ``grid``, else global at ``grid``, else error.

    Region names are not validated against Sheerwater's region list (that
    would pull in the full sheerwater geometry stack just for a name check).
    A typo'd region silently falls back to global from the cache's point of
    view, so the fallback is always logged to stderr — never silent.
    """
    product = _DATASETS[dataset]
    tried = []
    for candidate_region in dict.fromkeys([region, "global"]):
        key = _object_key(product, grid, candidate_region, variable)
        tried.append(key)
        if _object_exists(key):
            if candidate_region != region:
                print(
                    f"fetch-clim: no {dataset!r} climatology cached for "
                    f"region={region!r} at grid={grid!r}; falling back to "
                    f"region={candidate_region!r}",
                    file=sys.stderr,
                )
            return key
    raise DataError(
        f"no {dataset!r} climatology cached for region={region!r} or global "
        f"at grid={grid!r} (variable={variable!r}). Tried:\n  " + "\n  ".join(tried)
    )


_MEAN_KEY = "avg"
_VARIANCE_KEY = "variance"


def _resolve_variable_name(clim: xr.Dataset, requested: str) -> str:
    """The semantic variable name from the source's own attrs, not the raw data-var names.

    The mirror stores each mean/variance pair under generic names (``avg``,
    ``variance``); the actual physical variable is recorded in the global
    ``variable`` attr (e.g. ``"precip"``). Trust that over blindly assuming
    ``--variable`` matches a raw data-var name — but flag a mismatch loudly,
    since it means the bucket path and its own metadata disagree.
    """
    source_variable = clim.attrs.get("variable")
    if not source_variable:
        raise DataError(
            "cached climatology has no 'variable' attr; cannot determine the "
            f"physical variable name (expected {requested!r})."
        )
    if source_variable != requested:
        raise DataError(
            f"--variable {requested!r} was requested, but the cached climatology "
            f"at this path is stamped variable={source_variable!r}; the bucket "
            "path and its metadata disagree — check the object key."
        )
    return source_variable


def _standardize_units(clim: xr.Dataset, semantic_name: str) -> xr.Dataset:
    """Convert ``avg``/``variance`` to the kind's standard display units.

    Data-var names stay exactly as the source has them (``avg``, ``variance``)
    — only their ``units`` attr and values change. Classification uses
    ``semantic_name`` (the source's own ``variable`` global attr, e.g.
    ``"precip"``) rather than the raw data-var name, since ``avg``/``variance``
    carry no name hint of their own. Both vars are converted to the same
    standard units so they stay directly comparable.
    """
    if _MEAN_KEY not in clim.data_vars:
        return clim
    mean_da = clim[_MEAN_KEY]
    units = variable_units(mean_da)
    kind = classify_variable(
        semantic_name, units=units, standard_name=mean_da.attrs.get("standard_name")
    )
    if units is None or kind not in STANDARD:
        return clim
    dst_units = STANDARD[kind]["units"]
    dst_standard_name = STANDARD[kind]["standard_name"]

    clim = clim.copy()
    for name in (_MEAN_KEY, _VARIANCE_KEY):
        if name not in clim.data_vars:
            continue
        converted, _ = convert_dataarray(clim[name], dst_units)
        converted.attrs["units"] = dst_units
        clim[name] = converted
    if dst_standard_name:
        clim[_MEAN_KEY].attrs["standard_name"] = dst_standard_name
    return clim


def _open_remote(key: str) -> xr.Dataset:
    url = f"{_GCS_MEDIA}/{key}"
    try:
        return xr.open_zarr(url, consolidated=True)
    except Exception as exc:  # noqa: BLE001 — surface remote open failures cleanly
        raise DataError(
            f"failed to open remote climatology gs://{_BUCKET}/{key} ({exc})."
        ) from None


def _expand_climatology(clim: xr.Dataset, start, end) -> xr.Dataset:
    """Broadcast a 1904 day-of-year climatology onto every day in [start, end].

    ``clim``'s ``time`` dim carries 1904 dates (a leap year, so day_of_year
    spans 1..366 with no gaps) — every day-of-year the requested window could
    possibly need is guaranteed to resolve, regardless of whether the
    requested window itself spans a leap year.
    """
    doy = clim["time"].dt.dayofyear.values
    if sorted(doy.tolist()) != list(range(1, 367)):
        raise DataError(
            "expected a full 1904 leap-year climatology (day_of_year 1..366 "
            f"exactly once); got {sorted(set(doy.tolist()))}"
        )
    # Make day_of_year the indexing dimension (swap_dims is a relabel, not a
    # copy — valid here because the 366 values above are already unique).
    # Drop the old "time" coord once day_of_year is derived from it: swap_dims
    # leaves it behind as a non-dim coord, and it would otherwise collide with
    # the real "time" coord the indexer below introduces (same name, 1904 vs.
    # requested dates — xarray refuses to reconcile the two).
    clim = clim.assign_coords(day_of_year=("time", doy)).swap_dims({"time": "day_of_year"})
    clim = clim.drop_vars("time")

    target_dates = pd.date_range(start, end, freq="D")
    target_doy = xr.DataArray(target_dates.dayofyear, dims="time", coords={"time": target_dates})

    # Vectorized "left join": one climatology row gathered per requested date;
    # dates spanning more than one year repeat rows for free.
    expanded = clim.sel(day_of_year=target_doy)
    return expanded.drop_vars("day_of_year")


@weather_skill(name="fetch-clim", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset",
    required=True,
    choices=list(_DATASETS),
    help="Climatology source id.",
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    default=_DEFAULT_VARIABLE,
    help=f"Climate variable (default: {_DEFAULT_VARIABLE}).",
)
@weather_skill.argument(
    "--grid",
    default="global1_5",
    choices=list(_GRIDS),
    help="Sheerwater target grid (global0_25 = 0.25°, global1_5 = 1.5°).",
)
@weather_skill.argument(
    "--region",
    default="global",
    help=(
        "Sheerwater spatial region (default: global). Falls back to global "
        "if not cached for this --grid."
    ),
)
def fetch(dataset, start_time, end_time, variable, grid, region, **kwargs):
    """Fetch a cached daily climatology and expand it to the requested date range."""
    key = _resolve_key(dataset, grid, region, variable)
    print(f"fetch-clim: fetching gs://{_BUCKET}/{key}", file=sys.stderr)
    clim = _open_remote(key)

    source_variable = _resolve_variable_name(clim, variable)
    clim = _standardize_units(clim, source_variable)

    expanded = _expand_climatology(clim, start_time, end_time)
    expanded = expanded.load()
    # deep=False: clear only the Dataset-level (global) attrs — the source's
    # own leftovers (start_time, mask, prob_type, ...). deep=True would also
    # wipe the per-variable units/standard_name _standardize_units just set.
    expanded = expanded.drop_attrs(deep=False)

    expanded.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"sheerwater-mirror:{dataset}",
        climatology_dataset=dataset,
        climatology_variable=source_variable,
        climatology_grid=grid,
        climatology_region=region,
    )
    stamp_cf_attrs(expanded)
    return stamp_data_interval(expanded, period="1 day")


if __name__ == "__main__":
    fetch()
