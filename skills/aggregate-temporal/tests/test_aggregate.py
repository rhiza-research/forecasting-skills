"""Correctness tests for aggregate-temporal."""

from pathlib import Path

import numpy as np
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
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert load_history(out)[-1]["skill"] == "aggregate-temporal"


def test_aggregate_drops_incomplete_trailing_week(tmp_path, aggregate, capsys):
    """15 daily samples → 2 full weeks; trailing 1-day bin dropped by default."""
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    err = capsys.readouterr().err
    assert "dropped 1 incomplete weekly bin" in err
    assert "--keep-partial" in err


def test_aggregate_keep_partial_retains_trailing_week(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--keep-partial",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 3


def test_aggregate_drops_incomplete_trailing_month(tmp_path, aggregate):
    # Jan (31) + Feb 1..10 → drop incomplete February.
    src = write_zarr(
        make_gridded(n_time=41, fill=1.0, start="2026-01-01"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "monthly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert str(ds.time.values[0])[:10] == "2026-01-01"


def test_aggregate_end_time_weekly_two_bins(tmp_path, aggregate):
    """15 days ending 2026-08-30 → bins labeled 2026-08-23 and 2026-08-30."""
    src = write_zarr(
        make_gridded(n_time=15, fill=2.0, start="2026-08-16"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    labels = [str(t)[:10] for t in ds.time.values]
    assert labels == ["2026-08-23", "2026-08-30"]


def test_aggregate_end_time_drops_incomplete_leading(tmp_path, aggregate, capsys):
    """Series starting mid-bin: leading short week dropped unless --keep-partial."""
    # 2026-08-20 .. 2026-08-30 (11 days): full week ending 30, partial week ending 23.
    src = write_zarr(
        make_gridded(n_time=11, fill=1.0, start="2026-08-20"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert str(ds.time.values[0])[:10] == "2026-08-30"
    assert "dropped 1 incomplete weekly bin" in capsys.readouterr().err


def test_aggregate_duration_21_day(tmp_path, aggregate):
    """Pint duration --period '21 day' bins 21 daily samples into one complete window."""
    src = write_zarr(make_gridded(n_time=21, fill=3.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "21 day")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["precip"].values.flat[0]) == pytest.approx(3.0)
    assert ds["precip"].attrs.get("aggregation_period") == "21 day"


def test_aggregate_21_day_partial_stamps_coverage(tmp_path, aggregate):
    """19 of 21 daily samples → coverage ~0.9 when --keep-partial is set."""
    src = write_zarr(make_gridded(n_time=19, fill=3.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "21 day",
        "--keep-partial",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert ds["precip"].attrs.get("aggregation_period") == "21 day"
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(19 / 21)


def test_aggregate_requires_period_or_window(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=7), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def _s2s_accum_forecast():
    """S2S-like steps with known cumulative tp (mm)."""
    import numpy as np
    import xarray as xr

    days = [0, 7, 10, 14, 20, 21, 28]
    # Amounts added in each interval after step 0: 7, 6, 12, 24, 10, 35
    increments = [0.0, 7.0, 6.0, 12.0, 24.0, 10.0, 35.0]
    accum = np.cumsum(increments)
    ds = xr.Dataset(
        {
            "tp": (
                ("step", "latitude", "longitude"),
                accum.reshape(-1, 1, 1),
                {"units": "mm", "standard_name": "lwe_thickness_of_precipitation_amount"},
            )
        },
        coords={
            "step": np.array(days, dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "time": np.datetime64("2026-01-01", "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    return ds, accum, days


def test_irregular_weekly_totals_match_tp_differences(tmp_path, aggregate):
    from conftest import load_skill

    deaccumulate = load_skill("deaccumulate", "deaccumulate").deaccumulate
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    ds, accum, days = _s2s_accum_forecast()
    src = write_zarr(ds, tmp_path / "in.zarr")
    rates = tmp_path / "rates.zarr"
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(rates))
    run_skill(aggregate, "-i", str(rates), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals))

    rates_ds = xr.open_zarr(rates, consolidated=True)
    assert "step_bounds" in rates_ds.coords or "step_bounds" in rates_ds.variables
    assert rates_ds["tp"].attrs.get("data_interval") is None

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    idx = {
        int(np.asarray(s).astype("timedelta64[D]").astype(int)): i
        for i, s in enumerate(weekly_ds.step.values)
    }
    assert 21 in idx
    assert float(weekly_ds["aggregation_coverage"].values[idx[21]]) == pytest.approx(1.0)

    out = xr.open_zarr(totals, consolidated=True)
    # Week 3 is (14d, 21d] = tp[21] - tp[14]
    assert float(out["tp"].values[idx[21]].flat[0]) == pytest.approx(
        accum[days.index(21)] - accum[days.index(14)]
    )


def test_window_refused_on_cf_bounds(tmp_path, aggregate):
    from conftest import load_skill

    deaccumulate = load_skill("deaccumulate", "deaccumulate").deaccumulate
    ds, _, _ = _s2s_accum_forecast()
    src = write_zarr(ds, tmp_path / "in.zarr")
    rates = tmp_path / "rates.zarr"
    out = tmp_path / "out.zarr"
    run_skill(deaccumulate, "-i", str(src), "-o", str(rates))
    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(rates), "-o", str(out), "--window", "7")
    assert exc.value.code == 2


def test_reaggregate_weekly_to_21_day_expects_three_weeks(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=21, fill=2.0), tmp_path / "daily.zarr")
    weekly = tmp_path / "weekly.zarr"
    out = tmp_path / "21d.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(aggregate, "-i", str(weekly), "-o", str(out), "--period", "21 day")
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
