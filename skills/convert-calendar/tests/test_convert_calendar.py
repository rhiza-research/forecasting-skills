"""Correctness tests for convert-calendar."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def convert_calendar():
    return load_skill("convert-calendar", "convert_calendar").convert_calendar


def test_convert_calendar_to_noleap(tmp_path, convert_calendar):
    src = write_zarr(make_gridded(n_time=2), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "noleap")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" in ds.dims
    assert ds.sizes["time"] == 2
    assert load_history(out)[-1]["skill"] == "convert-calendar"


def test_convert_calendar_360_day_needs_align_on(tmp_path, convert_calendar):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "360_day")
    assert exc.value.code == 2
