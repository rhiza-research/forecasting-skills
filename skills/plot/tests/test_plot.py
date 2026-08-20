"""Correctness tests for plot."""

from pathlib import Path

import numpy as np
import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_fn():
    return load_skill("plot", "plot").plot


def test_heatmap_writes_png(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "map.png"

    run_skill(plot_fn, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_heatmap_stamps_history(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "map.png"

    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--title", "Precip")

    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot"
    assert history[-1]["args"]["title"] == "Precip"


def test_timeseries_forecast_axis_is_valid_time(plot_fn):
    plot_mod = load_skill("plot", "plot")
    da = make_forecast(init="2026-01-01")["tp"]
    xvals, xlabel = plot_mod._timeseries_axis(da, "step")
    assert xlabel == "valid time"
    assert np.datetime_as_string(xvals[0], unit="D") == "2026-01-02"
    assert np.datetime_as_string(xvals[-1], unit="D") == "2026-01-04"


def test_timeseries_forecast_writes_png(tmp_path, plot_fn):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "ts.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "timeseries")
    assert Path(out).exists()
    assert out.stat().st_size > 0
