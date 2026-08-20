"""Correctness tests for imerg-fetch (mocked network)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch():
    return load_skill("imerg-fetch", "fetch").fetch


def test_fetch_writes_zarr_with_mocked_earthaccess(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    mock_ds = make_gridded(n_time=2, start="2026-01-01")
    mock_ds = mock_ds.rename({"latitude": "lat", "longitude": "lon", "precip": "precipitation"})

    mock_results = [MagicMock()]
    mock_files = ["/tmp/fake.h5"]

    with (
        patch("earthaccess.login"),
        patch("earthaccess.search_data", return_value=mock_results),
        patch("earthaccess.download", return_value=mock_files),
        patch("xarray.open_mfdataset", return_value=mock_ds),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert "aggregation_period" not in ds["precip"].attrs
    assert load_history(out)[-1]["skill"] == "imerg-fetch"
