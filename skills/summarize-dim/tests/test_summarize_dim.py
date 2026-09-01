"""Correctness tests for summarize-dim."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def summarize_dim():
    return load_skill("summarize-dim", "summarize_dim").summarize_dim


def test_mean_collapses_time(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["precip"].values == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "summarize-dim"


def test_sum_collapses_longitude(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim, "-i", str(src), "-o", str(out), "--dim", "longitude", "--method", "sum"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "longitude" not in ds.dims
    assert ds["precip"].sizes["latitude"] == 3


def test_accepts_precip_totals(tmp_path, summarize_dim):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    result = xr.open_zarr(out, consolidated=True)
    assert "time" not in result.dims
    assert "sum" in result["precip"].attrs["cell_methods"]
