"""Correctness tests for oisst-fetch (mocked network)."""

from pathlib import Path
from unittest.mock import patch

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch():
    return load_skill("oisst-fetch", "fetch").fetch


def _oisst_piece():
    ds = make_gridded(n_time=2, start="2026-01-01", name="sst", fill=295.0)
    ds = ds.rename({"latitude": "lat", "longitude": "lon"})
    ds["sst"].attrs.update(units="K", long_name="Sea Surface Temperature")
    return ds


def test_fetch_writes_zarr_with_mocked_opendap(tmp_path, fetch):
    out = tmp_path / "out.zarr"

    with patch("xarray.open_dataset", return_value=_oisst_piece()):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "3/10/0/13",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "sst" in ds
    assert ds["sst"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "oisst-fetch"
