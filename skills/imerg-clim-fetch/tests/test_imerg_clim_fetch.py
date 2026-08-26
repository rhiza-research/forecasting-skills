"""Correctness tests for imerg-clim-fetch (mocked Sheerwater)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


def _make_clim_forecast(n_init=2, start="2020-01-01"):
    init = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_init, "D"))
    leads = np.array([np.timedelta64(0, "D"), np.timedelta64(1, "D")], dtype="timedelta64[ns]")
    lats = [0.0, 1.0]
    lons = [10.0, 11.0]
    data = np.full((n_init, len(leads), len(lats), len(lons)), 2.5, dtype=np.float64)
    return xr.Dataset(
        {"precip": (("init_time", "prediction_timedelta", "lat", "lon"), data)},
        coords={
            "init_time": init.astype("datetime64[ns]"),
            "prediction_timedelta": leads,
            "lat": lats,
            "lon": lons,
        },
    )


@pytest.fixture(scope="module")
def fetch():
    return load_skill("imerg-clim-fetch", "fetch").fetch


def test_fetch_writes_zarr_with_mocked_sheerwater(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    mock_ds = _make_clim_forecast()

    with patch(
        "sheerwater.climatology.climatology_imerg_1998_2024",
        return_value=mock_ds,
    ) as clim_fn:
        run_skill(
            fetch,
            "--start-time",
            "2020-01-01",
            "--end-time",
            "2020-01-02",
            "--grid",
            "global0_25",
            "-o",
            str(out),
        )
        clim_fn.assert_called_once_with(
            "2020-01-01",
            "2020-01-02",
            "precip",
            grid="global0_25",
            region="global",
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert set(ds.dims) == {"time", "lat", "lon"}
    assert ds.sizes["time"] == 2
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert ds.attrs.get("climatology_first_year") == 1998
    assert ds.attrs.get("weather_skills_source") == "sheerwater:climatology_imerg_1998_2024"
    assert load_history(out)[-1]["skill"] == "imerg-clim-fetch"
