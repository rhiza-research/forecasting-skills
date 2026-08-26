"""Correctness tests for plot-compare-forecasts."""

from pathlib import Path

import numpy as np
import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core import DataError
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mod():
    return load_skill("plot-compare-forecasts", "plot_compare_forecasts")


@pytest.fixture(scope="module")
def plot_fn(plot_mod):
    return plot_mod.plot_compare_forecasts


def test_two_forecasts_write_png_and_stamp_history(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"

    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--title",
        "Two forecasts",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot-compare-forecasts"
    assert history[-1]["args"]["title"] == "Two forecasts"


def test_shorter_horizon_blank_does_not_crash(tmp_path, plot_fn, plot_mod):
    long = make_forecast(n_step=3, fill=1.0)
    short = make_forecast(n_step=2, fill=2.0)
    columns, matches, _w, _steps, _dims = plot_mod.align_valid_times([long, short])
    assert len(columns) == 3
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    a = write_zarr(long, tmp_path / "long.zarr")
    b = write_zarr(short, tmp_path / "short.zarr")
    out = tmp_path / "grid.png"
    run_skill(plot_fn, "-i", str(a), "-i", str(b), "-o", str(out))
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_different_inits_are_different_valid_time_columns(plot_mod):
    a = make_forecast(n_step=3, init="2026-01-01")
    b = make_forecast(n_step=3, init="2026-01-02")
    columns, matches, _w, _steps, _dims = plot_mod.align_valid_times([a, b])
    assert len(columns) == 4
    dates = [
        np.datetime_as_string(np.asarray(t).astype("datetime64[D]"), unit="D") for t in columns
    ]
    assert dates == ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    assert matches[0] == [0, 1, 2, None]
    assert matches[1] == [None, 0, 1, 2]


def test_resolution_mismatch_errors(plot_mod):
    daily = make_forecast(n_step=3)
    weekly = make_forecast(n_step=3).assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (7, 14, 21)]))
    )
    with pytest.raises(DataError, match="different time resolutions"):
        plot_mod.align_valid_times([daily, weekly])


def test_bbox_slices_before_draw(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--bbox",
        "3/9/0/12",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_two_obs_time_cubes_write_png(tmp_path, plot_fn, plot_mod):
    a = make_gridded(n_time=3, fill=1.0, start="2026-01-01")
    b = make_gridded(n_time=2, fill=2.0, start="2026-01-01")
    columns, matches, _w, _steps, dims = plot_mod.align_valid_times([a, b])
    assert dims == ["time", "time"]
    assert len(columns) == 3
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(write_zarr(a, tmp_path / "a.zarr")),
        "-i",
        str(write_zarr(b, tmp_path / "b.zarr")),
        "-o",
        str(out),
        "--variable",
        "precip",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_forecast_and_obs_share_valid_times(tmp_path, plot_fn, plot_mod):
    fc = make_forecast(n_step=3, fill=1.0, init="2026-01-01", name="precip")
    obs = make_gridded(n_time=2, fill=2.0, start="2026-01-02")
    columns, matches, _w, _steps, dims = plot_mod.align_valid_times([fc, obs])
    assert dims == ["step", "time"]
    dates = [
        np.datetime_as_string(np.asarray(t).astype("datetime64[D]"), unit="D") for t in columns
    ]
    assert dates == ["2026-01-02", "2026-01-03", "2026-01-04"]
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(write_zarr(fc, tmp_path / "fc.zarr")),
        "-i",
        str(write_zarr(obs, tmp_path / "obs.zarr")),
        "-o",
        str(out),
        "--variable",
        "precip",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_precip_default_colormap_is_discrete_kenya_palette(plot_mod):
    from matplotlib.colors import BoundaryNorm, ListedColormap

    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    cmap, norm = plot_mod._heatmap_scale(da, None)
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "wgbrp"
    assert cmap.N == 10
    assert cmap(0.0)[:3] == pytest.approx((1.0, 1.0, 1.0), abs=0.02)
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_BOUNDS)

    t2m = make_gridded(name="t2m")["t2m"]
    t2m.attrs.update(units="degree_Celsius", standard_name="air_temperature")
    cmap_t, norm_t = plot_mod._heatmap_scale(t2m, None)
    assert cmap_t == "viridis"
    assert norm_t is None
