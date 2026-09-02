"""Correctness tests for plot."""

from pathlib import Path

import numpy as np
import pytest
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr

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


def test_fontsize_writes_png(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "map.png"

    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--fontsize", "22", "--title", "Large")

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_contour_levels_span_and_pad_constant():
    plot_mod = load_skill("plot", "plot")
    levels = plot_mod._contour_levels(0.0, 10.0, n=10)
    assert levels[0] == 0.0
    assert levels[-1] == 10.0
    assert len(levels) == 11
    constant = plot_mod._contour_levels(5.0, 5.0, n=10)
    assert constant[0] < 5.0 < constant[-1]
    assert len(constant) == 11


def test_contour_writes_png(tmp_path, plot_fn):
    ds = make_gridded()
    ds["precip"] = ds["precip"] + ds["latitude"] + 0.01 * ds["longitude"]
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "contour.png"

    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "contour")

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_contour_stamps_history(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "contour.png"

    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "contour", "--title", "Isolines")

    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot"
    assert history[-1]["args"]["style"] == "contour"
    assert history[-1]["args"]["title"] == "Isolines"


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
    assert xlabel == "Valid time"
    assert np.datetime_as_string(xvals[0], unit="D") == "2026-01-01"
    assert np.datetime_as_string(xvals[-1], unit="D") == "2026-01-03"


def test_axis_label_capitalizes():
    plot_mod = load_skill("plot", "plot")
    assert plot_mod._axis_label("lon") == "Longitude"
    assert plot_mod._axis_label("valid time") == "Valid time"
    assert plot_mod._axis_label("total precipitation [mm]") == "Total precipitation [mm]"
    assert plot_mod._axis_label("Latitude") == "Latitude"


def test_resolve_axis_label_override_is_verbatim():
    plot_mod = load_skill("plot", "plot")
    assert plot_mod._resolve_axis_label("lon (E)", "Longitude") == "lon (E)"
    assert plot_mod._resolve_axis_label(None, "lon") == "Longitude"
    assert plot_mod._resolve_axis_label("", "Latitude") == "Latitude"


def test_heatmap_axis_label_overrides(tmp_path, plot_fn):
    import matplotlib

    matplotlib.use("Agg")
    plot_mod = load_skill("plot", "plot")
    da = make_gridded(n_time=1)["precip"]
    fig = plot_mod._heatmap(
        da,
        "latitude",
        "longitude",
        "viridis",
        extent=(10.0, 11.0, 1.0, 2.0),
        cities={},
        title=None,
        fontsize=14,
        wrap_lon=True,
        xlabel="Eastings",
        ylabel="Northings",
    )
    axes = [ax for ax in fig.axes if hasattr(ax, "get_xlabel") and ax.get_visible()]
    assert any(ax.get_xlabel() == "Eastings" for ax in axes)
    assert any(ax.get_ylabel() == "Northings" for ax in axes)
    import matplotlib.pyplot as plt

    plt.close(fig)


def test_panel_title_lead_zero_is_first_24h(plot_fn):
    plot_mod = load_skill("plot", "plot")
    da = make_forecast(init="2025-01-01", n_step=3)["tp"]
    da.attrs["data_interval"] = "1 day"
    title = plot_mod._panel_title(da, "step", da["step"].values[0], da["step"].values)
    assert title.startswith("2025-01-01")
    assert "until 2025-01-02" in title


def test_panel_title_calendar_weekly_range(plot_fn):
    import numpy as np
    import xarray as xr

    plot_mod = load_skill("plot", "plot")
    times = np.arange("2026-08-04", "2026-09-01", dtype="datetime64[D]")[::7]
    da = xr.DataArray(
        np.zeros((len(times), 2, 2)),
        dims=("time", "latitude", "longitude"),
        coords={
            "time": times,
            "latitude": [0.0, 1.0],
            "longitude": [36.0, 37.0],
        },
        name="precip",
    )
    da.attrs["aggregation_period"] = "7 day"
    title = plot_mod._panel_title(da, "time", times[0], times)
    assert title == "2026-08-04 to 2026-08-10"


