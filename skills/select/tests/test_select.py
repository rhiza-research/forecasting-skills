"""Correctness tests for select."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def select():
    return load_skill("select", "select_dim").select


def test_select_by_index_collapses_dim(tmp_path, select):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--index", "1")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert load_history(out)[-1]["skill"] == "select"


def test_select_requires_index_or_value(tmp_path, select):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time")
    assert exc.value.code == 2
