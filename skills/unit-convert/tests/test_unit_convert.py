"""Correctness tests for unit-convert."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def unit_convert():
    return load_skill("unit-convert", "unit-convert").unit_convert


def test_unit_convert_to_standard(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-standard")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")
    assert load_history(out)[-1]["skill"] == "unit-convert"


def test_unit_convert_rejects_both_targets(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert,
            "-i",
            str(src),
            "-o",
            str(out),
            "--to-standard",
            "--to-units",
            "mm day-1",
        )
    assert exc.value.code == 2


def test_unit_convert_requires_target(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
