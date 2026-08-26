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


def test_precip_shared_scale_is_discrete_kenya_palette():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    plot_mod = load_skill("plot-compare", "plot_compare")
    cmap, norm = plot_mod._precip_scale()
    assert isinstance(cmap, ListedColormap)
    assert cmap.name == "wgbrp"
    assert cmap.N == 10
    assert cmap(0.0)[:3] == pytest.approx((1.0, 1.0, 1.0), abs=0.02)
    assert isinstance(norm, BoundaryNorm)
    assert list(norm.boundaries) == pytest.approx(plot_mod.PRECIP_BOUNDS)


def test_parse_colormap_accepts_comma_separated_colors():
    from matplotlib.colors import LinearSegmentedColormap

    plot_mod = load_skill("plot-compare", "plot_compare")
    assert plot_mod._parse_colormap(None) is None
    assert plot_mod._parse_colormap("magma") == "magma"
    cmap = plot_mod._parse_colormap("white,wheat,green")
    assert isinstance(cmap, LinearSegmentedColormap)
    assert cmap.name == "custom"


def test_custom_color_list_writes_png(tmp_path, plot_compare):
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
        "--colormap",
        "white,wheat,green",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
