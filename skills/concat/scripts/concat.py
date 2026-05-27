# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Concatenate Rhiza Envelope Zarr stores along a named dim."""

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


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        "-i",
        action="append",
        required=True,
        help="Input Zarr (repeat the flag for each input; need at least 2)",
    )
    p.add_argument("--dim", required=True)
    p.add_argument("--coords", help="Comma-separated coord values for the new dim")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    import xarray as xr

    paths = [Path(s) for s in args.input]
    if len(paths) < 2:
        print("Error: need at least 2 inputs.", file=sys.stderr)
        sys.exit(2)
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"Error: missing inputs: {missing}", file=sys.stderr)
        sys.exit(2)

    dss = [xr.open_zarr(p, consolidated=False) for p in paths]
    dim_on_inputs = all(args.dim in ds.dims for ds in dss)

    if not dim_on_inputs:
        if args.coords:
            coord_vals = _coerce([c.strip() for c in args.coords.split(",")])
            if len(coord_vals) != len(dss):
                print(
                    f"Error: --coords len {len(coord_vals)} != inputs {len(dss)}",
                    file=sys.stderr,
                )
                sys.exit(2)
            dss = [d.expand_dims({args.dim: [v]}) for d, v in zip(dss, coord_vals, strict=True)]
        else:
            dss = [d.expand_dims(args.dim) for d in dss]

    out_ds = xr.concat(dss, dim=args.dim)
    out_ds.attrs = dict(dss[0].attrs)
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    # Provenance: concat is a multi-input op, so the entry's `input` is a
    # list of `{basename, hash}` dicts (schema extension over the single-
    # input zarr-transformers). The upstream chain is taken from the first
    # input's `rhiza_history` (matching the attr-passthrough above); if the
    # other inputs have non-empty histories that disagree with the first,
    # warn but proceed — concat is not in the business of reconciling
    # divergent provenance.
    upstream = _load_history(paths[0])
    other_histories = [_load_history(ip) for ip in paths[1:]]
    for ip, h in zip(paths[1:], other_histories, strict=True):
        if h and h != upstream:
            print(
                f"Warning: input {ip.name} has a rhiza_history that diverges from "
                f"{paths[0].name}; keeping the first input's chain on the output.",
                file=sys.stderr,
            )
    if not upstream:
        print(
            f"Warning: no upstream rhiza_history on input {paths[0].name}; "
            f"treating inputs as opaque.",
            file=sys.stderr,
        )
    entry = {
        "skill": "concat",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": [{"basename": ip.name, "hash": _hash_zarr(ip)} for ip in paths],
    }
    out_ds.attrs["rhiza_history"] = json.dumps(upstream + [entry], sort_keys=True)

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
