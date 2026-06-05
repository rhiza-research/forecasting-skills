# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "xarray-regrid",
#   "zarr",
#   "numpy",
# ]
# ///
"""Downscale a Rhiza Envelope Zarr onto a finer grid via a chosen method."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.3"


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
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


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
        # Secondary references (--reference-grid, --qq-reference) are
        # content-hashed and compared so an in-place change to a reference
        # forces a recompute. Absent on entries that supplied no reference.
        and last.get("reference_inputs") == entry.get("reference_inputs")
    )


def _grid_spacing(ds, dim):
    import numpy as np

    coord = ds[dim].values
    if coord.size < 2:
        raise ValueError(f"Cannot infer spacing for dim '{dim}' with size {coord.size}")
    return float(abs(np.median(np.diff(coord))))


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
    p.add_argument(
        "--method",
        choices=["linear-interpolation", "q-q"],
        required=True,
        help=(
            "How to add information when going finer. 'linear-interpolation' "
            "linearly interpolates onto the finer grid; 'q-q' interpolates and "
            "then empirically quantile-maps onto a distribution reference."
        ),
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--factor",
        "-f",
        type=int,
        help="Integer refinement factor (>= 1). New spacing = input spacing / factor.",
    )
    grp.add_argument(
        "--target-resolution",
        type=float,
        help="Target grid spacing in degrees. Must be finer-or-equal (<=) to the input.",
    )
    grp.add_argument(
        "--reference-grid",
        help=(
            "Path to a reference Zarr whose lat/lon grid defines the finer "
            "target. The reference grid must be finer-or-equal to the input."
        ),
    )
    p.add_argument("--dims", help="Override as LAT,LON dim names")
    p.add_argument("--variable", "-v", help="Restrict to a single data variable")
    p.add_argument(
        "--qq-reference",
        help=(
            "Reference Zarr whose distribution the q-q method maps the output "
            "onto. Empirical quantile mapping per grid cell along --time-dim. "
            "The reference must already be on the post-downscale lat/lon grid. "
            "Required for --method q-q."
        ),
    )
    p.add_argument(
        "--time-dim",
        default="time",
        help="Time dimension used as the sample axis for q-q mapping (default: time).",
    )
    args = p.parse_args()

    if args.factor is not None and args.factor < 1:
        print("Error: --factor must be >= 1.", file=sys.stderr)
        sys.exit(2)
    if args.target_resolution is not None and args.target_resolution <= 0:
        print("Error: --target-resolution must be > 0.", file=sys.stderr)
        sys.exit(2)
    if args.method == "q-q" and not args.qq_reference:
        print(
            "Error: --method q-q requires --qq-reference (the distribution reference to map onto).",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.qq_reference and args.method != "q-q":
        print(
            "Error: --qq-reference is only valid with --method q-q.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Build the cheap fields first; defer the expensive _hash_zarr over the main
    # --input until after the cache-hit check so we don't hash hundreds of MB of
    # zarr on hits. Secondary references (--reference-grid, --qq-reference) ARE
    # hashed up front when supplied — they're typically far smaller, and their
    # content must enter the cache key so an in-place edit forces a recompute
    # (the main input's hash deliberately does NOT enter the key; see _cache_hit).
    src = Path(args.input)
    reference_inputs = []
    for flag, ref in (
        ("--reference-grid", args.reference_grid),
        ("--qq-reference", args.qq_reference),
    ):
        if ref:
            ref_p = Path(ref)
            if not ref_p.exists():
                print(f"Error: {flag} {ref_p} not found.", file=sys.stderr)
                sys.exit(2)
            reference_inputs.append({"basename": ref_p.name, "hash": _hash_zarr(ref_p)})
    partial_entry = {
        "skill": "downscale",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    if reference_inputs:
        partial_entry["reference_inputs"] = reference_inputs
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
    import numpy as np
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

    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[[args.variable]]

    in_lat_res = _grid_spacing(ds, lat_dim)
    in_lon_res = _grid_spacing(ds, lon_dim)

    # Build the finer target grid from one of the three mutually-exclusive specs.
    if args.reference_grid is not None:
        ref_grid_path = Path(args.reference_grid)
        ref_grid_ds = xr.open_zarr(ref_grid_path, consolidated=False)
        for d in (lat_dim, lon_dim):
            if d not in ref_grid_ds.dims:
                print(
                    f"Error: --reference-grid missing '{d}' dim (have {list(ref_grid_ds.dims)}).",
                    file=sys.stderr,
                )
                sys.exit(2)
        new_lat = np.asarray(ref_grid_ds[lat_dim].values)
        new_lon = np.asarray(ref_grid_ds[lon_dim].values)
        ref_lat_res = _grid_spacing(ref_grid_ds, lat_dim)
        ref_lon_res = _grid_spacing(ref_grid_ds, lon_dim)
        if ref_lat_res > in_lat_res or ref_lon_res > in_lon_res:
            print(
                f"Error: --reference-grid is coarser than the input "
                f"(input ~{in_lat_res:.4f}°x{in_lon_res:.4f}°, reference "
                f"~{ref_lat_res:.4f}°x{ref_lon_res:.4f}°). Downscaling goes "
                f"finer-or-equal; to coarsen onto a coarser grid use the "
                f"coarsen skill.",
                file=sys.stderr,
            )
            sys.exit(2)
        target_desc = f"reference grid {ref_grid_path.name}"
    else:
        if args.factor is not None:
            lat_spacing = in_lat_res / args.factor
            lon_spacing = in_lon_res / args.factor
            target_desc = f"factor {args.factor}"
        else:
            # --target-resolution: the requested spacing applies to both axes.
            # Require it to be finer-or-equal to BOTH input axis spacings, so a
            # value finer than one axis but coarser than the other is still rejected.
            if args.target_resolution > in_lat_res or args.target_resolution > in_lon_res:
                print(
                    f"Error: --target-resolution {args.target_resolution}° is "
                    f"coarser than the input on at least one axis "
                    f"(~{in_lat_res:.4f}°x{in_lon_res:.4f}°). "
                    f"Downscaling goes finer-or-equal; to coarsen onto a coarser "
                    f"grid use the coarsen skill.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if abs(in_lat_res - in_lon_res) / max(in_lat_res, in_lon_res) > 0.05:
                print(
                    f"Warning: input grid is anisotropic "
                    f"(~{in_lat_res:.4f}°x{in_lon_res:.4f}°); the single "
                    f"--target-resolution {args.target_resolution}° is applied to "
                    f"both axes.",
                    file=sys.stderr,
                )
            lat_spacing = args.target_resolution
            lon_spacing = args.target_resolution
            target_desc = f"target-resolution {args.target_resolution}°"
        new_lat = _target_coord(ds[lat_dim].values, lat_spacing)
        new_lon = _target_coord(ds[lon_dim].values, lon_spacing)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Downscaling {lat_dim},{lon_dim} (method={args.method}, "
        f"{target_desc}): {ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> "
        f"{len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    out_ds = ds.regrid.linear(target)

    out_ds.attrs = dict(ds.attrs)

    if args.method == "q-q":
        ref_path = Path(args.qq_reference)
        ref_ds = xr.open_zarr(ref_path, consolidated=False)
        time_dim = args.time_dim
        if time_dim not in out_ds.dims:
            print(
                f"Error: downscaled output has no '{time_dim}' dim "
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
                    f"downscaled grid. Reference must be on the post-downscale lat/lon.",
                    file=sys.stderr,
                )
                sys.exit(2)
        shared = [v for v in out_ds.data_vars if v in ref_ds.data_vars]
        if not shared:
            print(
                f"Error: --qq-reference shares no variables with the downscaled "
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
