# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "xarray-regrid",
#   "zarr",
#   "numpy",
# ]
# ///
"""Linear regridding for Rhiza Envelope Zarr stores, with optional post-regrid q-q mapping."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"


def _hash_zarr(zarr_path: Path) -> str:
    """Stable content hash of a zarr's stored bytes. Walks the zarr dir
    deterministically and hashes relative-path bytes + each file's
    content. Returns sha256 hex digest."""
    h = hashlib.sha256()
    for p in sorted(zarr_path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(zarr_path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
            return json.loads(raw) if raw else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _cache_hit(out: Path, upstream: list, entry: dict) -> bool:
    """Cache check that compares everything except input.hash.

    The hash over the upstream zarr is expensive; the basename + upstream
    history chain is sufficient to identify whether a recompute is needed.
    """
    if not out.exists():
        return False
    history = _load_history(out)
    if len(history) != len(upstream) + 1:
        return False
    if history[:-1] != upstream:
        return False
    last = history[-1]
    last_input = last.get("input") or {}
    entry_input = entry.get("input") or {}
    return (
        last.get("skill") == entry["skill"]
        and last.get("version") == entry["version"]
        and last.get("args") == entry["args"]
        and last_input.get("basename") == entry_input.get("basename")
    )


def _grid_spacing(ds, dim):
    import numpy as np

    coord = ds[dim].values
    if coord.size < 2:
        raise ValueError(f"Cannot infer spacing for dim '{dim}' with size {coord.size}")
    return float(abs(np.median(np.diff(coord))))


def _factor_from_target(ds, lat_dim, lon_dim, target_res):
    lat_res = _grid_spacing(ds, lat_dim)
    lon_res = _grid_spacing(ds, lon_dim)
    if abs(lat_res - lon_res) / max(lat_res, lon_res) > 0.05:
        print(
            f"Warning: anisotropic input grid ({lat_res:.4f}° vs {lon_res:.4f}°); "
            f"using mean for factor.",
            file=sys.stderr,
        )
    mean_res = (lat_res + lon_res) / 2
    factor = round(target_res / mean_res)
    if factor < 2:
        print(
            f"Error: --target-resolution {target_res}° is not coarser than input "
            f"grid (~{mean_res:.4f}°).",
            file=sys.stderr,
        )
        sys.exit(2)
    return factor


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin = float(np.min(coord))
    vmax = float(np.max(coord))
    n = int(np.floor((vmax - vmin) / new_spacing)) + 1
    target = vmin + new_spacing * np.arange(n)
    if coord[0] > coord[-1]:
        target = target[::-1]
    return target


def _empirical_qmap_1d(model, ref):
    import numpy as np

    out = np.full(model.shape, np.nan, dtype=float)
    m_valid = ~np.isnan(model)
    r_valid = ~np.isnan(ref)
    if not m_valid.any() or not r_valid.any():
        return out
    sorted_ref = np.sort(ref[r_valid])
    sorted_model = np.sort(model[m_valid])
    n_m = sorted_model.size
    n_r = sorted_ref.size
    ranks = np.searchsorted(sorted_model, model[m_valid], side="right")
    quants = np.clip((ranks - 0.5) / n_m, 0.0, 1.0)
    ref_q = (np.arange(n_r) + 0.5) / n_r
    out[m_valid] = np.interp(quants, ref_q, sorted_ref)
    return out


def _qmap_dataarray(model_da, ref_da, time_dim):
    import xarray as xr

    ref_renamed = ref_da.rename({time_dim: "_qq_ref_time"})
    return xr.apply_ufunc(
        _empirical_qmap_1d,
        model_da,
        ref_renamed,
        input_core_dims=[[time_dim], ["_qq_ref_time"]],
        output_core_dims=[[time_dim]],
        vectorize=True,
        output_dtypes=[float],
    )


def _coords_match(a, b, atol=1e-6):
    import numpy as np

    if a.shape != b.shape:
        return False
    return bool(np.allclose(a, b, atol=atol, rtol=0))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--factor", "-f", type=int)
    grp.add_argument("--target-resolution", type=float, help="Target grid spacing in degrees")
    p.add_argument("--dims", help="Override as LAT,LON dim names")
    p.add_argument("--variable", "-v", help="Restrict to a single data variable")
    p.add_argument(
        "--qq-reference",
        help=(
            "Optional path to a reference Zarr. When given, applies empirical "
            "quantile mapping per grid cell along --time-dim, mapping the "
            "regridded values to the reference distribution. Reference must be "
            "on the post-regrid lat/lon grid."
        ),
    )
    p.add_argument(
        "--time-dim",
        default="time",
        help="Time dimension used as the sample axis for q-q mapping (default: time).",
    )
    args = p.parse_args()

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    src = Path(args.input)
    partial_entry = {
        "skill": "downscale",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping downscale.",
            file=sys.stderr,
        )
        return

    # Cache miss: build the final entry (hashes the upstream zarr).
    entry = {
        **partial_entry,
        "input": {
            "basename": src.name,
            "hash": _hash_zarr(src),
        },
    }

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import xarray as xr
    import xarray_regrid  # noqa: F401 — registers the .regrid accessor

    if not src.exists():
        print(f"Error: input {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.dims:
        lat_dim, lon_dim = [s.strip() for s in args.dims.split(",")]
        if lat_dim not in ds.dims or lon_dim not in ds.dims:
            print(
                f"Error: --dims names not in dataset dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        try:
            lat_dim = ds.cf["latitude"].name
            lon_dim = ds.cf["longitude"].name
        except KeyError:
            print(
                f"Error: could not identify lat/lon coords via CF metadata or name "
                f"heuristics in {list(ds.coords)}. Pass --dims to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    factor = (
        args.factor
        if args.factor is not None
        else _factor_from_target(ds, lat_dim, lon_dim, args.target_resolution)
    )
    if factor < 2:
        print("Error: factor must be >= 2.", file=sys.stderr)
        sys.exit(2)

    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[[args.variable]]

    new_lat = _target_coord(ds[lat_dim].values, factor * _grid_spacing(ds, lat_dim))
    new_lon = _target_coord(ds[lon_dim].values, factor * _grid_spacing(ds, lon_dim))
    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Regridding {lat_dim},{lon_dim} by factor {factor} (linear): "
        f"{ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> {len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    out_ds = ds.regrid.linear(target)

    out_ds.attrs = dict(ds.attrs)

    if args.qq_reference:
        ref_path = Path(args.qq_reference)
        if not ref_path.exists():
            print(f"Error: --qq-reference {ref_path} not found.", file=sys.stderr)
            sys.exit(2)
        ref_ds = xr.open_zarr(ref_path, consolidated=False)
        time_dim = args.time_dim
        if time_dim not in out_ds.dims:
            print(
                f"Error: regridded output has no '{time_dim}' dim "
                f"(have {list(out_ds.dims)}); pass --time-dim.",
                file=sys.stderr,
            )
            sys.exit(2)
        if time_dim not in ref_ds.dims:
            print(
                f"Error: --qq-reference has no '{time_dim}' dim "
                f"(have {list(ref_ds.dims)}); pass --time-dim.",
                file=sys.stderr,
            )
            sys.exit(2)
        for d in (lat_dim, lon_dim):
            if d not in ref_ds.dims:
                print(
                    f"Error: --qq-reference missing '{d}' dim (have {list(ref_ds.dims)}).",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not _coords_match(out_ds[d].values, ref_ds[d].values):
                print(
                    f"Error: --qq-reference '{d}' coords do not match the "
                    f"regridded grid. Reference must be on the post-regrid lat/lon.",
                    file=sys.stderr,
                )
                sys.exit(2)
        shared = [v for v in out_ds.data_vars if v in ref_ds.data_vars]
        if not shared:
            print(
                f"Error: --qq-reference shares no variables with the regridded "
                f"output (out: {list(out_ds.data_vars)}, ref: {list(ref_ds.data_vars)}).",
                file=sys.stderr,
            )
            sys.exit(2)
        for v in out_ds.data_vars:
            if v not in shared:
                print(
                    f"Warning: variable '{v}' not in --qq-reference; passing through unmapped.",
                    file=sys.stderr,
                )
        print(
            f"Q-Q mapping (empirical) {shared} along '{time_dim}': "
            f"model n={out_ds.sizes[time_dim]}, ref n={ref_ds.sizes[time_dim]}",
            file=sys.stderr,
        )
        ref_aligned = ref_ds.assign_coords({lat_dim: out_ds[lat_dim], lon_dim: out_ds[lon_dim]})
        for v in shared:
            mapped = _qmap_dataarray(out_ds[v], ref_aligned[v], time_dim)
            mapped.attrs = dict(out_ds[v].attrs)
            out_ds[v] = mapped

    if not upstream:
        print(
            "Warning: no upstream rhiza_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    out_ds.attrs["rhiza_history"] = json.dumps(upstream + [entry], sort_keys=True)
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
