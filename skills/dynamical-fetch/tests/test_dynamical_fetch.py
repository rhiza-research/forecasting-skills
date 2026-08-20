"""Correctness tests for dynamical-fetch (mocked catalog)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("dynamical-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _forecast_catalog_ds():
    inits = np.array([np.datetime64("2026-01-01")])
    steps = np.array([np.timedelta64(1, "D"), np.timedelta64(2, "D")])
    lats = [1.0, 2.0]
    lons = [10.0, 11.0]
    data = np.ones((1, 2, 2, 2))
    return xr.Dataset(
        {"tp": (("init_time", "lead_time", "latitude", "longitude"), data)},
        coords={
            "init_time": inits,
            "lead_time": steps,
            "latitude": lats,
            "longitude": lons,
        },
    )


def test_forecast_fetch_writes_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "test-forecast",
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    written = xr.open_zarr(out, consolidated=True)
    assert "tp" in written
    assert load_history(out)[-1]["skill"] == "dynamical-fetch"


def _imerg_like_analysis_ds():
    times = np.array([np.datetime64("2026-01-01"), np.datetime64("2026-01-02")])
    lats = [1.0, 2.0]
    lons = [10.0, 11.0]
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.ones((2, 2, 2)) * 1e-6,
                {"units": "kg m-2 s-1"},
            ),
            "precipitation_quality_index_surface": (
                ("time", "latitude", "longitude"),
                np.full((2, 2, 2), 0.8),
                {"units": "1"},
            ),
        },
        coords={"time": times, "latitude": lats, "longitude": lons},
    )


def test_imerg_quality_index_does_not_block_standard_units(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    state = {"ds": _imerg_like_analysis_ds(), "shape": "analysis"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "nasa-imerg-analysis-late",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert written["precipitation_quality_index_surface"].attrs["units"] == "1"
    assert written["precipitation_surface"].attrs["units"] == "mm day-1"


def test_forecast_missing_date_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "--dataset", "test-forecast", "-o", str(out))
    assert exc.value.code == 2
