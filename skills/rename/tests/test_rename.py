"""Correctness tests for rename."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def rename():
    return load_skill("rename", "rename").rename


def test_rename_default_variable_and_stamps_history(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "--to-name", "rain")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "rain" in ds.data_vars
    assert "precip" not in ds.data_vars
    assert load_history(out)[-1]["skill"] == "rename"


def test_rename_explicit_variable(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "precip", "--to-name", "rain")

    ds = xr.open_zarr(out, consolidated=True)
    assert "rain" in ds.data_vars


def test_rename_accepts_precip_totals(tmp_path, rename):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "--to-name", "rain")

    result = xr.open_zarr(out, consolidated=True)
    assert "rain" in result.data_vars
    assert "sum" in result["rain"].attrs["cell_methods"]
