"""Synthetic weather-skills Zarr fixtures for transform smoke tests."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_core() -> Path | None:
    """Editable core override: WEATHER_SKILLS_CORE, sibling checkout, or .deps/."""
    env = os.environ.get("WEATHER_SKILLS_CORE")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            REPO_ROOT.parent / "weather-skills-core",
            REPO_ROOT / ".deps" / "weather-skills-core",
        ]
    )
    for path in candidates:
        if path.is_dir() and (path / "pyproject.toml").is_file():
            return path.resolve()
    return None


CORE_SIBLING = resolve_core()


def make_gridded(
    n_time=2,
    lats=(1.0, 2.0, 3.0),
    lons=(10.0, 11.0, 12.0, 13.0),
    name="precip",
    fill=1.0,
    start="2026-01-01",
    units="mm/day",
):
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    data = np.full((n_time, len(lats), len(lons)), fill, dtype=np.float32)
    ds = xr.Dataset(
        {name: (("time", "latitude", "longitude"), data)},
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )
    ds[name].attrs.update(units=units, standard_name="lwe_precipitation_rate")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def write_zarr(path: Path, ds: xr.Dataset | None = None, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    (ds if ds is not None else make_gridded(**kwargs)).to_zarr(path, mode="w", consolidated=True)
    return path


@pytest.fixture
def gridded_a(tmp_path) -> Path:
    return write_zarr(tmp_path / "a.zarr", fill=5.0)


@pytest.fixture
def gridded_b(tmp_path) -> Path:
    return write_zarr(tmp_path / "b.zarr", fill=2.0)
