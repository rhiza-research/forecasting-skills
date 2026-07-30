"""Smoke tests for middle-pipeline transform skills (synthetic Zarrs, no network)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import xarray as xr

from conftest import CORE_SIBLING, REPO_ROOT, make_gridded, write_zarr

SCRIPTS = REPO_ROOT / "skills"


def run_skill(script: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["uv", "run"]
    if CORE_SIBLING is not None:
        cmd += ["--with-editable", str(CORE_SIBLING)]
    cmd += ["--script", str(script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def test_difference(tmp_path, gridded_a, gridded_b):
    out = tmp_path / "diff.zarr"
    run_skill(
        SCRIPTS / "difference/scripts/difference.py",
        "-i",
        str(gridded_a),
        "-i",
        str(gridded_b),
        "-o",
        str(out),
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(ds["precip"].values, 3.0)
    history = json.loads(ds.attrs["weather_skills_history"])
    assert history[-1]["skill"] == "difference"


def test_rename(tmp_path, gridded_a):
    out = tmp_path / "renamed.zarr"
    run_skill(
        SCRIPTS / "rename/scripts/rename.py",
        "-i",
        str(gridded_a),
        "-o",
        str(out),
        "--to-name",
        "rainfall",
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "rainfall" in ds.data_vars
    assert "precip" not in ds.data_vars


def test_select_by_index(tmp_path, gridded_a):
    out = tmp_path / "sel.zarr"
    run_skill(
        SCRIPTS / "select/scripts/select_dim.py",
        "-i",
        str(gridded_a),
        "-o",
        str(out),
        "--dim",
        "time",
        "--index",
        "0",
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1


def test_reduce_mean_time(tmp_path, gridded_a):
    out = tmp_path / "reduced.zarr"
    run_skill(
        SCRIPTS / "reduce/scripts/reduce.py",
        "-i",
        str(gridded_a),
        "-o",
        str(out),
        "--dim",
        "time",
        "--method",
        "mean",
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    np.testing.assert_allclose(ds["precip"].values, 5.0)


def test_unit_convert_mm_day_to_kg_m2_s1(tmp_path):
    src = write_zarr(tmp_path / "in.zarr", make_gridded(fill=86.4, units="mm/day"))
    out = tmp_path / "converted.zarr"
    run_skill(
        SCRIPTS / "unit-convert/scripts/unit-convert.py",
        "-i",
        str(src),
        "-o",
        str(out),
        "--to-units",
        "kg m-2 s-1",
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    # 86.4 mm/day = 1e-3 kg m-2 s-1 via liquid-water density bridge
    np.testing.assert_allclose(ds["precip"].values, 1e-3, rtol=1e-5)
    assert ds["precip"].attrs["units"] == "kg m-2 s-1"


def test_concat_along_time(tmp_path):
    a = write_zarr(tmp_path / "a.zarr", make_gridded(n_time=2, start="2026-01-01", fill=1.0))
    b = write_zarr(tmp_path / "b.zarr", make_gridded(n_time=2, start="2026-01-03", fill=2.0))
    out = tmp_path / "cat.zarr"
    run_skill(
        SCRIPTS / "concat/scripts/concat.py",
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--dim",
        "time",
        "--no-check-cache",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 4

