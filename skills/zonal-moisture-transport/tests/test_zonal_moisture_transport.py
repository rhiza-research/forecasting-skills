"""Correctness tests for zonal-moisture-transport."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.provenance import load_history

G = 9.80665


def make_qu(
    q_fill=0.01,
    u_fill=10.0,
    levels=(1000.0, 850.0, 500.0),
    n_time=2,
    lats=(1.0, 2.0),
    lons=(10.0, 11.0),
    start="2026-01-01",
):
    """Small gridded q + u on a pressure `vertical` dim (hPa)."""
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    shape = (n_time, len(levels), len(lats), len(lons))
    ds = xr.Dataset(
        {
            "q": (("time", "vertical", "latitude", "longitude"), np.full(shape, q_fill)),
            "u": (("time", "vertical", "latitude", "longitude"), np.full(shape, u_fill)),
        },
        coords={
            "time": times.astype("datetime64[ns]"),
            "vertical": list(levels),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )
    ds["q"].attrs.update(units="kg kg-1", standard_name="specific_humidity")
    ds["u"].attrs.update(units="m s-1", standard_name="eastward_wind")
    ds["vertical"].attrs.update(
        units="hPa", standard_name="air_pressure", positive="down", axis="Z"
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


@pytest.fixture(scope="module")
def zmt():
    return load_skill(
        "zonal-moisture-transport", "zonal_moisture_transport"
    ).zonal_moisture_transport


@pytest.fixture(scope="module")
def mod():
    return load_skill("zonal-moisture-transport", "zonal_moisture_transport")


def test_integrates_to_viwve(tmp_path, zmt):
    src = write_zarr(make_qu(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(zmt, "-i", str(src), "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    assert "viwve" in ds.data_vars
    assert "vertical" not in ds.dims
    # q*u = 0.1; p = 1000, 850, 500 hPa → trapz in Pa, then /g
    expected = abs(np.trapezoid(np.full(3, 0.1), x=np.array([1000.0, 850.0, 500.0]) * 100.0)) / G
    assert ds["viwve"].values == pytest.approx(expected)
    assert ds["viwve"].attrs["units"] == "kg m-1 s-1"
    assert load_history(out)[-1]["skill"] == "zonal-moisture-transport"


def test_no_integrate_keeps_levels(tmp_path, zmt):
    src = write_zarr(make_qu(q_fill=0.02, u_fill=5.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(zmt, "-i", str(src), "--no-integrate", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    assert "qu" in ds.data_vars
    assert "vertical" in ds.dims
    assert ds["qu"].values == pytest.approx(0.10)
    assert ds["qu"].attrs["units"] == "kg kg-1 m s-1"


def test_two_inputs_humidity_then_wind(tmp_path, zmt):
    both = make_qu()
    q = write_zarr(both[["q"]], tmp_path / "q.zarr")
    u = write_zarr(both[["u"]], tmp_path / "u.zarr")
    out = tmp_path / "out.zarr"

    run_skill(zmt, "-i", str(q), "-i", str(u), "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    expected = abs(np.trapezoid(np.full(3, 0.1), x=np.array([1000.0, 850.0, 500.0]) * 100.0)) / G
    assert ds["viwve"].values == pytest.approx(expected)


def test_inner_join_overlapping_levels(tmp_path, zmt):
    q = make_qu(levels=(1000.0, 850.0, 500.0))[["q"]]
    u = make_qu(levels=(1000.0, 850.0, 700.0, 500.0, 200.0))[["u"]]
    q_path = write_zarr(q, tmp_path / "q.zarr")
    u_path = write_zarr(u, tmp_path / "u.zarr")
    out = tmp_path / "out.zarr"

    run_skill(zmt, "-i", str(q_path), "-i", str(u_path), "-o", str(out))

    expected = abs(np.trapezoid(np.full(3, 0.1), x=np.array([1000.0, 850.0, 500.0]) * 100.0)) / G
    ds = xr.open_zarr(out, consolidated=True)
    assert ds["viwve"].values == pytest.approx(expected)


def test_integrate_requires_two_levels(tmp_path, zmt):
    src = write_zarr(make_qu(levels=(850.0,)), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(zmt, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
    assert not Path(out).exists()


def test_missing_humidity_exits(tmp_path, zmt):
    src = write_zarr(make_qu()[["u"]], tmp_path / "u_only.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(zmt, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_output_name_override(tmp_path, zmt):
    src = write_zarr(make_qu(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(zmt, "-i", str(src), "--output-name", "ivt_x", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["ivt_x"]


def test_as_pa_hpa_and_already_pa(mod):
    hpa = xr.DataArray([1000.0, 850.0], dims="vertical", attrs={"units": "hPa"})
    assert mod._as_pa(hpa).values == pytest.approx([100000.0, 85000.0])
    pa = xr.DataArray([100000.0, 85000.0], dims="vertical", attrs={"units": "Pa"})
    assert mod._as_pa(pa).values == pytest.approx([100000.0, 85000.0])
