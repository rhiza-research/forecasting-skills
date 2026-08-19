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


def test_aggregate_requires_period_or_window(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=7), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