def test_panel_title_calendar_daily_is_single_date(plot_fn):
    import numpy as np
    import xarray as xr

    plot_mod = load_skill("plot", "plot")
    times = np.arange("2026-08-04", "2026-08-08", dtype="datetime64[D]")
    da = xr.DataArray(
        np.zeros((len(times), 2, 2)),
        dims=("time", "latitude", "longitude"),
        coords={
            "time": times,
            "latitude": [0.0, 1.0],
            "longitude": [36.0, 37.0],
        },
        name="precip",
    )
    da.attrs["aggregation_period"] = "1 day"
    title = plot_mod._panel_title(da, "time", times[0], times)
    assert title == "2026-08-04"
    assert "time=" not in title


def test_timeseries_forecast_writes_png(tmp_path, plot_fn):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "ts.png"
    run_skill(plot_fn, "-i", str(src), "-o", str(out), "--style", "timeseries")
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_precip_default_colormap_is_chirps_total_palette():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    plot_mod = load_skill("plot", "plot")
    da = make_forecast()["tp"]
    da.attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        aggregation_period="10 day",
    )
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "chirps_total"
    assert cmap.N == 14
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_BOUNDS)

    rate = make_gridded()["precip"]
    rate.attrs["aggregation_period"] = "7 day"
    cmap_rate, norm_rate = plot_mod._heatmap_scale(rate, None)
    assert isinstance(cmap_rate, ListedColormap)
    assert cmap_rate.name == "chirps_total"
    assert isinstance(norm_rate, BoundaryNorm)


def test_precip_short_period_colormap_uses_subpentad_bounds():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    plot_mod = load_skill("plot", "plot")
    da = make_gridded(fill=3.0)["precip"]
    da.attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        aggregation_period="1 day",
    )
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "chirps_short"
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_SHORT_BOUNDS)


def test_precip_anomaly_colormap_is_chirps_palette():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    plot_mod = load_skill("plot", "plot")
    da = make_gridded(fill=-25.0)["precip"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "chirps_anom"
    assert cmap.N == 13
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_ANOMALY_BOUNDS)

    named = make_gridded(fill=12.0)["precip"]
    named.attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        long_name="rainfall anomaly",
    )
    cmap_named, norm_named = plot_mod._heatmap_scale(named, None)
    assert cmap_named.name == "chirps_anom"
    assert isinstance(norm_named, BoundaryNorm)


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


def test_boundary_layers_regional_excludes_admin1():
    plot_mod = load_skill("plot", "plot")
    # East Africa-sized view (~30°) is multi-country, not country-scale
    spec = plot_mod._boundary_layers((22.0, 52.0, -12.0, 18.0))
    assert spec == {"scale": "10m", "admin1": False}


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


def test_pad_cell_extent_wrapped_global_is_full_globe():
    # GFS-like 0–360 wrap → [-180, 180−Δ]. Half-cell padding is a 360° span
    # whose endpoints are the same meridian; Cartopy then draws a ~9° sliver.
    plot_mod = load_skill("plot", "plot")
    lon = np.arange(0.0, 360.0, 10.0)
    lat = np.arange(-20.0, 21.0, 10.0)
    wrapped = np.sort((lon + 180.0) % 360.0 - 180.0)
    ext = plot_mod._pad_cell_extent(lat, wrapped)
    assert ext[0] == -180.0
    assert ext[1] == 180.0
    assert ext[3] - ext[2] == pytest.approx(50.0)


def test_pad_cell_extent_indian_ocean_keeps_basin():
    plot_mod = load_skill("plot", "plot")
    lon = np.arange(40.0, 121.0, 10.0)
    lat = np.arange(-20.0, 21.0, 10.0)
    ext = plot_mod._pad_cell_extent(lat, lon)
    assert ext[0] == pytest.approx(35.0)
    assert ext[1] == pytest.approx(125.0)
    assert ext[2] == pytest.approx(-25.0)
    assert ext[3] == pytest.approx(25.0)


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


