"""Correctness tests for plot-timeseries."""

from pathlib import Path

import pytest
from conftest import load_skill, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def plot_timeseries():
    return load_skill("plot-timeseries", "plot_timeseries").plot_timeseries


def test_single_input_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_reduce_spatial_dims(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--title",
        "Area mean",
    )

    assert Path(out).exists()
