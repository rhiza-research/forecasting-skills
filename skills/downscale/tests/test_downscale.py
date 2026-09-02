"""Correctness tests for downscale."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.standard_utils import normalize_latlon_coords
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def downscale():
    return load_skill("downscale", "downscale").downscale


def test_downscale_linear_factor2(tmp_path, downscale):
    src = write_zarr(
        make_gridded(lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "-f",
        "2",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] > 2
    assert ds.sizes["longitude"] > 2
    assert units_equal(ds["precip"].attrs.get("units"), "mm day-1")
    assert load_history(out)[-1]["skill"] == "downscale"


def test_downscale_requires_one_target(tmp_path, downscale):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(src),
            "-o",
            str(out),
            "--algorithm",
            "linear-interpolation",
        )
    assert exc.value.code == 2


def test_downscale_rejects_coarser_reference(tmp_path, downscale):
    fine = write_zarr(
        make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=1.0),
        tmp_path / "fine.zarr",
    )
    coarse = write_zarr(
        make_gridded(lats=(0.0, 2.0), lons=(10.0, 12.0), fill=1.0),
        tmp_path / "coarse.zarr",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(fine),
            "-o",
            str(tmp_path / "out.zarr"),
            "--algorithm",
            "linear-interpolation",
            "--reference-grid",
            str(coarse),
        )
    assert exc.value.code == 2


def test_downscale_reference_grid_allows_lateral_shift(tmp_path, downscale):
    """Same 0.05° spacing, half-cell lon offset — do not bounce to coarsen."""
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
    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "--reference-grid",
        str(ref),
    )
    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


def test_downscale_reference_grid_allows_near_equal_mixed_axes(tmp_path, downscale):
    """Lat slightly finer, lon slightly coarser — must not ping-pong to coarsen."""
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
    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "--reference-grid",
        str(ref),
    )
    assert Path(out).exists()
