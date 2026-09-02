"""Correctness tests for coarsen."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def coarsen():
    return load_skill("coarsen", "coarsen").coarsen


def test_coarsen_reduces_spatial_dims(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "2.0",
        "--offset",
        "0.0",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] < 3
    assert ds.sizes["longitude"] < 4
    assert units_equal(ds["precip"].attrs.get("units"), "mm day-1")
    assert load_history(out)[-1]["skill"] == "coarsen"


def test_coarsen_rejects_finer_target(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            coarsen,
            "-i",
            str(src),
            "-o",
            str(out),
            "--target-resolution",
            "0.5",
            "--offset",
            "0.0",
        )
    assert exc.value.code == 2


def test_coarsen_reference_grid_copies_exact_coords(tmp_path, coarsen):
    """Matching another Zarr must reuse its lat/lon values, not rebuild floats."""
    import numpy as np

    fine = make_gridded(
        lats=(0.0, 0.5, 1.0, 1.5, 2.0),
        lons=(10.0, 10.5, 11.0, 11.5, 12.0),
        fill=1.0,
    )
    # Deliberately awkward floats that resolution+offset would not reproduce exactly.
    ref_lats = np.array([0.05, 1.05, 2.05], dtype=np.float64)
    ref_lons = np.array([10.05, 11.05, 12.05], dtype=np.float64)
    ref = make_gridded(lats=tuple(ref_lats), lons=tuple(ref_lons), fill=2.0)
    # Nudge stored coords so they are not simple k*res reconstructions.
    ref = ref.assign_coords(
        latitude=ref_lats + np.array([1e-12, -2e-12, 3e-12]),
        longitude=ref_lons + np.array([-1e-12, 2e-12, -3e-12]),
    )
    src = write_zarr(fine, tmp_path / "fine.zarr")
    ref_path = write_zarr(ref, tmp_path / "ref.zarr")
    out = tmp_path / "out.zarr"

    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref_path))

    result = xr.open_zarr(out, consolidated=True)
    ref_open = xr.open_zarr(ref_path, consolidated=True)
    assert np.array_equal(result["latitude"].values, ref_open["latitude"].values)
    assert np.array_equal(result["longitude"].values, ref_open["longitude"].values)


def test_coarsen_reference_grid_rejects_finer(tmp_path, coarsen):
    coarse = write_zarr(
        make_gridded(lats=(0.0, 2.0), lons=(10.0, 12.0), fill=1.0),
        tmp_path / "coarse.zarr",
    )
    fine = write_zarr(
        make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=1.0),
        tmp_path / "fine.zarr",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(
            coarsen,
            "-i",
            str(coarse),
            "-o",
            str(tmp_path / "out.zarr"),
            "--reference-grid",
            str(fine),
        )
    assert exc.value.code == 2


def test_coarsen_requires_mode(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(coarsen, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
