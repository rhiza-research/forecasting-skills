"""Correctness tests for ecmwf-fetch (no network; helpers + missing creds)."""

import os
from unittest.mock import patch

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_missing_ecmwf_env_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")
    }

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--date",
                "2026-01-01",
                "--bbox",
                "3/10/0/13",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_build_request(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast")
    assert req["year"] == ["2026"]
    assert req["month"] == ["01"]
    assert req["day"] == ["15"]
    assert req["forecast_type"] == "control_forecast"
    assert req["area"] == [3.0, 10.0, 0.0, 13.0]
    assert "total_precipitation" in req["variable"]


def test_split_wrapped_area(mod):
    assert mod._split_wrapped_area([3.0, 10.0, 0.0, 13.0]) == [[3.0, 10.0, 0.0, 13.0]]
    wrapped = mod._split_wrapped_area([3.0, 170.0, 0.0, -170.0])
    assert wrapped == [[3.0, 170.0, 0.0, 180.0], [3.0, -180.0, 0.0, -170.0]]
