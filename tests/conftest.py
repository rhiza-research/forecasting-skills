"""Shared helpers for per-skill correctness tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import xarray as xr

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"


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
    data = np.full((n_time, len(lats), len(lons)), fill)
    return xr.Dataset(
        {name: (("time", "latitude", "longitude"), data)},
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )


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
