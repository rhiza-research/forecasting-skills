"""Correctness tests for coarsen."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.standard_utils import normalize_latlon_coords
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
    # Values that survive the 5-decimal snap but are not k*0.5 reconstructions.
    ref_lats = np.array([0.05123, 1.05234, 2.05345], dtype=np.float64)
    ref_lons = np.array([10.05123, 11.05234, 12.05345], dtype=np.float64)
    ref = make_gridded(lats=tuple(ref_lats), lons=tuple(ref_lons), fill=2.0)
    src = write_zarr(fine, tmp_path / "fine.zarr")
    ref_path = write_zarr(ref, tmp_path / "ref.zarr")
    out = tmp_path / "out.zarr"

    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref_path))

    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref_path, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


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


def test_coarsen_reference_grid_allows_lateral_shift(tmp_path, coarsen):
    """Same 0.05° spacing, half-cell lon offset (Kenya downscaled vs CHIRPS)."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.00, 10.05, 10.10, 10.15),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.025, 10.075, 10.125, 10.175),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref))
    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


def test_coarsen_reference_grid_allows_near_equal_mixed_axes(tmp_path, coarsen):
    """Lat slightly finer, lon slightly coarser — must not ping-pong to downscale."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.05)),
            lons=tuple(np.arange(10.0, 10.20, 0.05)),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.0499)),
            lons=tuple(np.arange(10.0, 10.20, 0.0501)),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref))
    assert Path(out).exists()


def test_coarsen_equal_resolution_offset_realigns(tmp_path, coarsen):
    """--target-resolution equal to input with a new --offset is a lateral shift."""
    src = write_zarr(
        make_gridded(
            lats=(0.0, 0.05, 0.10),
            lons=(10.0, 10.05, 10.10),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "0.05",
        "--offset",
        "0.025",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert Path(out).exists()
    assert ds.sizes["longitude"] >= 1


def test_coarsen_requires_mode(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(coarsen, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
