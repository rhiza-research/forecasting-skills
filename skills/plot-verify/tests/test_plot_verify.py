"""Correctness tests for plot-verify."""

from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mod():
    return load_skill("plot-verify", "plot_verify")


@pytest.fixture(scope="module")
def plot_fn(plot_mod):
    return plot_mod.plot_verify


def _week(*, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def test_classify_hit_disagree_below(plot_mod):
    fc = _week(event_at=[(0, 0), (0, 1)])["precip"].isel(time=0)
    obs = _week(event_at=[(0, 0), (1, 0)])["precip"].isel(time=0)
    hits, obs_event = plot_mod.classify(fc, obs, 1.0)
    assert hits.values[0, 0] == pytest.approx(1)
    assert hits.values[0, 1] == pytest.approx(-1)
    assert hits.values[1, 0] == pytest.approx(-1)
    assert hits.values[1, 1] == pytest.approx(0)
    rate, n_hit, n_obs = plot_mod.hit_rate(hits, obs_event)
    assert n_obs == 2
    assert n_hit == 1
    assert rate == pytest.approx(0.5)


def test_hit_rate_is_none_when_obs_has_no_event(plot_mod):
    da = _week(event_at=[], fill=0.0)["precip"].isel(time=0)
    hits, obs_event = plot_mod.classify(da, da, 1.0)
    rate, n_hit, n_obs = plot_mod.hit_rate(hits, obs_event)
    assert rate is None
    assert n_obs == 0
    assert n_hit == 0


def test_four_leads_write_png_and_stamp_history(tmp_path, plot_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0), (1, 0)]), tmp_path / "obs.zarr")
    forecasts = []
    # week-4 first (least recent) through week-1 (most recent)
    for k, cells in zip((4, 3, 2, 1), ([], [(1, 0)], [(0, 0), (1, 0)], [(0, 0)]), strict=True):
        forecasts.append(write_zarr(_week(event_at=cells), tmp_path / f"w{k}.zarr"))
    out = tmp_path / "verify.png"

    args = ["--obs", str(obs)]
    for path in forecasts:
        args.extend(["--forecast", str(path)])
    args.extend(["-o", str(out), "--title", "Obs week", "--threshold", "1"])
    run_skill(plot_fn, *args)

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot-verify"
    assert history[-1]["args"]["title"] == "Obs week"
    printed = capsys.readouterr().out
    weeks = [ln.split()[1] for ln in printed.splitlines() if ln.startswith("Week ")]
    assert weeks == ["4", "3", "2", "1"]
    assert "hit rate" in printed


def test_custom_lead_labels(tmp_path, plot_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    a = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "a.zarr")
    b = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "b.zarr")
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(a),
        "--forecast",
        str(b),
        "--lead",
        "W1",
        "--lead",
        "W2",
        "-o",
        str(out),
    )
    assert "W1" in capsys.readouterr().out
    assert Path(out).exists()


def test_lead_count_mismatch_is_refused(tmp_path, plot_fn):
    obs = write_zarr(_week(event_at=[]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[]), tmp_path / "fc.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "--lead",
            "W1",
            "--lead",
            "W2",
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_step_forecast_without_time_is_refused(tmp_path, plot_fn):
    obs = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_forecast(n_step=2, fill=5.0), tmp_path / "fc.zarr")
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


def test_obs_finer_grid_is_refused(tmp_path, plot_fn):
    fc = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "fc.zarr",
    )
    obs = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0)),
        tmp_path / "obs.zarr",
    )
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


def test_multi_time_obs_is_refused(tmp_path, plot_fn):
    obs = write_zarr(make_gridded(n_time=2, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "fc.zarr")
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


def test_bbox_slices_before_draw(tmp_path, plot_fn):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "fc.zarr")
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "-o",
        str(out),
        "--bbox",
        "3/9/0/12",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
