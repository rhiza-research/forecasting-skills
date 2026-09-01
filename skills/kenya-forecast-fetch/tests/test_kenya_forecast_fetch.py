"""Correctness tests for kenya-forecast-fetch (network / remote Zarr mocked)."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("kenya-forecast-fetch", "fetch")


def test_fetch_writes_zarr_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "kenya_tp.zarr"
    remote = make_forecast(name="tp", members=3)

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda: ["2026-08-01", "2026-08-04"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: "2026-08-04" in key)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--dataset", "precip", "-o", str(out))

    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "tp" in ds.data_vars
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        assert "aggregation_period" not in ds["tp"].attrs
        assert "kenya-forecasting-data:" in ds.attrs.get("weather_skills_source", "")
    history = load_history(out)
    assert history[-1]["skill"] == "kenya-forecast-fetch"


def test_fetch_overwrites_rate_standard_name_on_amount_units(tmp_path, fetch_mod, monkeypatch):
    """Archive grids may stamp lwe_precipitation_rate on kg m-2 amounts."""
    out = tmp_path / "kenya_tp_amount.zarr"
    remote = make_forecast(name="tp", members=2)
    remote["tp"].attrs.update(
        units="kg m-2",
        standard_name="lwe_precipitation_rate",
    )

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda: ["2026-08-17"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--dataset", "precip", "--date", "2026-08-17", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        assert "precipitation_amount" not in ds["tp"].attrs["standard_name"]
        assert ds["tp"].attrs["standard_name"] == "lwe_precipitation_rate"
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        assert "aggregation_period" not in ds["tp"].attrs
        assert "aggregation_coverage" not in ds.coords


def test_fetch_honors_date_variable_and_bbox(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "kenya_u.zarr"
    remote = make_forecast(name="u10", members=2, fill=3.0)
    remote["v10"] = remote["u10"].copy()
    remote["v10"].attrs.update(units="m s-1")
    seen = {}

    monkeypatch.setattr(
        fetch_mod,
        "_store_exists",
        lambda key: seen.setdefault("key", key) or True,
    )

    def fake_open(key):
        seen["open"] = key
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "10wind",
        "--date",
        "2026-08-04",
        "-v",
        "u10",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )

    assert seen["key"] == "2026-08-04/data/ECMWF_s2s_10wind_2026-08-04.zarr"
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["u10"]
    # bbox 3/9/0/12 against lats (1,2) lons (10,11) keeps both lats, lon 10..11
    assert float(ds.latitude.min()) >= 0
    assert float(ds.longitude.max()) <= 12


def test_probe_latest(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda: ["2026-08-01", "2026-08-04"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: "2026-08-04" in key)
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-08-04"


def test_fetch_gefs_already_daily_shifts_to_lead_zero(tmp_path, fetch_mod, monkeypatch):
    """GEFS archive tp is per-step mm starting at 1d — do not deaccumulate."""
    out = tmp_path / "gefs.zarr"
    remote = make_forecast(name="tp", n_step=3)
    remote = remote.assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (1, 2, 3)]))
    )
    remote["tp"].values[:] = np.array(
        [[[5.0, 5.0], [5.0, 5.0]], [[2.0, 2.0], [2.0, 2.0]], [[0.0, 0.0], [0.0, 0.0]]]
    )
    remote["tp"].attrs.update(units="mm")

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--dataset", "gefs", "--date", "2026-08-24", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0], [5.0, 2.0, 0.0])
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds.sizes["step"] == 3


def test_fetch_daily_vars_shifts_step_to_lead_zero(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "daily_vars.zarr"
    remote = make_forecast(name="t2m", n_step=3, fill=290.0)
    remote = remote.assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (1, 2, 3)]))
    )
    remote["t2m"].attrs.update(units="K", standard_name="air_temperature")

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "daily_vars",
        "--date",
        "2026-08-24",
        "-v",
        "t2m",
        "-o",
        str(out),
    )

    with xr.open_zarr(out, consolidated=True) as ds:
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        assert ds.sizes["step"] == 3


def test_fetch_medium_range_weekly_totals_left_labeled(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "medium.zarr"
    remote = make_forecast(name="tp", n_step=2)
    remote = remote.assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (7, 14)]))
    )
    remote["tp"].values[:] = np.array([[[7.0, 7.0], [7.0, 7.0]], [[14.0, 14.0], [14.0, 14.0]]])
    remote["tp"].attrs.update(units="mm")

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "medium_range_precip",
        "--date",
        "2026-08-24",
        "-o",
        str(out),
    )

    with xr.open_zarr(out, consolidated=True) as ds:
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 7]
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0], [1.0, 2.0])
        assert ds["tp"].attrs["units"] == "mm day-1"


def test_fetch_winds_do_not_shift_step(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wind.zarr"
    remote = make_forecast(name="u10", n_step=3, fill=3.0)
    remote = remote.assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (0, 1, 2)]))
    )
    remote["u10"].attrs.update(units="m s-1")

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "10wind",
        "--date",
        "2026-08-04",
        "-v",
        "u10",
        "-o",
        str(out),
    )

    with xr.open_zarr(out, consolidated=True) as ds:
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        assert ds.sizes["step"] == 3
