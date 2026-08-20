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
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
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
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
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


_MOUNT_KENYA_HIT = [
    {
        "display_name": "Mount Kenya, Kenya",
        "name": "Mount Kenya",
        "boundingbox": ["-0.25", "-0.05", "37.2", "37.4"],
        "lat": "-0.15",
        "lon": "37.3",
        "geojson": {
            "type": "Polygon",
            "coordinates": [
                [
                    [37.2, -0.25],
                    [37.4, -0.25],
                    [37.4, -0.05],
                    [37.2, -0.05],
                    [37.2, -0.25],
                ]
            ],
        },
    }
]


def test_landmark_falls_through_to_nominatim(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_nominatim",
        lambda query: _MOUNT_KENYA_HIT,
    )
    _nominatim_collection.cache_clear()

    run_skill(resolve_mod.resolve_region, "Mount Kenya, Kenya")

    captured = capsys.readouterr()
    n, w, s, e = (float(x) for x in captured.out.strip().split("/"))
    assert (n, w, s, e) == pytest.approx((-0.05, 37.2, -0.25, 37.4))
    assert "nominatim: Mount Kenya, Kenya" in captured.err


def test_unknown_admin_key_does_not_geocode(resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
    )
    _admin_collection.cache_clear()

    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for admin typo; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_mod.resolve_region, "kenya-nairbi")
    assert exc.value.code == 1


def test_unknown_iso3_does_not_geocode(resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for ISO3; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_region, "ZZZ")
    assert exc.value.code == 1


def test_east_africa_prints_ne_subregion_bbox(capsys, resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for East Africa; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    run_skill(resolve_region, "East Africa")
    n, w, s, e = (float(x) for x in capsys.readouterr().out.strip().split("/"))
    assert s < -20
    assert n > 10
    assert w < 30 < e
    assert w < 38 < e


def test_eastern_africa_matches_east_africa(capsys, resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for Eastern Africa; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    run_skill(resolve_region, "East Africa")
    east = capsys.readouterr().out.strip()
    run_skill(resolve_region, "Eastern Africa")
    eastern = capsys.readouterr().out.strip()
    assert east == eastern


def test_landmark_geojson_write(tmp_path, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_nominatim",
        lambda query: _MOUNT_KENYA_HIT,
    )
    _nominatim_collection.cache_clear()

    geo = tmp_path / "mount-kenya.geojson"
    run_skill(resolve_mod.resolve_region, "Mount Kenya", "--geojson", str(geo))

    data = json.loads(geo.read_text())
    props = data["features"][0]["properties"]
    assert props["level"] == "nominatim"
    assert props["name"] == "Mount Kenya"
    assert data["features"][0]["geometry"]["type"] == "Polygon"
