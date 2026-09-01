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
    assert np.datetime_as_string(xvals[0], unit="D") == "2026-01-01"
    assert np.datetime_as_string(xvals[-1], unit="D") == "2026-01-03"


def test_panel_title_lead_zero_is_first_24h(plot_fn):
    plot_mod = load_skill("plot", "plot")
    da = make_forecast(init="2025-01-01", n_step=3)["tp"]
    da.attrs["data_interval"] = "1 day"
    title = plot_mod._panel_title(da, "step", da["step"].values[0], da["step"].values)
    assert title.startswith("2025-01-01")
    assert "until 2025-01-02" in title


def test_timeseries_forecast_writes_png(tmp_path, plot_fn):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "ts.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "timeseries")
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_precip_default_colormap_is_kenya_palette():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "wgbrp"
    assert cmap.N == 10
    assert cmap(0.0)[:3] == pytest.approx((1.0, 1.0, 1.0), abs=0.02)
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_BOUNDS)

    rate = make_gridded()["precip"]
    cmap_rate, norm_rate = plot_mod._heatmap_scale(rate, None)
    assert isinstance(cmap_rate, ListedColormap)
    assert cmap_rate.name == "wgbrp"
    assert isinstance(norm_rate, BoundaryNorm)


def test_non_precip_default_colormap_is_viridis():
    plot_mod = load_skill("plot", "plot")
    da = make_gridded(name="t2m")["t2m"]
    da.attrs.update(units="degree_Celsius", standard_name="air_temperature")
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert cmap == "viridis"
    assert norm is None


def test_explicit_colormap_overrides_precip_default():
    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    cmap, norm = plot_mod._heatmap_scale(da, "magma")
    assert cmap == "magma"
    assert norm is None


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

    da.attrs["long_name"] = "Total precipitation"
    da.attrs["GRIB_name"] = "Precipitation rate"
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


def test_flag_field_heatmap_writes_png(tmp_path, plot_fn):
    ds = make_gridded(n_time=1, fill=0.0, name="event_hit")
    ds["event_hit"].values[0, 0, 0] = 1
    ds["event_hit"].values[0, 0, 1] = -1
    ds["event_hit"].attrs.update(
        units="1",
        long_name="Event verification",
        flag_values=np.array([-1, 0, 1], dtype=np.int8),
        flag_meanings="disagree below hit",
    )
    ds["event_hit"].attrs.pop("standard_name", None)
    src = write_zarr(ds, tmp_path / "hits.zarr")
    out = tmp_path / "hits.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out))
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_panel_shape_default_caps_columns_at_four():
    plot_mod = load_skill("plot", "plot")
    assert plot_mod._panel_shape(1) == (1, 1)
    assert plot_mod._panel_shape(3) == (1, 3)
    assert plot_mod._panel_shape(4) == (1, 4)
    assert plot_mod._panel_shape(5) == (2, 4)
    assert plot_mod._panel_shape(8) == (2, 4)


def test_panel_shape_rows_and_columns_must_match_data():
    from weather_skills_core import UsageError

    plot_mod = load_skill("plot", "plot")
    assert plot_mod._panel_shape(6, rows=2, columns=3) == (2, 3)
    assert plot_mod._panel_shape(6, columns=3) == (2, 3)
    assert plot_mod._panel_shape(6, rows=2) == (2, 3)
    with pytest.raises(UsageError, match="must match"):
        plot_mod._panel_shape(6, rows=2, columns=4)
    with pytest.raises(UsageError, match="does not divide"):
        plot_mod._panel_shape(5, columns=3)
    with pytest.raises(UsageError, match="does not divide"):
        plot_mod._panel_shape(5, rows=2)
    with pytest.raises(UsageError, match="positive integer"):
        plot_mod._panel_shape(3, rows=0)


def test_heatmap_rows_columns_writes_png(tmp_path, plot_fn):
    src = write_zarr(make_forecast(n_step=6), tmp_path / "in.zarr")
    out = tmp_path / "grid.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--rows", "2", "--columns", "3")
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_heatmap_rows_columns_mismatch_exits(tmp_path, plot_fn, capsys):
    src = write_zarr(make_forecast(n_step=3), tmp_path / "in.zarr")
    out = tmp_path / "bad.png"
    with pytest.raises(SystemExit):
        run_skill(
            plot_fn,
            "-i",
            str(src),
            "-o",
            str(out),
            "--rows",
            "2",
            "--columns",
            "3",
        )
    assert "must match" in capsys.readouterr().err


