"""Correctness tests for clip-region."""

import json
from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def clip_region():
    return load_skill("clip-region", "clip").clip_region


def test_clip_bbox_subsets_and_stamps_history(tmp_path, clip_region):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "2.5/10.5/0.5/12.5")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.latitude.values) == [1.0, 2.0]
    assert list(ds.longitude.values) == [11.0, 12.0]
    assert ds.sizes["latitude"] < 3
    assert ds.sizes["longitude"] < 4
    assert load_history(out)[-1]["skill"] == "clip-region"


def test_clip_empty_bbox_exits_1(tmp_path, clip_region):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "50/10/40/12")
    assert exc.value.code == 1


def test_clip_kenya_bbox_from_resolve(tmp_path, clip_region):
    # Compose the same way an agent would: resolve-region library → --bbox.
    from weather_skills_core.region import bbox_from_geometry, lookup_region

    n, w, s, e = bbox_from_geometry(lookup_region("KEN")["geometry"])
    src = write_zarr(
        make_gridded(lats=(-5.0, 0.0, 5.0), lons=(34.0, 38.0, 42.0)),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", f"{n}/{w}/{s}/{e}")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] >= 1
    assert ds.sizes["longitude"] >= 1
    assert load_history(out)[-1]["args"]["bbox"] == f"{n}/{w}/{s}/{e}"


def test_clip_geojson_polygon(tmp_path, clip_region):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    geo = tmp_path / "box.geojson"
    # Polygon covering lon 10.5–12.5, lat 0.5–2.5 (cells at 11,12 and lat 1,2).
    geo.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [[10.5, 0.5], [12.5, 0.5], [12.5, 2.5], [10.5, 2.5], [10.5, 0.5]]
                ],
            }
        )
    )
    out = tmp_path / "out.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(out), "--geojson", str(geo))
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.latitude.values) == [1.0, 2.0]
    assert list(ds.longitude.values) == [11.0, 12.0]


def test_clip_accepts_precip_totals(tmp_path, clip_region):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "2.5/10.5/0.5/12.5")

    result = xr.open_zarr(out, consolidated=True)
    assert "sum" in result["precip"].attrs["cell_methods"]
    assert list(result.latitude.values) == [1.0, 2.0]
