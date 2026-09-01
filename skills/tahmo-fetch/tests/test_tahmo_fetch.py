"""Correctness tests for tahmo-fetch (missing creds + mocked API)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("tahmo-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_missing_tahmo_credentials_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {
        k: v for k, v in os.environ.items() if k not in ("TAHMO_API_USERNAME", "TAHMO_API_PASSWORD")
    }

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-02",
                "--country",
                "Kenya",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    stations = pd.DataFrame(
        {
            "code": ["TA00001"],
            "location_countrycode": ["KE"],
            "location_latitude": [1.0],
            "location_longitude": [36.0],
        }
    )

    daily = pd.DataFrame(
        {"precip": [1.5, 2.0], "station_id": "TA00001"},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    daily.index.name = "time"

    api = MagicMock()
    api.getVariables.return_value = {"pr": {"units": "mm", "description": "Precipitation"}}

    def fake_setup(state, countries):
        state["api"] = api
        state["stations"] = stations
        state["var_meta"] = api.getVariables()
        return api, stations, state["var_meta"]

    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=fake_setup),
        patch.object(mod, "_station_frame", return_value=daily),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--country",
            "Kenya",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "tahmo-fetch"
