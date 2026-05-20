# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
#   "dask",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch IMERG live precipitation and write a Rhiza Envelope Zarr."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import earthaccess
import xarray as xr

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}


def _resolve_version() -> str:
    """Return '<git_sha_or_unknown>+<skill_dir_hash>'. The git part comes
    from `git rev-parse HEAD` against the script's parent dir; falls back
    to 'unknown' when not resolvable. The hash part is sha256 of the
    enclosing skill directory's contents."""
    try:
        sha = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parent,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        sha = "unknown"
    h = hashlib.sha256()
    skill_dir = Path(__file__).resolve().parent.parent
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(skill_dir)).encode())
            h.update(p.read_bytes())
    return f"{sha}+{h.hexdigest()}"


def _load_history(zarr_path: Path) -> list:
    try:
        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
            return json.loads(raw) if raw else []
    except Exception:
        return []


def _cache_hit(out: Path, entry: dict) -> bool:
    """Return True if the zarr at `out` was produced by this same entry."""
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    last = history[0]
    return (
        last.get("skill") == entry["skill"]
        and last.get("args") == entry["args"]
        and last.get("input") == entry["input"]
    )


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
    for name in ("latitude", "lat", "y"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "latitude")
            ds[name].attrs.setdefault("units", "degrees_north")
            ds[name].attrs.setdefault("axis", "Y")
            break
    for name in ("longitude", "lon", "x"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "longitude")
            ds[name].attrs.setdefault("units", "degrees_east")
            ds[name].attrs.setdefault("axis", "X")
            break
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--version", default="late", choices=list(SHORTNAMES))
    args = p.parse_args()

    shortname = SHORTNAMES[args.version]
    entry = {
        "skill": "imerg-fetch",
        "version": _resolve_version(),
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(
        f"Fetching IMERG {args.version} ({shortname}) {args.start} -> {args.end}",
        file=sys.stderr,
    )

    earthaccess.login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(args.start, args.end),
    )
    if not results:
        raise RuntimeError(f"No IMERG {args.version} granules found in {args.start}..{args.end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(
            files,
            engine="h5netcdf",
            combine="by_coords",
        )
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        # CMR's temporal filter is overlap-based and can return granules just
        # outside [start, end]; trim to exact requested bounds to match the
        # prior sheerwater @timeseries() post-process.
        ds = ds.sel(time=slice(args.start, args.end))
        ds = ds.drop_attrs()
        ds.attrs.update(
            rhiza_source="imerg",
            rhiza_history=json.dumps([entry], sort_keys=True),
        )
        ds["precip"].attrs.update(
            units="mm/day",
            standard_name="lwe_precipitation_rate",
            long_name="IMERG daily precipitation",
        )
        _stamp_cf_attrs(ds)
        for v in ds.variables:
            ds[v].encoding = {}

        # Materialize before the temp dir vanishes — open_mfdataset is lazy.
        ds.load().to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