def test_auto_quiver_scale_uses_requested():
    plot_mod = load_skill("plot", "plot")
    assert plot_mod._auto_quiver_scale([10.0], [0.0], 60.0, 1.5, requested=100) == 100.0


def test_auto_quiver_scale_rejects_nonpositive():
    from weather_skills_core import UsageError

    plot_mod = load_skill("plot", "plot")
    with pytest.raises(UsageError, match="> 0"):
        plot_mod._auto_quiver_scale([10.0], [0.0], 60.0, 1.5, requested=0)


def test_auto_quiver_scale_fits_typical_wind_to_spacing():
    plot_mod = load_skill("plot", "plot")
    u = np.full((8, 8), 10.0)
    v = np.zeros((8, 8))
    scale = plot_mod._auto_quiver_scale(u, v, 60.0, 1.5)
    assert scale == pytest.approx(10.0 * 60.0 / (plot_mod.QUIVER_ARROW_LEN_SPACING * 1.5))


def test_auto_quiver_scale_grows_with_map_width():
    plot_mod = load_skill("plot", "plot")
    u = np.ones((4, 4))
    v = np.zeros((4, 4))
    narrow = plot_mod._auto_quiver_scale(u, v, 10.0, 1.5)
    wide = plot_mod._auto_quiver_scale(u, v, 80.0, 1.5)
    assert wide > narrow


def test_auto_quiver_scale_full_wind_exceeds_s2s_anomaly_default():
    """10 m/s on a 60° basin needs a larger matplotlib scale than S2S's 100."""
    plot_mod = load_skill("plot", "plot")
    u = np.full((6, 6), 10.0)
    v = np.zeros((6, 6))
    assert plot_mod._auto_quiver_scale(u, v, 60.0, 1.5) > plot_mod.QUIVER_SCALE


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


def _write_box_geojson(path, lon_min=9.5, lon_max=13.5, lat_min=0.5, lat_max=3.5):
    import json

    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [lon_min, lat_min],
                                    [lon_max, lat_min],
                                    [lon_max, lat_max],
                                    [lon_min, lat_max],
                                    [lon_min, lat_min],
                                ]
                            ],
                        },
                    }
                ],
            }
        )
    )
    return path


def test_parse_layer_kind_path_and_options():
    plot_mod = load_skill("plot", "plot")
    spec = plot_mod.parse_layer("heatmap:/tmp/a.zarr")
    assert spec.kind == "heatmap"
    assert spec.path.as_posix() == "/tmp/a.zarr"
    assert spec.options == {}
    spec = plot_mod.parse_layer(
        "scatter:/tmp/b.zarr::variable=precip,index=step=0,1,2,colormap=magma"
    )
    assert spec.kind == "scatter"
    assert spec.options["variable"] == "precip"
    assert spec.options["index"] == "step=0,1,2"
    assert spec.options["colormap"] == "magma"
    spec = plot_mod.parse_layer("outline:/tmp/kenya.geojson")
    assert spec.kind == "outline"
    assert spec.zarr_paths() == []
    spec = plot_mod.parse_layer("heatmap:/tmp/a.zarr")
    assert spec.zarr_paths()[0].as_posix() == "/tmp/a.zarr"


def test_parse_layer_rejects_unknown_kind():
    import argparse

    plot_mod = load_skill("plot", "plot")
    with pytest.raises(argparse.ArgumentTypeError, match="unknown --layer kind"):
        plot_mod.parse_layer("contour:/tmp/a.zarr")


def test_layer_heatmap_writes_png(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "map.png"
    run_skill(plot_fn, "--layer", f"heatmap:{src}", "-o", str(out))
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]["skill"] == "plot"
    assert history[-1]["input"]["basename"] == "in.zarr"


