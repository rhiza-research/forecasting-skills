"""Correctness tests for arco-era5-fetch (mocked GCS open)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("arco-era5-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _arco_ds():
    ds = make_gridded(n_time=2, start="2026-01-01", name="2m_temperature", fill=280.0)
    ds["2m_temperature"].attrs.update(
        units="K",
        long_name="2 metre temperature",
        standard_name="air_temperature",
    )
    return ds


def test_fetch_writes_zarr_with_mocked_open(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_arco", return_value=_arco_ds()):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-v",
            "2m_temperature",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "2m_temperature" in ds
    assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["2m_temperature"].values, 280.0 - 273.15, rtol=1e-5)
    assert ds["2m_temperature"].attrs.get("data_interval") == "1 hour"
    assert load_history(out)[-1]["skill"] == "arco-era5-fetch"
