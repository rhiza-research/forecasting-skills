"""Correctness tests for verify."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


def _grid(*, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def test_hit_disagree_and_below(tmp_path, verify_fn):
    fc = write_zarr(_grid(event_at=[(0, 0), (0, 1)]), tmp_path / "fc.zarr")
    obs = write_zarr(_grid(event_at=[(0, 0), (1, 0)]), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"

    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--variable",
        "precip",
        "--threshold",
        "1",
        "-o",
        str(out),
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert Path(out).exists()
    hit = ds["event_hit"].isel(time=0).values
    assert hit[0, 0] == pytest.approx(1)
    assert hit[0, 1] == pytest.approx(-1)
    assert hit[1, 0] == pytest.approx(-1)
    assert hit[1, 1] == pytest.approx(0)
    assert ds["event_hit"].attrs["event_threshold"] == 1.0
    assert ds["event_hit"].attrs["event_variable"] == "precip"
    assert ds.attrs["verify_metric"] == "hits"
    assert "hit rate" in ds.attrs.get("verify_score_summary", "")
    assert load_history(out)[-1]["skill"] == "verify"


def test_threshold_raises_the_bar(tmp_path, verify_fn):
    ds = make_gridded(n_time=1, fill=2.0)
    fc = write_zarr(ds, tmp_path / "fc.zarr")
    obs = write_zarr(ds.copy(), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"

    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--threshold",
        "10",
        "-o",
        str(out),
    )
    hit = xr.open_zarr(out, consolidated=True)["event_hit"].values
    np.testing.assert_array_equal(hit, 0)


def test_different_variable_names(tmp_path, verify_fn):
    fc = write_zarr(make_gridded(n_time=1, fill=5.0, name="tp"), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=5.0, name="precip"), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"
    run_skill(verify_fn, "--forecast", str(fc), "--obs", str(obs), "-o", str(out))
    assert xr.open_zarr(out, consolidated=True)["event_hit"].values == pytest.approx(1)


def test_bias_writes_field(tmp_path, verify_fn, capsys):
    fc = write_zarr(make_gridded(n_time=1, fill=3.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=1.0), tmp_path / "obs.zarr")
    out = tmp_path / "bias.zarr"
    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--metric",
        "bias",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "bias" in ds
    assert ds.attrs["verify_metric"] == "bias"
    assert "bias" in capsys.readouterr().out


def test_mae_writes_field(tmp_path, verify_fn, capsys):
    fc = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=2.0), tmp_path / "obs.zarr")
    out = tmp_path / "mae.zarr"
    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--metric",
        "mae",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "mae" in ds
    assert "MAE" in capsys.readouterr().out


def test_step_forecast_without_time_is_refused(tmp_path, verify_fn):
    fc = write_zarr(make_forecast(n_step=2, fill=5.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=2, fill=5.0), tmp_path / "obs.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn,
            "--forecast",
            str(fc),
            "--obs",
            str(obs),
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_obs_finer_grid_is_refused(tmp_path, verify_fn):
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
            verify_fn,
            "--forecast",
            str(fc),
            "--obs",
            str(obs),
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2
