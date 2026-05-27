# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "matplotlib",
#   "numpy",
#   "xarray",
#   "zarr",
# ]
# ///
"""Render a multi-input timeseries PNG from one or more Rhiza Envelope Zarrs.

Each input contributes one 1D line trace on a shared set of axes, plotted
against its time-like coord. Inputs whose selected variable is not already
1D must list the dims to reduce via repeated --reduce flags; reductions are
mean-only and explicit (no silent averaging).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.2"


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


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


def _pick_time_dim(da, override):
    if override:
        if override not in da.dims:
            raise ValueError(f"--time-dim '{override}' not in dims {list(da.dims)}.")
        return override
    if "time" in da.dims:
        return "time"
    if "step" in da.dims:
        return "step"
    cf = _cf_dim(da, "time")
    if cf and cf in da.dims:
        return cf
    raise ValueError(
        f"Could not identify a time-like dim in {list(da.dims)}; pass --time-dim explicitly."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        "-i",
        action="append",
        required=True,
        help="Input Zarr; repeat for each input. Order is preserved in the legend.",
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument("--time-dim")
    p.add_argument(
        "--reduce",
        action="append",
        default=[],
        help="Name of a non-time dim to mean-reduce before plotting. Repeatable.",
    )
    p.add_argument("--title")
    args = p.parse_args()

    # PNG metadata keys are lettered by CLI position (rhiza_history_a,
    # _b, ..., _z). The scheme stops at z; reject more inputs early so
    # users see a clear error rather than a KeyError later.
    if len(args.input) > 26:
        print(
            f"Error: --input must be passed at most 26 times; got {len(args.input)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import xarray as xr

    for pth in args.input:
        if not Path(pth).exists():
            print(f"Error: {pth} not found.", file=sys.stderr)
            sys.exit(2)

    datasets = [xr.open_zarr(pth, consolidated=False) for pth in args.input]

    variable = args.variable or (list(datasets[0].data_vars)[0] if datasets[0].data_vars else None)
    if variable is None:
        print(
            f"Error: no usable variable in {args.input[0]}.",
            file=sys.stderr,
        )
        sys.exit(2)
    for pth, ds in zip(args.input, datasets, strict=True):
        if variable not in ds:
            print(
                f"Error: variable '{variable}' missing from {pth}. Available: {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)

    fig, ax = plt.subplots(figsize=(10, 6))
    units = None
    first_tdim = None

    for pth, ds in zip(args.input, datasets, strict=True):
        da = ds[variable]
        try:
            tdim = _pick_time_dim(da, args.time_dim)
        except ValueError as exc:
            print(f"Error ({pth}): {exc}", file=sys.stderr)
            sys.exit(2)

        applicable = [d for d in args.reduce if d in da.dims]
        if applicable:
            da = da.mean(applicable, keep_attrs=True)

        extras = [d for d in da.dims if d != tdim]
        if extras:
            print(
                f"Error ({pth}): variable '{variable}' still has non-time dims "
                f"{extras} after --reduce. Pass --reduce <dim> for each.",
                file=sys.stderr,
            )
            sys.exit(2)

        label = Path(pth).stem
        ax.plot(da[tdim].values, da.values, label=label)

        if units is None:
            units = da.attrs.get("units")
        if first_tdim is None:
            first_tdim = tdim

    ax.set_xlabel(first_tdim or "time")
    ylabel = variable if not units else f"{variable} [{units}]"
    ax.set_ylabel(ylabel)
    if args.title:
        ax.set_title(args.title)
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.5)

    fig.autofmt_xdate()
    fig.tight_layout()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    args_dict = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    png_metadata: dict[str, str] = {"Software": "forecasting-skills"}
    for idx, pth in enumerate(args.input):
        src = Path(pth)
        upstream = _load_history(src)
        entry = {
            "skill": "plot-timeseries",
            "version": _RHIZA_SKILL_VERSION,
            "args": args_dict,
            "input": {"basename": src.name, "hash": _hash_zarr(src)},
        }
        if not upstream:
            print(
                f"Warning: no upstream rhiza_history on {src.name}; "
                "embedding plot-timeseries step alone.",
                file=sys.stderr,
            )
        letter = chr(ord("a") + idx)
        png_metadata[f"rhiza_history_{letter}"] = json.dumps(upstream + [entry], sort_keys=True)

    fig.savefig(out, dpi=150, metadata=png_metadata)
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
