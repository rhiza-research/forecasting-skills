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
                "coordinates": [[[10.5, 0.5], [12.5, 0.5], [12.5, 2.5], [10.5, 2.5], [10.5, 0.5]]],
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


def _tahmo_like():
    """TAHMO-style point_obs: station_id + time, unsorted lat/lon coords."""
    import numpy as np

    times = np.array(["2026-08-19", "2026-08-20"], dtype="datetime64[ns]")
    ds = xr.Dataset(
        {"precip": (("time", "station_id"), np.ones((2, 3)))},
        coords={
            "time": times,
            "station_id": ["TA00001", "TA00002", "TA00003"],
            "latitude": ("station_id", [1.28, -4.04, 0.51]),
            "longitude": ("station_id", [36.82, 39.67, 34.77]),
            "name": ("station_id", ["Nairobi", "Mombasa", "Kisumu"]),
        },
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    ds["time"].attrs.update(standard_name="time", axis="T")
    ds["station_id"].attrs.update(cf_role="timeseries_id")
    return ds


def test_clip_bbox_tahmo_stations(tmp_path, clip_region):
    src = write_zarr(_tahmo_like(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    # Mombasa city bbox (N/W/S/E); only TA00002 sits inside.
    run_skill(
        clip_region,
        "-i",
        str(src),
        "-o",
        str(out),
        "--bbox",
        "-3.9187/39.5667/-4.1550/39.7639",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.station_id.values) == ["TA00002"]
    assert ds["name"].values[0] == "Mombasa"
    assert ds.sizes["time"] == 2
    assert load_history(out)[-1]["skill"] == "clip-region"


def test_clip_bbox_point_id_obs(tmp_path, clip_region):
    from conftest import make_point_obs

    src = write_zarr(make_point_obs(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "1.6/9.5/0.5/10.6")
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.point_id.values) == ["S0"]


def test_clip_geojson_tahmo_stations(tmp_path, clip_region):
    src = write_zarr(_tahmo_like(), tmp_path / "in.zarr")
    geo = tmp_path / "mombasa.geojson"
    geo.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [39.56, -4.16],
                        [39.77, -4.16],
                        [39.77, -3.91],
                        [39.56, -3.91],
                        [39.56, -4.16],
                    ]
                ],
            }
        )
    )
    out = tmp_path / "out.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(out), "--geojson", str(geo))
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.station_id.values) == ["TA00002"]
