"""Correctness tests for chirps-fetch (mocked network)."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("chirps-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _fake_open_day(_tif, day: date):
    da = make_gridded(n_time=1, start=day.isoformat()).precip
    return da.isel(time=0).expand_dims(time=[np.datetime64(day.isoformat(), "ns")])


def test_fetch_writes_zarr_with_mocked_download(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    def fake_download(_session, day, dest_dir):
        return dest_dir / f"{day.isoformat()}.tif"

    with (
        patch.object(mod, "_download_day_tif", side_effect=fake_download),
        patch.object(mod, "_open_day", side_effect=_fake_open_day),
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
    assert ds.sizes["time"] == 2
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert "aggregation_period" not in ds["precip"].attrs
    assert load_history(out)[-1]["skill"] == "chirps-fetch"


def test_missing_days_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_download_day_tif", side_effect=mod.DayUnavailable("404")):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-01",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_probe_latest_lists_directory(capsys, fetch, monkeypatch, mod):
    def fake_list(prefix: str):
        year = prefix.rstrip("/").rsplit("/", 1)[-1]
        if "prelim" in prefix:
            return [f"{prefix}chirps-v3.0.prelim.{year}.08.15.tif"]
        return []

    monkeypatch.setattr(mod, "_list_object_names", fake_list)
    run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == f"{date.today().year}-08-15"


def test_day_urls_use_chc_mirror(mod):
    day = date(2024, 1, 2)
    final = mod._object_url(
        f"{mod._CHIRPS_FINAL_PREFIX}/{day.year:04d}/chirps-v3.0.sat.2024.01.02.tif"
    )
    assert "sheerwater-public-datalake/chc-mirror/products/CHIRPS/" in final
    assert "storage.googleapis.com" in final
