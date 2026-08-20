"""Correctness tests for smap-fetch (mocked Earthdata; auth failure)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError


@pytest.fixture(scope="module")
def mod():
    return load_skill("smap-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_login_without_credentials_raises_data_error(mod):
    env = {
        k: v for k, v in os.environ.items() if k not in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(DataError, match="Earthdata authentication failed"):
            mod._login()


def test_fetch_writes_zarr_with_mocked_earthaccess(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    granule = MagicMock()
    granule.data_links.return_value = ["https://example/SPL3SMP_E_20260101_001.h5"]

    sm = np.array([[0.2, 0.3], [0.4, 0.5]])

    def fake_slice_from_file(_path, _group, _day_iso):
        return xr.DataArray(
            sm,
            dims=("latitude", "longitude"),
            coords={"latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
            name="soil_moisture",
            attrs={"units": "m3/m3"},
        )

    with (
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=[granule]),
        patch("earthaccess.download", return_value=["/tmp/fake.h5"]),
        patch.object(mod, "_slice_from_file", side_effect=fake_slice_from_file),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-01",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "soil_moisture" in ds
    assert ds["soil_moisture"].attrs.get("data_interval") == "1 day"
