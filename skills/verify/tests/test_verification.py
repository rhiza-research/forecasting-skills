"""Unit tests for verify skill computation helpers."""

import importlib.util
from pathlib import Path

import pytest

from weather_skills_core.errors import UsageError
from conftest import SKILLS_ROOT, make_gridded


def _load_verification():
    import sys

    path = SKILLS_ROOT / "verify" / "scripts" / "verification.py"
    mod_name = "verify_verification"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v():
    return _load_verification()


def _grid(v, *, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def test_compute_hits_hit_disagree_below(v):
    fc = _grid(v, event_at=[(0, 0), (0, 1)])["precip"].isel(time=0)
    obs = _grid(v, event_at=[(0, 0), (1, 0)])["precip"].isel(time=0)
    result = v.compute(fc, obs, metric="hits", threshold=1.0)
    hit = result.field.values
    assert hit[0, 0] == pytest.approx(1)
    assert hit[0, 1] == pytest.approx(-1)
    assert hit[1, 0] == pytest.approx(-1)
    assert hit[1, 1] == pytest.approx(0)
    rate, n_hit, n_obs = v.hit_rate(result.field, result.obs_event)
    assert n_obs == 2
    assert n_hit == 1
    assert rate == pytest.approx(0.5)


def test_compute_bias(v):
    fc = _grid(v, event_at=[], fill=2.0)["precip"].isel(time=0)
    obs = _grid(v, event_at=[], fill=1.0)["precip"].isel(time=0)
    result = v.compute(fc, obs, metric="bias")
    assert result.field.name == "bias"
    assert result.field.values[0, 0] == pytest.approx(1.0)


def test_score_summary_hits(v):
    fc = _grid(v, event_at=[(0, 0), (0, 1)])["precip"].isel(time=0)
    obs = _grid(v, event_at=[(0, 0), (1, 0)])["precip"].isel(time=0)
    result = v.compute(fc, obs, metric="hits", threshold=1.0)
    assert "hit rate 50%" in v.score_summary("hits", field=result.field, obs_event=result.obs_event)


def test_obs_finer_grid_is_refused(v):
    fc = make_gridded(n_time=1, fill=5.0, lats=(1.0, 2.0), lons=(10.0, 11.0))
    obs = make_gridded(n_time=1, fill=5.0, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0))
    with pytest.raises(UsageError, match="grid spacing"):
        v.require_obs_on_forecast_grid(fc, obs)
