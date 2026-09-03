"""Correctness tests for select."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def select():
    return load_skill("select", "select_dim").select


def test_select_by_index_collapses_dim(tmp_path, select):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--index", "1")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert load_history(out)[-1]["skill"] == "select"


def test_select_requires_index_or_value(tmp_path, select):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time")
    assert exc.value.code == 2


def test_select_value_station_id_stringdtype(tmp_path, select):
    """Zarr v3 stores pandas/object station ids as NumPy StringDType (kind T)."""
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    frames = []
    for sid in ("TA00072", "TA00131"):
        frames.append(
            pd.DataFrame(
                {"precip": [1.0, 2.0], "station_id": sid},
                index=times,
            )
        )
    long = pd.concat(frames)
    long.index.name = "time"
    long = long.reset_index().set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    assert xr.open_zarr(src, consolidated=True)["station_id"].dtype.kind == "T"

    out = tmp_path / "out.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
        "--value",
        "TA00131",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert list(result["station_id"].values) == ["TA00072", "TA00131"]
    assert load_history(out)[-1]["skill"] == "select"


def test_select_one_station_keeps_identity_coords(tmp_path, select):
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    long = pd.DataFrame(
        {
            "precip": [1.0, 2.0, 3.0, 4.0],
            "station_id": ["TA00072", "TA00072", "TA00131", "TA00131"],
            "time": list(times) * 2,
        }
    ).set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds = ds.assign_coords(
        name=("station_id", ["Likoni", "Shimo la Tewa"]),
        latitude=("station_id", [-4.08, -3.99]),
        longitude=("station_id", [39.66, 39.74]),
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "one.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert "station_id" not in result.dims
    assert str(result["station_id"].values) == "TA00072"
    assert str(result["name"].values) == "Likoni"
