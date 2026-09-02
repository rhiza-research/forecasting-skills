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
    assert mod._resolve_variables(["sst"]) == ["sst"]
    assert mod._resolve_variables(["wtmp"]) == ["sst"]
    assert mod._resolve_variables(["sea_surface_temperature"]) == ["sst"]
    with pytest.raises(UsageError, match="most used first"):
        mod._resolve_variables(["2m_temperature"])


def test_resolve_variables_space_comma_and_repeated_flags(mod):
    """One call can take `-v tp t2m`, `-v tp -v t2m`, or `-v tp,t2m`."""
    assert mod._resolve_variables([["tp", "t2m"]]) == ["tp", "t2m"]
    assert mod._resolve_variables([["tp"], ["t2m"]]) == ["tp", "t2m"]
    assert mod._resolve_variables(["tp,t2m"]) == ["tp", "t2m"]
    assert mod._resolve_variables([["tp,t2m", "sst"]]) == ["tp", "t2m", "sst"]


def test_parser_accepts_space_separated_variables(fetch):
    args = fetch.parser.parse_args(
        ["--date", "2026-01-01", "--bbox", "3/10/0/13", "-v", "tp", "t2m", "-o", "out.zarr"]
    )
    assert args.variable == [["tp", "t2m"]]
    args = fetch.parser.parse_args(
        [
            "--date",
            "2026-01-01",
            "--bbox",
            "3/10/0/13",
            "-v",
            "tp",
            "-v",
            "t2m",
            "-o",
            "out.zarr",
        ]
    )
    assert args.variable == [["tp"], ["t2m"]]


def test_variables_most_used_first(mod):
    names = list(mod.VARIABLES)
    assert names[:3] == ["tp", "t2m", "sst"]
    assert "skt" in names
    assert "ocu" in names
    assert "gh" in names
    assert "t" in names
    assert "q" in names
    assert "pv" in names


def test_build_request(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast")
    assert req["year"] == ["2026"]
    assert req["month"] == ["01"]
    assert req["day"] == ["15"]
    assert req["forecast_type"] == "control_forecast"
    assert req["area"] == [3.0, 10.0, 0.0, 13.0]
    assert req["variable"] == ["total_precipitation"]
    assert req["leadtime_hour"][0] == "0"
    assert req["leadtime_hour"][1] == "24"
    assert req["leadtime_hour"][-1] == "1104"
    assert req["level_type"] == "single_level"
    assert "pressure_level" not in req


def test_leadtime_hours_are_daily_through_day_46(mod):
    hours = [int(h) for h in mod.LEADTIME_HOURS]
    assert hours == list(range(0, 46 * 24 + 1, 24))


def test_build_request_daily_mean(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t2m"])
    assert req["variable"] == ["2_m_temperature"]
    assert req["leadtime_hour"][0] == "0_24"
    assert "24_48" in req["leadtime_hour"]
    assert "144_168" in req["leadtime_hour"]
    assert req["leadtime_hour"][-1] == "1080_1104"
    assert "0" not in req["leadtime_hour"]


def test_build_request_sst(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["sst"])
    assert req["variable"] == ["sea_surface_temperature"]
    assert req["level_type"] == "single_level"
    assert req["leadtime_hour"][0] == "0_24"
    assert "pressure_level" not in req


def test_build_request_pressure_level(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t"])
    assert req["variable"] == ["temperature"]
    assert req["level_type"] == "pressure_level"
    assert req["pressure_level"][0] == "1000"
    assert req["pressure_level"][-1] == "10"
    assert req["leadtime_hour"][0] == "0"


def test_build_request_specific_humidity_seven_levels(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["q"])
    assert req["pressure_level"] == ["1000", "925", "850", "700", "500", "300", "200"]


def test_build_request_potential_vorticity(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["pv"])
    assert req["level_type"] == "potential_temperature"
    assert req["potential_temperature"] == ["320"]


def test_group_pressure_separate_from_surface(mod):
    groups = dict(mod._group_for_request(["tp", "t", "q"]))
    assert len(groups) == 3


def test_group_same_family_stays_one_request(mod):
    groups = dict(mod._group_for_request(["t2m", "sst", "tp"]))
    assert len(groups) == 2
    daily = next(names for key, names in groups.items() if key[0] == "daily")
    instant = next(names for key, names in groups.items() if key[0] == "instant")
    assert daily == ["t2m", "sst"]
    assert instant == ["tp"]


def test_build_request_multiple_daily_variables(mod):
    req = mod._build_request(
        "2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t2m", "sst"]
    )
    assert req["variable"] == ["2_m_temperature", "sea_surface_temperature"]
    assert req["leadtime_hour"][0] == "0_24"


def test_promote_vertical_isobaric(mod):
    ds = xr.Dataset(
        {"t": (("isobaricInhPa", "latitude"), [[1.0, 2.0], [3.0, 4.0]])},
        coords={"isobaricInhPa": [1000.0, 850.0], "latitude": [1.0, 2.0]},
    )
    out = mod._promote_vertical(ds)
    assert "vertical" in out.dims
    assert "isobaricInhPa" not in out.dims
    assert out["vertical"].attrs["units"] == "hPa"


def test_rename_grib_short_names(mod):
    ds = xr.Dataset({"2t": ("x", [1.0]), "noise": ("x", [0.0])})
    out = mod._rename_to_short(ds, ["t2m"])
    assert list(out.data_vars) == ["t2m"]


def test_rename_wtmp_to_sst(mod):
    ds = xr.Dataset({"wtmp": ("x", [290.0])})
    out = mod._rename_to_short(ds, ["sst"])
    assert list(out.data_vars) == ["sst"]


def test_split_wrapped_area(mod):
    assert mod._split_wrapped_area([3.0, 10.0, 0.0, 13.0]) == [[3.0, 10.0, 0.0, 13.0]]
    wrapped = mod._split_wrapped_area([3.0, 170.0, 0.0, -170.0])
    assert wrapped == [[3.0, 170.0, 0.0, 180.0], [3.0, -180.0, 0.0, -170.0]]


def test_standardize_mixed_tp_and_t2m(mod):
    steps = np.array([np.timedelta64(d, "D") for d in (0, 1, 2)])
    ds = xr.Dataset(
        {
            "tp": (("step", "latitude"), np.array([[0.0, 0.0], [2.0, 2.0], [5.0, 5.0]])),
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
    np.testing.assert_array_equal(
        np.asarray(out["step"].values).astype("timedelta64[D]"),
        np.array([0, 1], dtype="timedelta64[D]"),
    )
    assert out["tp"].attrs["units"] == "mm day-1"
    np.testing.assert_allclose(out["tp"].values, [[2.0, 2.0], [3.0, 3.0]])
    assert out["t2m"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(
        out["t2m"].values,
        np.array([[280.0, 281.0], [282.0, 283.0]]) - 273.15,
        rtol=1e-5,
    )


def test_standardize_sst_to_celsius(mod):
    steps = np.array([np.timedelta64(d, "D") for d in (1, 2)])
    ds = xr.Dataset(
        {"sst": (("step", "latitude"), np.array([[290.0, 291.0], [292.0, 293.0]]))},
        coords={
            "time": np.datetime64("2026-01-01", "ns"),
            "step": steps,
            "latitude": [1.0, 2.0],
        },
    )
    ds["sst"].attrs.update(units="K")
    out = mod._standardize(ds)
    assert out["sst"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(
        out["sst"].values, np.array([[290.0, 291.0], [292.0, 293.0]]) - 273.15
    )
