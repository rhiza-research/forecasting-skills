"""Seed offline workspaces with tiny standard-dataset Zarrs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from weather_skills_core.provenance import stamp_zarr


def _write(ds: xr.Dataset, path: Path, *, skill: str = "eval-fixture") -> None:
    path = Path(path)
    if path.exists():
        import shutil

        shutil.rmtree(path)
    stamp_zarr(
        ds,
        [{"skill": skill, "version": "0.0.0", "args": {"fixture": True}, "input": None}],
        source="evals-fixture",
    )
    ds.to_zarr(path, mode="w", consolidated=True)


def daily_rates(
    path: Path,
    *,
    n_time: int = 15,
    start: str = "2026-08-16",
    name: str = "precip",
    fill: float = 2.0,
    lats=(1.0, 2.0),
    lons=(36.0, 37.0),
) -> None:
    times = np.arange(
        np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D")
    )
    data = np.full((n_time, len(lats), len(lons)), fill, dtype=np.float64)
    ds = xr.Dataset(
        {name: (("time", "latitude", "longitude"), data)},
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )
    ds[name].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    _write(ds, path)


def cumulative_forecast(
    path: Path,
    *,
    n_step: int = 14,
    init: str = "2026-08-01",
    name: str = "tp",
    fill: float = 1.0,
) -> None:
    steps = np.array([np.timedelta64(d, "D") for d in range(1, n_step + 1)])
    # Cumulative-looking values: increase with step.
    data = np.cumsum(np.full((n_step, 2, 2), fill, dtype=np.float64), axis=0)
    ds = xr.Dataset(
        {name: (("step", "latitude", "longitude"), data)},
        coords={
            "time": np.datetime64(init, "ns"),
            "step": steps,
            "latitude": [1.0, 2.0],
            "longitude": [36.0, 37.0],
        },
    )
    ds[name].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        long_name="Total precipitation",
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    _write(ds, path, skill="eval-fixture-cumulative")


_KIND = {
    "daily_rates": daily_rates,
    "cumulative_forecast": cumulative_forecast,
}


def prepare_fixtures(workdir: Path, specs: list[dict]) -> list[Path]:
    """Materialize fixture specs from ``expect.json`` into ``workdir``."""
    written = []
    for spec in specs:
        kind = spec["kind"]
        if kind not in _KIND:
            raise ValueError(f"unknown fixture kind {kind!r}; have {sorted(_KIND)}")
        rel = spec.get("path") or spec.get("name")
        if not rel:
            raise ValueError(f"fixture spec needs path/name: {spec}")
        out = workdir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        kwargs = {k: v for k, v in spec.items() if k not in ("kind", "path", "name")}
        _KIND[kind](out, **kwargs)
        written.append(out)
    return written
