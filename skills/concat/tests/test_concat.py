"""Correctness tests for concat."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def concat():
    return load_skill("concat", "concat").concat


def test_concat_along_new_dim_with_coords(tmp_path, concat):
    d1 = write_zarr(make_gridded(fill=1.0), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(fill=2.0), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        concat,
        "-i",
        str(d1),
        str(d2),
        "-o",
        str(out),
        "--dim",
        "number",
        "--coords",
        "0,1",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["number"] == 2
    assert list(ds["number"].values) == [0, 1]
    assert load_history(out)[-1]["skill"] == "concat"


def test_concat_along_time(tmp_path, concat):
    d1 = write_zarr(make_gridded(n_time=2, start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(n_time=2, start="2026-01-03"), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(concat, "-i", str(d1), str(d2), "-o", str(out), "--dim", "time")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 4
