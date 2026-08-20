"""Correctness tests for openaq-fetch (mocked network + missing key)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("openaq-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_missing_openaq_key_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {k: v for k, v in os.environ.items() if k != "OPENAQ_API_KEY"}

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-02",
                "--bbox",
                "2/9/0/11",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    sensors = [
        {
            "sensor_id": 1,
            "parameter": "pm25",
            "units": "µg/m³",
            "station_id": "loc1",
            "name": "Test",
            "latitude": 1.0,
            "longitude": 10.0,
        }
    ]

    def fake_sensor_daily(_session, desc, _start_iso, _end_iso):
        return [("2026-01-01", 12.5), ("2026-01-02", 13.0)]

    with (
        patch.dict(os.environ, {"OPENAQ_API_KEY": "test-key"}),
        patch.object(mod, "_find_sensors", return_value=sensors),
        patch.object(mod, "_sensor_daily", side_effect=fake_sensor_daily),
        patch.object(mod.requests, "Session", return_value=MagicMock()),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "2/9/0/11",
            "-v",
            "pm25",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "pm25" in ds
    assert ds["pm25"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "openaq-fetch"
