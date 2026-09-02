"""Correctness tests for plot-verify."""

from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.errors import UsageError
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mod():
    return load_skill("plot-verify", "plot_verify")


@pytest.fixture(scope="module")
def plot_fn(plot_mod):
    return plot_mod.plot_verify


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


def _week(*, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def _run_verify(verify_fn, fc_path, obs_path, out_path, *, metric="hits", threshold="1"):
    args = ["--forecast", str(fc_path), "--obs", str(obs_path), "-o", str(out_path)]
    if metric != "hits":
        args.extend(["--metric", metric])
    if threshold != "1":
        args.extend(["--threshold", threshold])
    run_skill(verify_fn, *args)


def test_four_leads_write_png_and_stamp_history(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0), (1, 0)]), tmp_path / "obs.zarr")
    forecasts = []
    verify_paths = []
    for k, cells in zip((4, 3, 2, 1), ([], [(1, 0)], [(0, 0), (1, 0)], [(0, 0)]), strict=True):
        fc_path = write_zarr(_week(event_at=cells), tmp_path / f"w{k}.zarr")
        forecasts.append(fc_path)
        vpath = tmp_path / f"v{k}.zarr"
        _run_verify(verify_fn, fc_path, obs, vpath)
        verify_paths.append(vpath)
    out = tmp_path / "verify.png"

    args = ["--obs", str(obs)]
    for fc_path, vpath in zip(forecasts, verify_paths, strict=True):
        args.extend(["--forecast", str(fc_path), "--verify", str(vpath)])
    args.extend(["-o", str(out), "--title", "Obs week"])
    run_skill(plot_fn, *args)

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot-verify"
    printed = capsys.readouterr().out
    weeks = [ln.split()[1] for ln in printed.splitlines() if ln.startswith("Week ")]
    assert weeks == ["4", "3", "2", "1"]
    assert "hit rate" in printed


def test_metric_bias_writes_png(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)], fill=1.0), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)], fill=3.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "bias.zarr"
    _run_verify(verify_fn, fc, obs, vpath, metric="bias")
    out = tmp_path / "verify_bias.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert "bias" in capsys.readouterr().out


def test_error_scale_bias_white_at_zero(plot_mod):
    import numpy as np
    import xarray as xr
    from matplotlib.colors import TwoSlopeNorm

    da = xr.DataArray(np.array([[-2.0, 0.0], [0.5, 3.0]]), name="bias")
    cmap, norm, vmin, vmax = plot_mod._error_scale(da, "bias")
    assert cmap.name == "verify_bias"
    assert isinstance(norm, TwoSlopeNorm)
    assert norm.vcenter == 0.0
    assert vmin is None and vmax is None
    # Midpoint of the colormap is white
    mid = cmap(0.5)[:3]
    assert all(c > 0.95 for c in mid)


def test_error_scale_mae_white_at_zero(plot_mod):
    import numpy as np
    import xarray as xr

    da = xr.DataArray(np.array([[0.0, 1.0], [2.0, 4.0]]), name="mae")
    cmap, norm, vmin, vmax = plot_mod._error_scale(da, "mae")
    assert cmap.name == "verify_mae"
    assert norm is None
    assert vmin == 0.0
    assert vmax == 4.0
    assert all(c > 0.95 for c in cmap(0.0)[:3])


def test_verify_count_mismatch_is_refused(tmp_path, plot_fn):
    obs = write_zarr(_week(event_at=[]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[]), tmp_path / "fc.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_colorbar_figure_expands_for_precip_class_ticks(plot_mod):
    n_ticks = len(plot_mod.PRECIP_BOUNDS)
    one_col = plot_mod._colorbar_figure_width(1, n_ticks)
    four_col = plot_mod._colorbar_figure_width(4, n_ticks)
    assert one_col > 7.0
    assert one_col * plot_mod._FIELD_CBAR_WIDTH >= plot_mod._CBAR_INCHES_PER_TICK * n_ticks
    assert four_col == max(3.6 * 4, one_col)
    assert plot_mod._colorbar_figure_width(1, 0) == 7.0


def test_colorbar_axes_stack_field_above_verify(plot_mod):
    maps_bottom, top, field, verify = plot_mod._colorbar_axes_boxes(title=True)
    assert field[2] == plot_mod._FIELD_CBAR_WIDTH
    assert field[2] > verify[2]
    assert field[1] > verify[1] + verify[3]
    # Room between bars for the field colorbar label.
    assert field[1] - (verify[1] + verify[3]) >= 0.05
    assert maps_bottom > field[1] + field[3]
    assert top == 0.92


def test_row_labels_use_weather_skills_source(plot_mod):
    obs = _week(event_at=[])
    obs.attrs["weather_skills_source"] = "chirps"
    a = _week(event_at=[])
    a.attrs["weather_skills_source"] = "ecmwf-s2s"
    b = _week(event_at=[])
    b.attrs["weather_skills_source"] = "ecmwf-s2s"
    assert plot_mod._row_labels(obs, [a, b], "hits") == ("CHIRPS", "ECMWF S2S", "Hits")
    assert plot_mod._row_labels(obs, [a, b], "bias") == ("CHIRPS", "ECMWF S2S", "Bias")


def test_row_labels_fallback_without_source(plot_mod):
    obs = _week(event_at=[])
    fc = _week(event_at=[])
    assert plot_mod._row_labels(obs, [fc]) == ("Observation", "Forecast", "Hits")


def test_row_labels_explicit_overrides(plot_mod):
    obs = _week(event_at=[])
    a = _week(event_at=[])
    b = _week(event_at=[])
    assert plot_mod._row_labels(
        obs, [a, b], "hits", labels=["Obs custom", "Fc A", "Fc B"]
    ) == ("Obs custom", "Fc A / Fc B", "Hits")


def test_label_count_mismatch_is_refused(plot_mod):
    obs = _week(event_at=[])
    fc = _week(event_at=[])
    with pytest.raises(UsageError, match="expected 2 --label"):
        plot_mod._row_labels(obs, [fc], labels=["Only obs"])


def test_custom_lead_labels(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    a = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "a.zarr")
    b = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "b.zarr")
    va = tmp_path / "va.zarr"
    vb = tmp_path / "vb.zarr"
    _run_verify(verify_fn, a, obs, va)
    _run_verify(verify_fn, b, obs, vb)
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(a),
        "--verify",
        str(va),
        "--forecast",
        str(b),
        "--verify",
        str(vb),
        "--lead",
        "W1",
        "--lead",
        "W2",
        "-o",
        str(out),
    )
    assert "W1" in capsys.readouterr().out
    assert Path(out).exists()


def test_lead_count_mismatch_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[]), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "--verify",
            str(vpath),
            "--lead",
            "W1",
            "--lead",
            "W2",
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_step_forecast_without_time_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_forecast(n_step=2, fill=5.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    with pytest.raises(SystemExit):
        _run_verify(verify_fn, fc, obs, vpath)


def test_obs_finer_grid_is_refused(tmp_path, plot_fn, verify_fn):
    fc = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "fc.zarr",
    )
    obs = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0)),
        tmp_path / "obs.zarr",
    )
    vpath = tmp_path / "v.zarr"
    with pytest.raises(SystemExit):
        _run_verify(verify_fn, fc, obs, vpath)


def test_multi_time_obs_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(make_gridded(n_time=2, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "--verify",
            str(vpath),
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_bbox_slices_before_draw(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(out),
        "--bbox",
        "3/9/0/12",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
