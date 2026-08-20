"""Correctness tests for ecmwf-fetch (no network; helpers + missing creds)."""

import os
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import UsageError


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


def test_unknown_variable_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {
        **os.environ,
        "ECMWF_DATASTORES_URL": "https://example.invalid",
        "ECMWF_DATASTORES_KEY": "dummy",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--date",
                "2026-01-01",
                "--bbox",
                "3/10/0/13",
                "-v",
                "2m_temperature",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_resolve_variables_default_and_aliases(mod):
    assert mod._resolve_variables(None) == ["tp"]
    assert mod._resolve_variables(["t2m", "tp", "t2m"]) == ["t2m", "tp"]
    assert mod._resolve_variables(["2_m_temperature"]) == ["t2m"]
    with pytest.raises(UsageError, match="most used first"):
        mod._resolve_variables(["2m_temperature"])


def test_variables_most_used_first(mod):
    names = list(mod.VARIABLES)
    assert names[:2] == ["tp", "t2m"]


def test_build_request(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast")
    assert req["year"] == ["2026"]
    assert req["month"] == ["01"]
    assert req["day"] == ["15"]
    assert req["forecast_type"] == "control_forecast"
    assert req["area"] == [3.0, 10.0, 0.0, 13.0]
    assert req["variable"] == ["total_precipitation"]
    assert req["leadtime_hour"][0] == "0"


def test_build_request_daily_mean(mod):
    req = mod._build_request(
        "2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t2m"]
    )
    assert req["variable"] == ["2_m_temperature"]
    assert req["leadtime_hour"][0] == "0_24"
    assert "144_168" in req["leadtime_hour"]
    assert "0" not in req["leadtime_hour"]


def test_rename_grib_short_names(mod):
    ds = xr.Dataset({"2t": ("x", [1.0]), "noise": ("x", [0.0])})
    out = mod._rename_to_short(ds, ["t2m"])
    assert list(out.data_vars) == ["t2m"]


def test_split_wrapped_area(mod):
    assert mod._split_wrapped_area([3.0, 10.0, 0.0, 13.0]) == [[3.0, 10.0, 0.0, 13.0]]
    wrapped = mod._split_wrapped_area([3.0, 170.0, 0.0, -170.0])
    assert wrapped == [[3.0, 170.0, 0.0, 180.0], [3.0, -180.0, 0.0, -170.0]]


def test_standardize_mixed_tp_and_t2m(mod):
    steps = np.array([np.timedelta64(d, "D") for d in (1, 2, 3)])
    ds = xr.Dataset(
        {
            "tp": (("step", "latitude"), np.array([[1.0, 1.0], [3.0, 3.0], [6.0, 6.0]])),
            "t2m": (
                ("step", "latitude"),
                np.array([[280.0, 281.0], [282.0, 283.0], [284.0, 285.0]]),
            ),
        },
        coords={
            "time": np.datetime64("2026-01-01", "ns"),
            "step": steps,
            "latitude": [1.0, 2.0],
        },
    )
    ds["tp"].attrs.update(units="kg m-2")
    ds["t2m"].attrs.update(units="K")
    out = mod._standardize(ds)
    assert out.sizes["step"] == 2
    assert out["tp"].attrs["units"] == "mm day-1"
    np.testing.assert_allclose(out["tp"].values, [[2.0, 2.0], [3.0, 3.0]])
    assert out["t2m"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(
        out["t2m"].values,
        np.array([[282.0, 283.0], [284.0, 285.0]]) - 273.15,
        rtol=1e-5,
    )
