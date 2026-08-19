"""Correctness tests for reduce."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def reduce():
    return load_skill("reduce", "reduce").reduce


def test_reduce_mean_collapses_time(tmp_path, reduce):
    src = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(reduce, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["precip"].values == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "reduce"


def test_reduce_sum_collapses_longitude(tmp_path, reduce):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(reduce, "-i", str(src), "-o", str(out), "--dim", "longitude", "--method", "sum")

    ds = xr.open_zarr(out, consolidated=True)
    assert "longitude" not in ds.dims
    assert ds["precip"].sizes["latitude"] == 3


def test_reduce_refuses_precip_totals(tmp_path, reduce):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(reduce, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")
    assert exc.value.code == 2
