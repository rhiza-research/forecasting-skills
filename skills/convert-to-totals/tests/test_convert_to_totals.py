"""Correctness tests for convert-to-totals."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
)


@pytest.fixture(scope="module")
def convert_to_totals():
    return load_skill("convert-to-totals", "convert_to_totals").convert_to_totals


@pytest.fixture(scope="module")
def aggregate():
    return load_skill("aggregate-temporal", "aggregate").aggregate


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


def test_convert_to_totals_refuses_finer_than_period(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=1.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "21 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_convert_to_totals_requires_stamped_period(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=5.0)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


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


def test_convert_min_coverage_default_rejects_partial(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=1, fill=1.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "21 day"
    ds["precip"].attrs[DATA_INTERVAL_ATTR] = "1 day"
    ds = ds.assign_coords({AGGREGATION_COVERAGE_COORD: ("time", [0.9])})
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2

    run_skill(
        convert_to_totals,
        "-i",
        str(src),
        "-o",
        str(out),
        "--min-coverage",
        "0.6",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].values == pytest.approx(21.0)


def test_convert_refuses_overlapping_21_day(tmp_path, convert_to_totals):
    times = np.array(["2026-01-21", "2026-01-31"], dtype="datetime64[ns]")
    data = np.ones((2, 2, 2))
    ds = xr.Dataset(
        {
            "precip": (
                ("time", "latitude", "longitude"),
                data,
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    AGGREGATION_PERIOD_ATTR: "21 day",
                    DATA_INTERVAL_ATTR: "1 day",
                },
            )
        },
        coords={
            "time": times,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_full_week_of_halfhourly_converts(tmp_path, aggregate, convert_to_totals):
    n = 7 * 48
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-01T00:00") + np.timedelta64(n * 30, "m"),
        np.timedelta64(30, "m"),
    )
    ds = xr.Dataset(
        {
            "precip": (
                ("time", "latitude", "longitude"),
                np.full((n, 1, 1), 1.0),
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    DATA_INTERVAL_ATTR: "30 minute",
                },
            )
        },
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    src = write_zarr(ds, tmp_path / "half.zarr")
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(convert_to_totals, "-i", str(weekly), "-o", str(totals))

    result = xr.open_zarr(totals, consolidated=True)
    assert result.sizes["time"] == 1
    assert result["precip"].attrs["units"] == "mm"
    assert float(result["precip"].values.flat[0]) == pytest.approx(7.0)


def test_convert_to_totals_refuses_precip_totals(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=1, fill=10.0)
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "1 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
