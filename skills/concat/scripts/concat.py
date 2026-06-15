# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cftime",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Concatenate weather-skills envelope Zarr stores along a named dim."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"


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
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
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
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


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
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
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

    # Input-units guard. Concatenation places the inputs' values into a single
    # array under one set of attrs (the first input's, stamped below). If the
    # inputs hold the same variable in different units, the concatenated array
    # mixes incompatible numbers under one units label, producing objectively
    # wrong data. For each data variable common to all inputs, compare the
    # `units` attr across inputs; error when two inputs carry the variable in
    # differing units. Inputs that omit `units` for a variable are not a
    # violation (missing metadata can't be checked), so only present values
    # participate in the comparison. The check spans the union of all inputs'
    # data variables, so a variable that appears in only some inputs with
    # conflicting units is still caught; inputs lacking the variable are
    # skipped. Units are compared after stripping surrounding whitespace and
    # only when they are strings, so a trailing space is not read as a real
    # difference and a non-string attr does not break the comparison.
    all_vars = set()
    for ds in dss:
        all_vars |= set(ds.data_vars)
    for var in sorted(all_vars):
        seen_units = {}
        for ip, ds in zip(paths, dss, strict=True):
            if var not in ds.data_vars:
                continue
            u = ds[var].attrs.get("units")
            if not isinstance(u, str):
                continue
            seen_units[ip.name] = u.strip()
        if len(set(seen_units.values())) > 1:
            detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
            print(
                f"Error: variable '{var}' has differing units across the inputs "
                f"({detail}). Concatenation combines these inputs into one array "
                f"that carries a single units label, so values measured in "
                f"different units would be mixed together as if they were the "
                f"same quantity. Concatenation requires the inputs to express "
                f"'{var}' in one consistent unit.",
                file=sys.stderr,
            )
            sys.exit(2)

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
    # list of `{basename, hash, history}` dicts (schema extension over the
    # single-input zarr-transformers). Each item's `history` is that input's
    # full `weather_skills_history` chain (`[]` when the input had none), so the merge
    # records every input branch and the output is fully reproducible from its
    # own provenance. The output's top-level `weather_skills_history` stays a single
    # linear array — the trunk is the first input's chain plus this entry,
    # matching the attr-passthrough above — so every existing single-attr
    # reader keeps working.
    input_histories = [_load_history(ip) for ip in paths]
    upstream = input_histories[0]
    entry = {
        "skill": "concat",
        "version": _SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": [
            {"basename": ip.name, "hash": _hash_zarr(ip), "history": hist}
            for ip, hist in zip(paths, input_histories, strict=True)
        ],
    }
    out_ds.attrs["weather_skills_history"] = json.dumps(upstream + [entry], sort_keys=True)
    # compatibility migration for the rhiza_ attr prefix; scheduled for removal
    for _old in ("rhiza_history", "rhiza_source", "rhiza_forecast_init"):
        if _old in out_ds.attrs:
            _new = "weather_skills_" + _old.removeprefix("rhiza_")
            out_ds.attrs.setdefault(_new, out_ds.attrs.pop(_old))

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
