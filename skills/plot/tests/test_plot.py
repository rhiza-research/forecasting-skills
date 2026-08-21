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
    assert plot_mod._variable_label(rate) == "precipitation rate [mm/day]"

    quantified = rate.pint.quantify()
    assert plot_mod._variable_label(quantified) == "precipitation rate [mm/day]"


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


def test_boundary_layers_country_scale_includes_admin1():
    plot_mod = load_skill("plot", "plot")
    # Kenya-sized view (~8° × 10°)
    spec = plot_mod._boundary_layers((33.9, 41.9, -4.7, 5.0))
    assert spec == {"scale": "10m", "admin1": True}


def test_boundary_layers_continental_excludes_admin1():
    plot_mod = load_skill("plot", "plot")
    # Africa-sized view
    spec = plot_mod._boundary_layers((-17.5, 51.5, -35.0, 37.5))
    assert spec == {"scale": "50m", "admin1": False}


def test_boundary_layers_global_is_coarse():
    plot_mod = load_skill("plot", "plot")
    spec = plot_mod._boundary_layers((-180.0, 180.0, -90.0, 90.0))
    assert spec == {"scale": "110m", "admin1": False}


def test_extent_clip_geom_splits_unwrapped_antimeridian():
    plot_mod = load_skill("plot", "plot")
    clip = plot_mod._extent_clip_geom((170.0, 190.0, -10.0, 10.0))
    assert clip.intersects(plot_mod._extent_clip_geom((175.0, 179.0, -1.0, 1.0)))
    # The +190 unwrapped piece lives at lon -170 in Natural Earth coords.
    west = plot_mod._extent_clip_geom((-172.0, -168.0, -1.0, 1.0))
    assert clip.intersects(west)


def test_load_geo_overlays_skips_on_download_failure(monkeypatch, capsys):
    plot_mod = load_skill("plot", "plot")
    import cartopy.io.shapereader as shpreader

    def _boom(**_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(shpreader, "natural_earth", _boom)
    overlays = plot_mod._load_geo_overlays((33.9, 41.9, -4.7, 5.0))
    assert overlays == []
    err = capsys.readouterr().err
    assert "overlay unavailable" in err


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
