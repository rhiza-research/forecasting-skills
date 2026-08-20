"""Correctness tests for step-to-time."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def step_to_time():
    return load_skill("step-to-time", "step_to_time").step_to_time


def _forecast_without_precip_totals(**kwargs):
    ds = make_forecast(name="t2m", **kwargs)
    ds["t2m"].attrs.update(units="K", standard_name="air_temperature", long_name="Temperature")
    return ds


def test_step_to_time_replaces_step_dim(tmp_path, step_to_time):
    src = write_zarr(_forecast_without_precip_totals(n_step=3, init="2026-01-01"), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "step" not in ds.dims
    assert "time" in ds.dims
    assert ds.sizes["time"] == 3
    expected = np.array(
        ["2026-01-02", "2026-01-03", "2026-01-04"],
        dtype="datetime64[ns]",
    )
    assert np.array_equal(ds["time"].values.astype("datetime64[D]"), expected.astype("datetime64[D]"))
    assert load_history(out)[-1]["skill"] == "step-to-time"


def test_step_to_time_accepts_precip_totals(tmp_path, step_to_time):
    ds = make_forecast(n_step=3, init="2026-01-01")
    ds["tp"].attrs.update(
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="step: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert "time" in result.dims
    assert "sum" in result["tp"].attrs["cell_methods"]


def test_step_to_time_rewrites_cf_bounds(tmp_path, step_to_time):
    from weather_skills_core.units import stamp_data_interval

    ds = _forecast_without_precip_totals(n_step=4, init="2026-01-01")
    ds = ds.assign_coords(
        step=np.array([7, 10, 14, 21], dtype="timedelta64[D]").astype("timedelta64[ns]")
    )
    ds = stamp_data_interval(ds, dim="step")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(out))
    result = xr.open_zarr(out, consolidated=True)
    assert result["time"].attrs.get("bounds") == "time_bounds"
    assert "step_bounds" not in result.variables
    bounds = np.asarray(result["time_bounds"].values)
    assert bounds.shape == (4, 2)
    assert str(bounds[0, 0])[:10] == "2026-01-01"
    assert str(bounds[0, 1])[:10] == "2026-01-08"
