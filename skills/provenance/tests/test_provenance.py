"""Correctness tests for provenance."""

import pytest
from conftest import load_skill, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def clip_region():
    return load_skill("clip-region", "clip").clip_region


@pytest.fixture(scope="module")
def provenance():
    return load_skill("provenance", "provenance").provenance


def _stamped_zarr(tmp_path, clip_region):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "3/10/0/13")
    return out


def test_check_valid_history(tmp_path, clip_region, provenance, capsys):
    out = _stamped_zarr(tmp_path, clip_region)

    run_skill(provenance, "-i", str(out), "--check")

    captured = capsys.readouterr().out
    assert "valid weather_skills_history" in captured


def test_human_format_lists_clip_region(tmp_path, clip_region, provenance, capsys):
    out = _stamped_zarr(tmp_path, clip_region)

    run_skill(provenance, "-i", str(out), "--format", "human")

    captured = capsys.readouterr().out
    assert "clip-region" in captured