def test_layer_heatmap_and_scatter_overlay(tmp_path, plot_fn):
    grid = write_zarr(make_gridded(), tmp_path / "grid.zarr")
    pts = write_zarr(make_point_obs(n_time=2), tmp_path / "pts.zarr")
    out = tmp_path / "overlay.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{grid}",
        "--layer",
        f"scatter:{pts}",
        "-o",
        str(out),
        "--title",
        "grid vs stations",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    inp = history[-1]["input"]
    assert isinstance(inp, list)
    names = {item["basename"] for item in inp}
    assert names == {"grid.zarr", "pts.zarr"}


def test_layer_heatmap_and_outline(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    geo = _write_box_geojson(tmp_path / "box.geojson")
    out = tmp_path / "outline.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{src}",
        "--layer",
        f"outline:{geo}",
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_layer_heatmap_scatter_outline(tmp_path, plot_fn):
    grid = write_zarr(make_gridded(), tmp_path / "grid.zarr")
    pts = write_zarr(make_point_obs(n_time=2), tmp_path / "pts.zarr")
    geo = _write_box_geojson(tmp_path / "box.geojson")
    out = tmp_path / "all.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{grid}",
        "--layer",
        f"scatter:{pts}",
        "--layer",
        f"outline:{geo}",
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_layer_forecast_panels_with_outline(tmp_path, plot_fn):
    src = write_zarr(make_forecast(n_step=3), tmp_path / "fc.zarr")
    geo = _write_box_geojson(tmp_path / "box.geojson")
    out = tmp_path / "leads.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{src}",
        "--layer",
        f"outline:{geo}",
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_layer_forecast_heatmap_and_same_step_quiver(tmp_path, plot_fn):
    precip = write_zarr(make_forecast(n_step=3), tmp_path / "tp.zarr")
    wind = write_zarr(_make_wind(forecast=True, n_step=3), tmp_path / "wind.zarr")
    out = tmp_path / "tp_wind.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{precip}",
        "--layer",
        f"quiver:{wind}",
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_layer_forecast_step_vs_obs_time_errors(tmp_path, plot_fn):
    fc = write_zarr(make_forecast(n_step=3), tmp_path / "fc.zarr")
    obs = write_zarr(make_point_obs(n_time=3), tmp_path / "obs.zarr")
    out = tmp_path / "bad.png"
    with pytest.raises(SystemExit):
        run_skill(
            plot_fn,
            "--layer",
            f"heatmap:{fc}",
            "--layer",
            f"scatter:{obs}",
            "-o",
            str(out),
        )


def test_layer_time_mismatch_no_overlap(tmp_path, plot_fn):
    a = write_zarr(make_gridded(start="2026-01-01"), tmp_path / "a.zarr")
    b = write_zarr(make_point_obs(n_time=2, start="2026-06-01"), tmp_path / "b.zarr")
    out = tmp_path / "bad.png"
    with pytest.raises(SystemExit):
        run_skill(
            plot_fn,
            "--layer",
            f"heatmap:{a}",
            "--layer",
            f"scatter:{b}",
            "-o",
            str(out),
        )


def test_layer_rejects_input_flag(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "bad.png"
    with pytest.raises(SystemExit):
        run_skill(
            plot_fn,
            "-i",
            str(src),
            "--layer",
            f"heatmap:{src}",
            "-o",
            str(out),
        )


def test_layer_rejects_timeseries_style(tmp_path, plot_fn):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "bad.png"
    with pytest.raises(SystemExit):
        run_skill(
            plot_fn,
            "--layer",
            f"heatmap:{src}",
            "--style",
            "timeseries",
            "-o",
            str(out),
        )


def test_layer_independent_scale(tmp_path, plot_fn):
    grid = write_zarr(make_gridded(), tmp_path / "grid.zarr")
    t2m = make_gridded(name="t2m")
    t2m["t2m"].attrs.update(units="degree_Celsius", standard_name="air_temperature")
    other = write_zarr(t2m, tmp_path / "t2m.zarr")
    out = tmp_path / "indep.png"
    run_skill(
        plot_fn,
        "--layer",
        f"heatmap:{grid}::variable=precip",
        "--layer",
        f"heatmap:{other}::variable=t2m",
        "--independent-scale",
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
