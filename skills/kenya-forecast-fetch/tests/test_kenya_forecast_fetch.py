"""Correctness tests for kenya-forecast-fetch (network / remote Zarr mocked)."""

from pathlib import Path

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
