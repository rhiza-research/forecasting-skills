"""Correctness tests for convert-to-totals."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import AGGREGATION_PERIOD_ATTR


@pytest.fixture(scope="module")
def convert_to_totals():
    return load_skill("convert-to-totals", "convert_to_totals").convert_to_totals


def test_convert_to_totals_from_attr(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=10.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "1 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].values == pytest.approx(10.0)
    assert AGGREGATION_PERIOD_ATTR not in result["precip"].attrs
    assert load_history(out)[-1]["skill"] == "convert-to-totals"


def test_convert_to_totals_cli_override(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=5.0)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        convert_to_totals,
        "-i",
        str(src),
        "-o",
        str(out),
        "--aggregation-period",
        "1 day",
    )

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].values == pytest.approx(5.0)


def test_convert_to_totals_singleton_time(tmp_path, convert_to_totals):
    """A single aggregated bin (e.g. one weekly mean) converts without a spacing gate."""
    ds = make_gridded(n_time=1, fill=2.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "7 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].values == pytest.approx(14.0)
