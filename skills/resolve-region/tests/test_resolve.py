"""Correctness tests for resolve-region."""

import json
from pathlib import Path

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def resolve_region():
    return load_skill("resolve-region", "resolve").resolve_region


def test_ken_prints_bbox(capsys, resolve_region):
    run_skill(resolve_region, "KEN")

    line = capsys.readouterr().out.strip()
    n, w, s, e = (float(x) for x in line.split("/"))
    assert n > s
    assert -180 <= w <= 180
    assert -180 <= e <= 180


def test_geojson_write(tmp_path, resolve_region):
    geo = tmp_path / "ken.geojson"

    run_skill(resolve_region, "KEN", "--geojson", str(geo))

    assert Path(geo).exists()
    data = json.loads(geo.read_text())
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["properties"]["iso3"] == "KEN"
