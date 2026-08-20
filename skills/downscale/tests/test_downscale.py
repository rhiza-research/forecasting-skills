"""Correctness tests for downscale."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def downscale():
    return load_skill("downscale", "downscale").downscale


def test_downscale_linear_factor2(tmp_path, downscale):
    src = write_zarr(
        make_gridded(lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "-f",
        "2",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] > 2
    assert ds.sizes["longitude"] > 2
    assert units_equal(ds["precip"].attrs.get("units"), "mm day-1")
    assert load_history(out)[-1]["skill"] == "downscale"


def test_downscale_requires_one_target(tmp_path, downscale):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(src),
            "-o",
            str(out),
            "--algorithm",
            "linear-interpolation",
        )
    assert exc.value.code == 2
