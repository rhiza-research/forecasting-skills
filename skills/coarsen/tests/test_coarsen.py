"""Correctness tests for coarsen."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def coarsen():
    return load_skill("coarsen", "coarsen").coarsen


def test_coarsen_reduces_spatial_dims(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "2.0",
        "--offset",
        "0.0",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] < 3
    assert ds.sizes["longitude"] < 4
    assert load_history(out)[-1]["skill"] == "coarsen"


def test_coarsen_rejects_finer_target(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            coarsen,
            "-i",
            str(src),
            "-o",
            str(out),
            "--target-resolution",
            "0.5",
            "--offset",
            "0.0",
        )
    assert exc.value.code == 2
