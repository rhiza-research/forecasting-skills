"""Correctness tests for plot-compare."""

from pathlib import Path

import pytest
from conftest import load_skill, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def plot_compare():
    return load_skill("plot-compare", "plot_compare").plot_compare


def test_two_gridded_inputs_write_png(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "cmp.png"

    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--panels",
        "2",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
