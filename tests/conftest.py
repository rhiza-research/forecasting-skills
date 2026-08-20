"""Shared helpers for per-skill correctness tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import xarray as xr

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"

# Minimal valid 1x1 PNG (for fetcher/figure mocks).
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_gridded(
    n_time=2,
    lats=(1.0, 2.0, 3.0),
    lons=(10.0, 11.0, 12.0, 13.0),
    name="precip",
    fill=1.0,
    start="2026-01-01",
):
    """Small gridded Dataset with CF-style lat/lon/time coords."""
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    data = np.full((n_time, len(lats), len(lons)), fill, dtype=np.float64)
    ds = xr.Dataset(
        {name: (("time", "latitude", "longitude"), data)},
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )
    ds[name].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def make_forecast(
    n_step=3,
    lats=(1.0, 2.0),
    lons=(10.0, 11.0),
    name="tp",
    fill=1.0,
    init="2026-01-01",
    members=None,
):
    """Classic forecast: scalar ``time`` init + ``step`` lead dim (+ optional ``number``)."""
    steps = np.array([np.timedelta64(d, "D") for d in range(1, n_step + 1)])
    dims = ["step", "latitude", "longitude"]
    shape = [n_step, len(lats), len(lons)]
    coords = {
        "time": np.datetime64(init, "ns"),
        "step": steps,
        "latitude": list(lats),
        "longitude": list(lons),
    }
    if members is not None:
        dims = ["number", *dims]
        shape = [members, *shape]
        coords["number"] = list(range(members))
    data = np.full(shape, fill, dtype=np.float64)
    ds = xr.Dataset({name: (dims, data)}, coords=coords)
    ds[name].attrs.update(units="mm", long_name="Total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    return ds


def make_point_obs(
    n_time=3,
    n_points=2,
    name="precip",
    fill=1.0,
    start="2026-01-01",
):
    """Small point_obs Dataset with point_id + time."""
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    point_ids = [f"S{i}" for i in range(n_points)]
    lats = np.linspace(1.0, 2.0, n_points)
    lons = np.linspace(10.0, 11.0, n_points)
    data = np.full((n_points, n_time), fill, dtype=np.float64)
    ds = xr.Dataset(
        {name: (("point_id", "time"), data)},
        coords={
            "point_id": point_ids,
            "time": times.astype("datetime64[ns]"),
            "latitude": ("point_id", lats),
            "longitude": ("point_id", lons),
        },
    )
    ds[name].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def write_zarr(ds, path):
    """Write a consolidated Zarr store."""
    ds.to_zarr(path, mode="w", consolidated=True)
    return path


def load_skill(skill_dir_name, script_stem):
    """Import a skill script module by directory name and script stem."""
    path = SKILLS_ROOT / skill_dir_name / "scripts" / f"{script_stem}.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    mod_name = f"skill_{skill_dir_name}_{script_stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load skill script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_skill(fn, *argv):
    """Invoke a decorated skill with an argv list (in-process)."""
    return fn(list(argv))