def _make_wind(
    u=0.0,
    v=-5.0,
    name_u="u10",
    name_v="v10",
    *,
    forecast=False,
    **kwargs,
):
    """Gridded eastward/northward pair. Default is a uniform northerly wind."""
    factory = make_forecast if forecast else make_gridded
    ds = factory(name=name_u, fill=float(u), **kwargs)
    ds[name_u].attrs.clear()
    ds[name_u].attrs.update(units="m s-1", standard_name="eastward_wind")
    ds[name_v] = ds[name_u].copy(deep=True)
    ds[name_v].values[:] = float(v)
    ds[name_v].attrs.update(units="m s-1", standard_name="northward_wind")
    return ds


def test_uv_to_speed_fromdir_cardinals():
    plot_mod = load_skill("plot", "plot")
    speed, fromdir = plot_mod._uv_to_speed_fromdir(
        [0.0, -5.0, 0.0, 5.0],
        [-5.0, 0.0, 5.0, 0.0],
    )
    assert speed == pytest.approx([5.0, 5.0, 5.0, 5.0])
    assert fromdir == pytest.approx([0.0, 90.0, 180.0, 270.0])


def test_wind_rose_hist_north_is_sector_zero():
    plot_mod = load_skill("plot", "plot")
    speed = np.full(20, 5.0)
    direction = np.zeros(20)
    edges = np.array([0.0, 2.0, 4.0, 6.0, np.inf])
    hist = plot_mod._wind_rose_hist(speed, direction, edges)
    assert hist.shape == (16, 4)
    assert hist[0].sum() == 20
    assert hist[1:].sum() == 0
    assert hist[0, 2] == 20  # 4–6 m/s bin


def test_resolve_uv_from_standard_names():
    plot_mod = load_skill("plot", "plot")
    ds = _make_wind(name_u="eastward_component", name_v="northward_component")
    assert plot_mod._resolve_uv(ds, None, None) == (
        "eastward_component",
        "northward_component",
    )


def test_resolve_uv_from_u10_v10_names():
    plot_mod = load_skill("plot", "plot")
    ds = _make_wind()
    ds["u10"].attrs.pop("standard_name")
    ds["v10"].attrs.pop("standard_name")
    assert plot_mod._resolve_uv(ds, None, None) == ("u10", "v10")


def test_resolve_uv_explicit_infers_partner():
    plot_mod = load_skill("plot", "plot")
    ds = _make_wind()
    assert plot_mod._resolve_uv(ds, "u10", None) == ("u10", "v10")
    assert plot_mod._resolve_uv(ds, None, "v10") == ("u10", "v10")


def test_resolve_uv_missing_pair_errors():
    from weather_skills_core import UsageError

    plot_mod = load_skill("plot", "plot")
    ds = make_gridded()
    with pytest.raises(UsageError, match="eastward"):
        plot_mod._resolve_uv(ds, None, None)


def test_windrose_writes_png(tmp_path, plot_fn):
    src = write_zarr(_make_wind(), tmp_path / "wind.zarr")
    out = tmp_path / "rose.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "windrose")
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_windrose_stamps_history(tmp_path, plot_fn):
    src = write_zarr(_make_wind(), tmp_path / "wind.zarr")
    out = tmp_path / "rose.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "windrose",
        "--title",
        "10 m wind",
    )
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot"
    assert history[-1]["args"]["style"] == "windrose"
    assert history[-1]["args"]["title"] == "10 m wind"


def test_windrose_forecast_and_explicit_vars(tmp_path, plot_fn):
    src = write_zarr(_make_wind(forecast=True, members=2), tmp_path / "fc.zarr")
    out = tmp_path / "rose.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "windrose",
        "--u-variable",
        "u10",
        "--v-variable",
        "v10",
        "--index",
        "step=0",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_windrose_bbox_writes_png(tmp_path, plot_fn):
    ds = _make_wind(lats=(-5.0, 0.0, 5.0), lons=(30.0, 35.0, 40.0, 45.0))
    src = write_zarr(ds, tmp_path / "wind.zarr")
    out = tmp_path / "rose.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "windrose",
        "--bbox",
        "3/32/-3/42",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_windrose_missing_uv_exits(tmp_path, plot_fn, capsys):
    src = write_zarr(make_gridded(), tmp_path / "precip.zarr")
    out = tmp_path / "rose.png"
    with pytest.raises(SystemExit):
        run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "windrose")
    assert "eastward" in capsys.readouterr().err


