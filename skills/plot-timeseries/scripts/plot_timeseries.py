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
import sys
from pathlib import Path


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _pick_time_dim(da, override):
    if override:
        if override not in da.dims:
            raise ValueError(
                f"--time-dim '{override}' not in dims {list(da.dims)}."
            )
        return override
    if "time" in da.dims:
        return "time"
    if "step" in da.dims:
        return "step"
    cf = _cf_dim(da, "time")
    if cf and cf in da.dims:
        return cf
    raise ValueError(
        f"Could not identify a time-like dim in {list(da.dims)}; "
        "pass --time-dim explicitly."
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

    variable = args.variable or (
        list(datasets[0].data_vars)[0] if datasets[0].data_vars else None
    )
    if variable is None:
        print(
            f"Error: no usable variable in {args.input[0]}.",
            file=sys.stderr,
        )
        sys.exit(2)
    for pth, ds in zip(args.input, datasets):
        if variable not in ds:
            print(
                f"Error: variable '{variable}' missing from {pth}. "
                f"Available: {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)

    fig, ax = plt.subplots(figsize=(10, 6))
    units = None
    first_tdim = None

    for pth, ds in zip(args.input, datasets):
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
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
