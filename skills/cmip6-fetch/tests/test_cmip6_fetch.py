"""Correctness tests for cmip6-fetch (mocked catalog + zarr open)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core import UsageError
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("cmip6-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _cmip6_state():
    ds = make_gridded(n_time=2, start="2020-01-01", name="tas", fill=280.0)
    ds = ds.rename({"latitude": "lat", "longitude": "lon"})
    ds["tas"].attrs.update(
        units="K",
        long_name="Near-Surface Air Temperature",
        standard_name="air_temperature",
    )
    ds["time"].encoding.update(calendar="proleptic_gregorian", units="days since 1850-01-01")
    return {
        "ds": ds,
        "grid_label": "gn",
        "version": "v20200101",
        "source_calendar": "proleptic_gregorian",
        "source_time_units": "days since 1850-01-01",
    }


def test_fetch_writes_zarr_with_mocked_remote(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_remote", return_value=_cmip6_state()):
        run_skill(
            fetch,
            "--start-time",
            "2020-01-01",
            "--end-time",
            "2020-01-02",
            "-v",
            "tas",
            "--model",
            "GFDL-CM4",
            "--experiment",
            "historical",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "tas" in ds
    assert ds["tas"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["tas"].values, 280.0 - 273.15, rtol=1e-5)
    assert ds["tas"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "cmip6-fetch"


def test_resolve_zstore_unknown_model(mod):
    import pandas as pd

    catalog = pd.DataFrame(
        {
            "source_id": ["GFDL-CM4"],
            "experiment_id": ["historical"],
            "variable_id": ["tas"],
            "member_id": ["r1i1p1f1"],
            "table_id": ["Amon"],
            "grid_label": ["gn"],
            "version": ["v20200101"],
            "zstore": ["gs://bucket/tas.zarr"],
        }
    )
    with patch.object(mod.pd, "read_csv", return_value=catalog):
        with pytest.raises(UsageError, match="NO-SUCH-MODEL"):
            mod._resolve_zstore("NO-SUCH-MODEL", "historical", "tas", "r1i1p1f1", "Amon", None)


def test_probe_latest_is_none(capsys, fetch):
    run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "none"