def test_heatmap_ignores_uv_flags(tmp_path, plot_fn, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "map.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--u-variable",
        "u10",
    )
    err = capsys.readouterr().err
    assert "only used with --style windrose or --style quiver" in err
    assert Path(out).exists()


def test_wind_speed_da_is_hypot():
    plot_mod = load_skill("plot", "plot")
    ds = _make_wind(u=3.0, v=4.0)
    speed = plot_mod._wind_speed_da(ds["u10"], ds["v10"])
    assert float(speed.mean()) == pytest.approx(5.0)
    assert speed.attrs["long_name"] == "Wind speed"


def test_quiver_writes_png(tmp_path, plot_fn):
    src = write_zarr(_make_wind(), tmp_path / "wind.zarr")
    out = tmp_path / "quiver.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "quiver")
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_quiver_stamps_history(tmp_path, plot_fn):
    src = write_zarr(_make_wind(), tmp_path / "wind.zarr")
    out = tmp_path / "quiver.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "quiver",
        "--title",
        "10 m wind",
    )
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot"
    assert history[-1]["args"]["style"] == "quiver"
    assert history[-1]["args"]["title"] == "10 m wind"


def test_quiver_forecast_panels(tmp_path, plot_fn):
    src = write_zarr(_make_wind(forecast=True, members=2, u=3.0, v=-4.0), tmp_path / "fc.zarr")
    out = tmp_path / "quiver.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "quiver",
        "--u-variable",
        "u10",
        "--v-variable",
        "v10",
        "--quiver-scale",
        "40",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_quiver_missing_uv_exits(tmp_path, plot_fn, capsys):
    src = write_zarr(make_gridded(), tmp_path / "precip.zarr")
    out = tmp_path / "quiver.png"
    with pytest.raises(SystemExit):
        run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "quiver")
    assert "eastward" in capsys.readouterr().err


def test_quiver_step_s2s_grid_is_one():
    plot_mod = load_skill("plot", "plot")
    lat = np.arange(-20.0, 20.0, 1.5)
    lon = np.arange(45.0, 120.0, 1.5)
    assert plot_mod._quiver_step(lat, lon) == 1
    assert plot_mod._quiver_step(lat, lon, requested=3) == 3


def test_quiver_step_auto_thins_quarter_degree():
    plot_mod = load_skill("plot", "plot")
    lat = np.arange(-10.0, 10.0, 0.25)
    lon = np.arange(40.0, 80.0, 0.25)
    assert plot_mod._quiver_step(lat, lon) == 6


def test_quiver_step_rejects_zero():
    from weather_skills_core import UsageError

    plot_mod = load_skill("plot", "plot")
    with pytest.raises(UsageError, match=">= 1"):
        plot_mod._quiver_step([0.0, 1.0], [10.0, 11.0], requested=0)


def test_subsample_quiver_stride():
    plot_mod = load_skill("plot", "plot")
    lon = np.array([0.0, 1.0, 2.0, 3.0])
    lat = np.array([10.0, 11.0, 12.0])
    u = np.arange(12.0).reshape(3, 4)
    v = -u
    lon_q, lat_q, u_q, v_q = plot_mod._subsample_quiver(lon, lat, u, v, 2)
    assert u_q.shape == (2, 2)
    assert lon_q.shape == (2, 2)
    np.testing.assert_array_equal(u_q, u[::2, ::2])
    np.testing.assert_array_equal(v_q, v[::2, ::2])


def test_quiver_step_flag_writes_png(tmp_path, plot_fn):
    src = write_zarr(_make_wind(), tmp_path / "wind.zarr")
    out = tmp_path / "quiver.png"
    run_skill(
        plot_fn,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "quiver",
        "--quiver-step",
        "1",
        "--quiver-scale",
        "100",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
