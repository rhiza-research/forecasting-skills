"""Correctness tests for indicator."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core import UsageError
from weather_skills_core.provenance import load_history
from weather_skills_core.units import DATA_INTERVAL_ATTR


@pytest.fixture(scope="module")
def indicator_mod():
    return load_skill("indicator", "indicator")


@pytest.fixture(scope="module")
def indicator_fn(indicator_mod):
    return indicator_mod.indicator


def _daily(n_time, fill=0.0, name="precip", start="2026-01-01"):
    ds = make_gridded(n_time=n_time, lats=(1.0,), lons=(10.0,), name=name, fill=fill, start=start)
    ds[name].attrs[DATA_INTERVAL_ATTR] = "1 day"
    return ds


def _cell(ds, name="indicator"):
    return xr.open_zarr(ds, consolidated=True)[name].isel(latitude=0, longitude=0)


def test_parse_alias_matches_expanded(indicator_mod):
    parse_rule = indicator_mod.parse_rule
    alias = parse_rule("icpac-onset")
    written = parse_rule("precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d")
    assert alias.clauses == written.clauses
    assert alias.combinator == written.combinator
    chc = parse_rule("chc-onset")
    chc_w = parse_rule("precip sum 10d > 25 and precip sum 20d > 20 after 10d")
    assert chc.clauses == chc_w.clauses


def test_parse_rejects_mixed_combinators(indicator_mod):
    with pytest.raises(UsageError, match="mixing"):
        indicator_mod.parse_rule("precip sum 1d >= 1 and precip sum 1d < 1 or precip sum 1d >= 0")


def test_parse_rejects_after_and_within(indicator_mod):
    with pytest.raises(UsageError, match="after"):
        indicator_mod.parse_rule("precip sum 3d >= 20 after 1d within 2d")


def test_eight_day_sum_left_labeled(tmp_path, indicator_fn):
    ds = _daily(16, fill=0.0)
    ds["precip"].values[5:13, 0, 0] = 4.0  # 8 days × 4 mm = 32
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(indicator_fn, "-i", str(src), "-o", str(out), "--rule", "precip sum 8d >= 25")
    hit = _cell(out).values
    times = xr.open_zarr(out, consolidated=True).time.values
    # Left-labeled: window starting day 5 covers days 5–12.
    t5 = np.datetime64("2026-01-06")
    t4 = np.datetime64("2026-01-05")
    assert hit[times == t5] == pytest.approx(1)
    # Day 4 window includes one zero + 7×4 = 28 >= 25
    assert hit[times == t4] == pytest.approx(1)
    # Day 0 window is all zeros
    assert hit[0] == pytest.approx(0)
    assert load_history(out)[-1]["skill"] == "indicator"


def test_ten_day_sum_below(tmp_path, indicator_fn):
    wet = _daily(12, fill=1.0)
    dry = _daily(12, fill=0.5)
    wet_p = write_zarr(wet, tmp_path / "wet.zarr")
    dry_p = write_zarr(dry, tmp_path / "dry.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(wet_p),
        "-o",
        str(tmp_path / "wet_out.zarr"),
        "--rule",
        "precip sum 10d < 9",
    )
    run_skill(
        indicator_fn,
        "-i",
        str(dry_p),
        "-o",
        str(tmp_path / "dry_out.zarr"),
        "--rule",
        "precip sum 10d < 9",
    )
    assert _cell(tmp_path / "wet_out.zarr").values[0] == pytest.approx(0)
    assert _cell(tmp_path / "dry_out.zarr").values[0] == pytest.approx(1)


def test_icpac_onset_and_false_start(tmp_path, indicator_fn):
    ok = _daily(40, fill=2.0)
    ok["precip"].values[0:3, 0, 0] = 10.0
    src = write_zarr(ok, tmp_path / "ok.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "ok_out.zarr"),
        "--rule",
        "icpac-onset",
    )
    assert _cell(tmp_path / "ok_out.zarr").values[0] == pytest.approx(1)

    false = _daily(40, fill=2.0)
    false["precip"].values[0:3, 0, 0] = 10.0
    false["precip"].values[3:10, 0, 0] = 0.0
    src_f = write_zarr(false, tmp_path / "false.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src_f),
        "-o",
        str(tmp_path / "false_out.zarr"),
        "--rule",
        "icpac-onset",
    )
    assert _cell(tmp_path / "false_out.zarr").values[0] == pytest.approx(0)


def test_icpac_incomplete_lookahead_is_nan(tmp_path, indicator_fn):
    ds = _daily(10, fill=10.0)
    src = write_zarr(ds, tmp_path / "short.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "short_out.zarr"),
        "--rule",
        "icpac-onset",
    )
    assert np.isnan(_cell(tmp_path / "short_out.zarr").values[0])


def test_chc_onset_strict_gt_and_follow_on(tmp_path, indicator_fn):
    ds = _daily(40, fill=0.0)
    ds["precip"].values[0:10, 0, 0] = 3.0  # 30 > 25
    ds["precip"].values[10:30, 0, 0] = 1.1  # 22 > 20
    src = write_zarr(ds, tmp_path / "chc.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "chc_out.zarr"),
        "--rule",
        "chc-onset",
    )
    assert _cell(tmp_path / "chc_out.zarr").values[0] == pytest.approx(1)

    exact = _daily(40, fill=0.0)
    exact["precip"].values[0:10, 0, 0] = 2.5  # 25 not > 25
    exact["precip"].values[10:30, 0, 0] = 1.0  # 20 not > 20
    src_e = write_zarr(exact, tmp_path / "exact.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src_e),
        "-o",
        str(tmp_path / "exact_out.zarr"),
        "--rule",
        "chc-onset",
    )
    assert _cell(tmp_path / "exact_out.zarr").values[0] == pytest.approx(0)

    dry_follow = _daily(40, fill=0.0)
    dry_follow["precip"].values[0:10, 0, 0] = 3.0
    src_d = write_zarr(dry_follow, tmp_path / "dryf.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src_d),
        "-o",
        str(tmp_path / "dryf_out.zarr"),
        "--rule",
        "chc-onset",
    )
    assert _cell(tmp_path / "dryf_out.zarr").values[0] == pytest.approx(0)


def test_alias_matches_written_rule_on_data(tmp_path, indicator_fn):
    ds = _daily(40, fill=2.0)
    ds["precip"].values[0:3, 0, 0] = 10.0
    src = write_zarr(ds, tmp_path / "in.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "alias.zarr"),
        "--rule",
        "icpac-onset",
    )
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "written.zarr"),
        "--rule",
        "precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d",
    )
    np.testing.assert_array_equal(
        _cell(tmp_path / "alias.zarr").values,
        _cell(tmp_path / "written.zarr").values,
    )


def test_ensemble_probability(tmp_path, indicator_fn):
    ds = _daily(8, fill=0.0)
    ds = ds.expand_dims(number=[0, 1, 2, 3]).copy(deep=True)
    ds["precip"].values[0:2, :, 0, 0] = 4.0
    src = write_zarr(ds, tmp_path / "ens.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "p.zarr"),
        "--rule",
        "precip sum 8d >= 25",
        "--probability",
    )
    out = xr.open_zarr(tmp_path / "p.zarr", consolidated=True)
    assert "probability" in out
    assert "number" not in out.dims
    assert out["probability"].isel(time=0, latitude=0, longitude=0).values == pytest.approx(0.5)


def test_detect_first_and_cumulative_probability(tmp_path, indicator_fn):
    ds = _daily(12, fill=0.0)
    ds["precip"].values[4:12, 0, 0] = 4.0
    src = write_zarr(ds, tmp_path / "in.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "first.zarr"),
        "--rule",
        "precip sum 8d >= 25",
        "--detect",
        "first",
    )
    first = xr.open_zarr(tmp_path / "first.zarr", consolidated=True)
    assert "indicator_time" in first
    got = first["indicator_time"].isel(latitude=0, longitude=0).values
    assert np.datetime64(got, "D") == np.datetime64("2026-01-04")

    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "cdf.zarr"),
        "--rule",
        "precip sum 8d >= 25",
        "--cumulative",
        "--probability",
    )
    cdf = _cell(tmp_path / "cdf.zarr", "probability").values
    assert cdf[0] == pytest.approx(0)
    assert cdf[-1] == pytest.approx(1)


def test_detect_first_rejects_probability_and_cumulative(tmp_path, indicator_fn):
    src = write_zarr(_daily(8, fill=4.0), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            indicator_fn,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--rule",
            "precip sum 8d >= 25",
            "--detect",
            "first",
            "--probability",
        )
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        run_skill(
            indicator_fn,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out2.zarr"),
            "--rule",
            "precip sum 8d >= 25",
            "--detect",
            "first",
            "--cumulative",
        )
    assert exc.value.code == 2


def test_non_daily_is_refused(tmp_path, indicator_fn):
    ds = make_gridded(n_time=3, lats=(1.0,), lons=(10.0,), fill=4.0)
    ds = ds.assign_coords(
        time=np.array(["2026-01-01", "2026-01-08", "2026-01-15"], dtype="datetime64[ns]")
    )
    src = write_zarr(ds, tmp_path / "weekly.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            indicator_fn,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--rule",
            "precip sum 1d >= 1",
        )
    assert exc.value.code == 2


def test_missing_variable(tmp_path, indicator_fn):
    src = write_zarr(_daily(8, fill=4.0, name="tp"), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            indicator_fn,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--rule",
            "precip sum 8d >= 25",
        )
    assert exc.value.code == 2
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "ok.zarr"),
        "--rule",
        "precip sum 8d >= 25",
        "-v",
        "tp",
    )
    assert _cell(tmp_path / "ok.zarr").values[0] == pytest.approx(1)


def test_step_forecast_axis(tmp_path, indicator_fn):
    ds = make_forecast(n_step=10, lats=(1.0,), lons=(10.0,), name="precip", fill=4.0)
    ds["precip"].attrs[DATA_INTERVAL_ATTR] = "1 day"
    src = write_zarr(ds, tmp_path / "fc.zarr")
    run_skill(
        indicator_fn,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "out.zarr"),
        "--rule",
        "precip sum 8d >= 25",
    )
    out = xr.open_zarr(tmp_path / "out.zarr", consolidated=True)
    assert "step" in out.dims
    assert out["indicator"].isel(step=0, latitude=0, longitude=0).values == pytest.approx(1)
