"""Correctness tests for deaccumulate."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def deaccumulate():
    return load_skill("deaccumulate", "deaccumulate").deaccumulate


def test_deaccumulate_cumulative_tp(tmp_path, deaccumulate):
    ds = make_forecast(n_step=3, lats=(1.0,), lons=(10.0,), fill=0.0)
    data = np.array([[[1.0]], [[3.0]], [[6.0]]])
    ds = ds.assign(tp=(("step", "latitude", "longitude"), data))
    ds["tp"].attrs.update(units="mm", long_name="Total precipitation")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    result = xr.open_zarr(out, consolidated=True)
    assert result.sizes["step"] == 2
    vals = result["tp"].values.flatten()
    assert vals[0] == pytest.approx(2.0)
    assert vals[1] == pytest.approx(3.0)
    assert result["tp"].attrs.get("data_interval") == "1 day"
    assert "step_bounds" not in result.variables
    assert load_history(out)[-1]["skill"] == "deaccumulate"


def test_deaccumulate_needs_two_steps(tmp_path, deaccumulate):
    ds = make_forecast(n_step=1)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(deaccumulate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
