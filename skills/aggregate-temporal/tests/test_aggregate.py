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


def test_aggregate_keeps_incomplete_trailing_week(tmp_path, aggregate, capsys):
    """15 daily samples → 3 weeks; last bin is kept and stamped coverage 1/7."""
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 3
    assert float(ds["aggregation_coverage"].values[-1]) == pytest.approx(1 / 7)
    err = capsys.readouterr().err
    assert "dropped" not in err


def test_aggregate_keeps_incomplete_trailing_month(tmp_path, aggregate):
    # Jan (31) + Feb 1..10 → keep February with coverage 10/28 (2026 is not a leap year).
    src = write_zarr(
        make_gridded(n_time=41, fill=1.0, start="2026-01-01"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "monthly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    assert str(ds.time.values[0])[:10] == "2026-01-01"
    assert str(ds.time.values[1])[:10] == "2026-02-01"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(10 / 28)


def test_aggregate_end_time_weekly_two_bins(tmp_path, aggregate):
    """15 days through 2026-08-30 → [Aug 16, Aug 23) and [Aug 23, Aug 30), left-labeled."""
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
    assert labels == ["2026-08-16", "2026-08-23"]
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(1.0)


def test_aggregate_end_time_keeps_incomplete_leading(tmp_path, aggregate, capsys):
    """Series starting mid-bin: leading short week is kept and stamped coverage < 1."""
    # 2026-08-20 .. 2026-08-30 (11 days): full [Aug 23, Aug 30), partial [Aug 16, Aug 23).
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
    labels = [str(t)[:10] for t in ds.time.values]
    assert labels == ["2026-08-16", "2026-08-23"]
    assert float(ds["aggregation_coverage"].values[0]) < 1.0
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(1.0)
    assert "dropped" not in capsys.readouterr().err


def test_end_time_weekly_left_edge_matches_forecast_week(tmp_path, aggregate):
    """Obs week [Aug 24, Aug 31) is labeled Aug 24, same as forecast step 0 after step-to-time."""
    src = write_zarr(
        make_gridded(n_time=7, fill=1.0, start="2026-08-24"),
        tmp_path / "obs.zarr",
    )
    out = tmp_path / "obs_weekly.zarr"
    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-31",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert str(ds.time.values[0])[:10] == "2026-08-24"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)


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
    """19 of 21 daily samples → one bin with coverage 19/21."""
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
    weekly_idx = {
        int(np.asarray(s).astype("timedelta64[D]").astype(int)): i
        for i, s in enumerate(weekly_ds.step.values)
    }
    assert 14 in weekly_idx
    assert float(weekly_ds["aggregation_coverage"].values[weekly_idx[14]]) == pytest.approx(1.0)

    out = xr.open_zarr(totals, consolidated=True)
    # convert-to-totals --min-coverage 1.0 may drop incomplete weeks, so index the output.
    out_idx = {
        int(np.asarray(s).astype("timedelta64[D]").astype(int)): i
        for i, s in enumerate(out.step.values)
    }
    assert 14 in out_idx
    # Week starting 14d is [14d, 21d) = tp[21] - tp[14]
    assert float(out["tp"].values[out_idx[14]].flat[0]) == pytest.approx(
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


def test_aggregate_all_incomplete_weeks_kept(tmp_path, aggregate):
    """Three daily samples still form one weekly bin; coverage is 3/7."""
    src = write_zarr(make_gridded(n_time=3, fill=1.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(3 / 7)


def test_convert_default_drops_incomplete_weeks_kept_by_aggregate(tmp_path, aggregate):
    """aggregate keeps the trailing 1-day week; convert-to-totals default drops it."""
    from conftest import load_skill

    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals))

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    totals_ds = xr.open_zarr(totals, consolidated=True)
    assert weekly_ds.sizes["time"] == 3
    assert totals_ds.sizes["time"] == 2
    assert float(weekly_ds["aggregation_coverage"].values[-1]) == pytest.approx(1 / 7)


def _gefs_like_mixed_step():
    h3 = np.arange(3, 241, 3)
    h6 = np.arange(246, 841, 6)
    hours = np.concatenate([h3, h6])
    steps = hours.astype("timedelta64[h]").astype("timedelta64[ns]")
    ds = xr.Dataset(
        {
            "precipitation_surface": (
                ("step", "latitude", "longitude"),
                np.ones((hours.size, 1, 1)),
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    "data_interval": "3 hour",
                },
            )
        },
        coords={
            "step": steps,
            "time": np.datetime64("2026-08-18", "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def test_gefs_like_mixed_step_weekly_then_totals(tmp_path, aggregate):
    from conftest import load_skill

    step_to_time = load_skill("step-to-time", "step_to_time").step_to_time
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(_gefs_like_mixed_step(), tmp_path / "gefs.zarr")
    as_time = tmp_path / "gefs_time.zarr"
    weekly = tmp_path / "gefs_weekly.zarr"
    totals = tmp_path / "gefs_weekly_mm.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(as_time))
    run_skill(aggregate, "-i", str(as_time), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals), "--min-coverage", "0.5")

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    out = xr.open_zarr(totals, consolidated=True)
    assert weekly_ds.sizes["time"] >= out.sizes["time"] >= 1
    assert "aggregation_coverage" in weekly_ds.coords
    assert out["precipitation_surface"].attrs["units"] == "mm"


def test_gefs_unfilled_long_leads_stay_nan(tmp_path, aggregate):
    """Unpublished GEFS leads are NaN; duration-weighted mean must not turn them into 0."""
    ds = _gefs_like_mixed_step()
    ds["precipitation_surface"].attrs.pop("data_interval", None)
    ds["precipitation_surface"] = ds["precipitation_surface"].where(
        ds["step"] <= np.timedelta64(21, "D")
    )
    src = write_zarr(ds, tmp_path / "gefs_partial.zarr")
    weekly = tmp_path / "gefs_partial_weekly.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")

    out = xr.open_zarr(weekly, consolidated=True)
    days = np.asarray(out["step"].values).astype("timedelta64[D]").astype(int)
    vals = np.asarray(out["precipitation_surface"].values).reshape(days.size)
    filled = {int(d): v for d, v in zip(days, vals, strict=True)}
    assert filled[0] == pytest.approx(1.0)
    assert filled[7] == pytest.approx(1.0)
    assert filled[14] == pytest.approx(1.0)
    assert np.isnan(filled[21])
    assert np.isnan(filled[28])
    assert 35 not in filled
    cov = {int(d): c for d, c in zip(days, out["aggregation_coverage"].values, strict=True)}
    assert cov[0] == pytest.approx(1.0)
    assert cov[7] == pytest.approx(1.0)
    assert cov[14] == pytest.approx(1.0)
    assert cov[21] == pytest.approx(0.0)
    assert cov[28] == pytest.approx(0.0)


def test_nan_days_do_not_count_as_coverage(tmp_path, aggregate):
    """A missing day inside an otherwise complete week stamps coverage 6/7."""
    ds = make_gridded(n_time=7, fill=2.0)
    ds["precip"].values[3] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)
    assert weekly.sizes["time"] == 1
    assert float(weekly["aggregation_coverage"].values[0]) == pytest.approx(6 / 7)
    assert float(weekly["precip"].values.flat[0]) == pytest.approx(2.0)


def test_spatial_nan_hole_does_not_reduce_coverage(tmp_path, aggregate):
    """A persistent spatial hole is not a missing time sample."""
    ds = make_gridded(n_time=7, fill=2.0)
    ds["precip"].values[:, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)
    assert float(weekly["aggregation_coverage"].values[0]) == pytest.approx(1.0)
