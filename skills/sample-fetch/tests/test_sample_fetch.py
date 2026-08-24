"""Correctness tests for sample-fetch (bundled samples only, no network)."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

# The ten source strings the samples stamp, written out so deleting or renaming
# an asset fails the suite instead of shrinking what it checks.
SOURCES = {
    "arco-era5",
    "chirps",
    "cmip6:GFDL-CM4/ssp245/r1i1p1f1/day/tas/gr1",
    "dynamical:noaa-gfs-analysis",
    "ecmwf-s2s",
    "ghcn-daily",
    "imerg",
    "oisst",
    "smap",
    "tahmo",
}

# Spellings that resolve to a real asset under a naive path rule but are not what
# any fetcher stamps: case, separator, redundant segments, and multi-colon forms.
ALIASES = [
    "Chirps",
    "CHIRPS",
    "dynamical/noaa-gfs-analysis",
    "./chirps",
    "chirps/../chirps",
    "cmip6:GFDL-CM4:ssp245:r1i1p1f1:day:tas:gr1",
]

ESCAPES = ["../../etc/passwd", "/etc/passwd", "a/../../x"]


@pytest.fixture(scope="module")
def mod():
    return load_skill("sample-fetch", "sample_fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.sample_fetch


def _sentinel(path):
    path.write_text("sentinel")
    return path


def test_bundled_sources_match_the_documented_set(mod):
    assert set(mod._bundled_samples()) == SOURCES


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_asset_stamps_its_own_source_string(mod, source):
    asset = xr.open_zarr(mod._bundled_samples()[source], consolidated=True)
    assert asset.attrs["weather_skills_source"] == source


def test_colon_source_maps_to_a_nested_asset_path(mod):
    samples = mod._bundled_samples()
    assert (
        samples["dynamical:noaa-gfs-analysis"]
        == mod._ASSETS_DIR / "dynamical" / "noaa-gfs-analysis.zarr"
    )
    assert samples["cmip6:GFDL-CM4/ssp245/r1i1p1f1/day/tas/gr1"] == (
        mod._ASSETS_DIR / "cmip6/GFDL-CM4/ssp245/r1i1p1f1/day/tas/gr1.zarr"
    )


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_source_round_trips_through_the_skill(tmp_path, mod, fetch, source):
    out = tmp_path / "out.zarr"

    run_skill(fetch, "--source", source, "-o", str(out))

    # load() forces every chunk read, so a store missing chunk files fails here.
    written = xr.open_zarr(out, consolidated=True).load()
    asset = xr.open_zarr(mod._bundled_samples()[source], consolidated=True).load()
    xr.testing.assert_equal(asset, written)
    assert written.attrs["weather_skills_source"] == source
    assert load_history(out)[-1]["skill"] == "sample-fetch"


@pytest.mark.parametrize("source", sorted(SOURCES - {"smap", "ecmwf-s2s"}))
def test_data_var_attrs_survive_the_write(tmp_path, mod, fetch, source):
    out = tmp_path / "out.zarr"

    run_skill(fetch, "--source", source, "-o", str(out))

    written = xr.open_zarr(out, consolidated=True)
    asset = xr.open_zarr(mod._bundled_samples()[source], consolidated=True)
    for name in asset.data_vars:
        assert written[name].attrs == asset[name].attrs, name


def test_smap_units_are_normalized_on_write(tmp_path, fetch):
    out = tmp_path / "out.zarr"

    run_skill(fetch, "--source", "smap", "-o", str(out))

    assert xr.open_zarr(out, consolidated=True)["soil_moisture"].attrs["units"] == "cm3/cm3"


def test_ecmwf_precip_amount_gets_its_cf_standard_name(tmp_path, fetch):
    out = tmp_path / "out.zarr"

    run_skill(fetch, "--source", "ecmwf-s2s", "-o", str(out))

    tp = xr.open_zarr(out, consolidated=True)["tp"].attrs
    assert tp["standard_name"] == "lwe_thickness_of_precipitation_amount"
    assert tp["units"] == "kg m-2"


def test_unknown_source_exits_2_and_lists_available(tmp_path, capsys, mod, fetch):
    out = _sentinel(tmp_path / "out.zarr")

    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--source", "not-a-source", "-o", str(out))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not-a-source" in err
    for source in mod._bundled_samples():
        assert source in err
    assert out.read_text() == "sentinel"


@pytest.mark.parametrize("source", ESCAPES + ALIASES)
def test_source_outside_the_documented_set_is_refused(tmp_path, capsys, fetch, source):
    out = _sentinel(tmp_path / "out.zarr")

    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--source", source, "-o", str(out))

    assert exc.value.code == 2
    assert "no bundled sample for --source" in capsys.readouterr().err
    assert out.read_text() == "sentinel"


def test_empty_assets_directory_names_the_directory(tmp_path, monkeypatch, capsys, mod, fetch):
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(mod, "_ASSETS_DIR", assets)
    out = _sentinel(tmp_path / "out.zarr")

    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--source", "chirps", "-o", str(out))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no bundled samples found under" in err
    assert str(assets) in err
    assert Path(out).read_text() == "sentinel"
