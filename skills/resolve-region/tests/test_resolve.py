"""Correctness tests for resolve-region."""

import json
from pathlib import Path

import pytest
from conftest import load_skill, run_skill

_NAIROBI = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "shapeName": "Nairobi",
                "shapeGroup": "KEN",
                "shapeType": "ADM1",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [36.6, -1.4],
                        [37.0, -1.4],
                        [37.0, -1.1],
                        [36.6, -1.1],
                        [36.6, -1.4],
                    ]
                ],
            },
        }
    ],
}


@pytest.fixture(scope="module")
def resolve_mod():
    return load_skill("resolve-region", "resolve")


@pytest.fixture
def resolve_region(resolve_mod):
    return resolve_mod.resolve_region


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


def test_lowercase_iso3_is_usage_error(resolve_region):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_region, "ken")
    assert exc.value.code == 2


def test_county_prints_bbox(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []},
    )
    _admin_collection.cache_clear()

    run_skill(resolve_mod.resolve_region, "kenya-nairobi")

    line = capsys.readouterr().out.strip()
    n, w, s, e = (float(x) for x in line.split("/"))
    assert n == pytest.approx(-1.1)
    assert s == pytest.approx(-1.4)
    assert w == pytest.approx(36.6)
    assert e == pytest.approx(37.0)


def test_county_geojson_write(tmp_path, resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []},
    )
    _admin_collection.cache_clear()

    geo = tmp_path / "nairobi.geojson"
    run_skill(resolve_mod.resolve_region, "KEN-nairobi", "--geojson", str(geo))

    data = json.loads(geo.read_text())
    props = data["features"][0]["properties"]
    assert props["iso3"] == "KEN"
    assert props["level"] == "admin_1"
    assert props["region_name"] == "kenya-nairobi"
    assert props["name"] == "Nairobi"
