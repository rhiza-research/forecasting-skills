"""Correctness tests for plot-timeseries."""

from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def plot_timeseries():
    return load_skill("plot-timeseries", "plot_timeseries").plot_timeseries


def test_single_input_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_reduce_spatial_dims(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--title",
        "Area mean",
    )

    assert Path(out).exists()


def test_forecast_step_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "fc.zarr")
    out = tmp_path / "ts.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_two_inputs_write_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "ts.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_repeated_dash_i_keeps_every_input(tmp_path, plot_timeseries):
    # First file has no precip. If a second -i overwrote the first (nargs="+"),
    # only b.zarr would be plotted and this would succeed.
    a = write_zarr(make_gridded(name="other"), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(), tmp_path / "b.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(a),
            "-i",
            str(b),
            "-o",
            str(tmp_path / "ts.png"),
            "-v",
            "precip",
            "--reduce",
            "latitude",
            "--reduce",
            "longitude",
        )
    assert exc.value.code == 2


def test_y_label_shows_units_from_pint():
    mod = load_skill("plot-timeseries", "plot_timeseries")
    da = make_gridded()["precip"]
    assert mod._y_label("precip", da) == "precip [mm/day]"
    assert mod._y_label("precip", da.pint.quantify()) == "precip [mm/day]"
