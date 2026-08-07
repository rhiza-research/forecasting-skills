"""Correctness tests for aggregate-temporal."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def aggregate():
    return load_skill("aggregate-temporal", "aggregate").aggregate


def test_aggregate_weekly_mean(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=14, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--method",
        "mean",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    assert float(ds["precip"].values.flat[0]) == pytest.approx(2.0)
    assert ds["precip"].attrs.get("aggregation_period") == "7 day"
    assert load_history(out)[-1]["skill"] == "aggregate-temporal"


def test_aggregate_requires_period_or_window(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=7), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
