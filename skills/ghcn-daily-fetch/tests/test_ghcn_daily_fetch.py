"""Correctness tests for ghcn-daily-fetch (mocked network)."""

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("ghcn-daily-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _stations():
    return pd.DataFrame(
        {"latitude": [1.0], "longitude": [10.0], "name": ["Test Station"]},
        index=pd.Index(["USW00094728"], name="station_id"),
    )


def _station_frame(station_id, _elements, _start_int, _end_int):
    times = pd.to_datetime(["2026-01-01", "2026-01-02"])
    frame = pd.DataFrame({"precip": [1.0, 2.0], "station_id": station_id}, index=times)
    frame.index.name = "time"
    return frame


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with (
        patch.object(mod, "_load_stations", return_value=_stations()),
        patch.object(mod, "_station_frame", side_effect=_station_frame),
    ):
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

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert "station_id" in ds.dims
    assert load_history(out)[-1]["skill"] == "ghcn-daily-fetch"
