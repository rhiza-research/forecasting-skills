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


def test_precip_default_colormap_is_kenya_palette():
    from matplotlib.colors import LinearSegmentedColormap

    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    cmap = plot_mod._heatmap_cmap(da, None)
    assert isinstance(cmap, LinearSegmentedColormap)
    assert cmap.name == "wgbrp"
    assert cmap(0.0)[:3] == pytest.approx((1.0, 1.0, 1.0), abs=0.02)

    rate = make_gridded()["precip"]
    cmap_rate = plot_mod._heatmap_cmap(rate, None)
    assert isinstance(cmap_rate, LinearSegmentedColormap)
    assert cmap_rate.name == "wgbrp"


def test_non_precip_default_colormap_is_viridis():
    plot_mod = load_skill("plot", "plot")
    da = make_gridded(name="t2m")["t2m"]
    da.attrs.update(units="degree_Celsius", standard_name="air_temperature")
    assert plot_mod._heatmap_cmap(da, None) == "viridis"


def test_explicit_colormap_overrides_precip_default():
    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    assert plot_mod._heatmap_cmap(da, "magma") == "magma"


def test_amount_colorbar_drops_leftover_rate_name():
    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        long_name="precipitation rate",
        GRIB_name="Precipitation rate",
    )
    assert plot_mod._variable_label(da) == "Total precipitation [mm]"

    rate = make_gridded()["precip"]
    rate.attrs["long_name"] = "precipitation rate"
    assert plot_mod._variable_label(rate) == "precipitation rate [mm day-1]"


def test_plot_converts_aggregated_precip_rate_to_totals():
    plot_mod = load_skill("plot", "plot")
    ds = make_gridded()
    ds["precip"].attrs["aggregation_period"] = "1 day"
    out = plot_mod.precip_for_display(ds, "precip")
    assert out["precip"].attrs["units"] == "mm"
    assert "Total precipitation" in plot_mod._variable_label(out["precip"])

def test_parse_draw_boxes():
    from weather_skills_core import UsageError

    plot_mod = load_skill("plot", "plot")
    boxes = plot_mod._parse_draw_boxes(["10/50/-10/70", "0/90/-10/110"])
    assert boxes == [(10.0, 50.0, -10.0, 70.0), (0.0, 90.0, -10.0, 110.0)]
    assert plot_mod._parse_draw_boxes(None) == []
    with pytest.raises(UsageError):
        plot_mod._parse_draw_boxes(["not-a-box"])


def test_heatmap_draw_box_writes_png(tmp_path, plot_fn):
    # Wider lon range so IOD-style boxes are on-map.
    ds = make_gridded(lats=(-15.0, 0.0, 15.0), lons=(40.0, 70.0, 100.0, 120.0))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "boxes.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--draw-box",
        "10/50/-10/70",
        "--draw-box",
        "0/90/-10/110",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
