# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "xarray",
#   "zarr",
# ]
# ///
"""Render a multi-input timeseries PNG from one or more weather-skills envelope Zarrs.

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
_SKILL_VERSION = "0.1.10"


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _auto_variable(ds):
    """First real data var, skipping CF grid-mapping (CRS) containers.

    A CF grid-mapping variable (e.g. ``latitude_longitude``) is a zero-data
    CRS container: it carries a ``grid_mapping_name`` attr and is named by
    another var's ``grid_mapping`` attr. Skip those so a no-flag auto-pick
    lands on a real data var. Prefer a var with >= 2 dims, falling back to
    the first remaining candidate.
    """
    mapping_targets = {
        ds[d].attrs.get("grid_mapping") for d in ds.data_vars if ds[d].attrs.get("grid_mapping")
    }
    candidates = [
        v
        for v in ds.data_vars
        if "grid_mapping_name" not in ds[v].attrs and v not in mapping_targets
    ]
    if not candidates:
        return None
    multidim = [v for v in candidates if len(ds[v].dims) >= 2]
    return (multidim or candidates)[0]


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
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
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
    p.add_argument(
        "--align-day-of-year",
        action="store_true",
        help=(
            "Plot each trace against day-of-year (1-366) instead of its absolute "
            "date, so inputs from different years overlay on a shared x-axis. "
            "Requires a calendar-date time axis (errors on a non-date axis such "
            "as a forecast 'step' timedelta)."
        ),
    )
    args = p.parse_args()

    # PNG metadata keys are lettered by CLI position (weather_skills_history_a,
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
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter
    import numpy as np
    import xarray as xr

    for pth in args.input:
        if not Path(pth).exists():
            print(f"Error: {pth} not found.", file=sys.stderr)
            sys.exit(2)

    datasets = [xr.open_zarr(pth, consolidated=False) for pth in args.input]

    variable = args.variable or _auto_variable(datasets[0])
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

    # Input-units check. The traces share one y-axis whose label takes the
    # units of the first input. If the inputs hold the variable in different
    # units, traces measured in different units are drawn against a single
    # scale and labeled with only one of them, which misrepresents the data.
    # This only affects the rendering, so warn and proceed. Compare only the
    # inputs that carry the variable with a string `units` attr; an input that
    # lacks the variable is skipped, and a missing or non-string value can't be
    # checked. Units are compared after stripping surrounding whitespace so a
    # trailing space is not read as a real difference.
    seen_units = {}
    for pth, ds in zip(args.input, datasets, strict=True):
        if variable not in ds:
            continue
        u = ds[variable].attrs.get("units")
        if isinstance(u, str):
            seen_units[Path(pth).stem] = u.strip()
    if len(set(seen_units.values())) > 1:
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        print(
            f"Warning: variable '{variable}' has differing units across the "
            f"overlaid inputs ({detail}). The traces share one y-axis labeled "
            f"with a single unit, so lines in different units are not directly "
            f"comparable in this figure.",
            file=sys.stderr,
        )

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
        if args.align_day_of_year:
            # `.dt.dayofyear` works for datetime64 and object-dtype cftime time
            # coords; it raises TypeError/AttributeError on a non-calendar axis
            # (e.g. a forecast `step` timedelta), which we surface clearly.
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                print(
                    f"Error ({pth}): --align-day-of-year needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis (e.g. a forecast "
                    f"'step' timedelta). Drop the flag or pick a date dim with "
                    f"--time-dim.",
                    file=sys.stderr,
                )
                sys.exit(2)
            # A non-monotonic day-of-year sequence draws over itself on the
            # shared axis. This happens when a trace crosses a year boundary,
            # spans multiple years, or has an out-of-order time axis. Rendering
            # caveat only — warn and proceed.
            if len(xvals) > 1 and np.any(np.diff(xvals) < 0):
                print(
                    f"Warning ({pth}): day-of-year values are non-monotonic "
                    f"(decrease at some point — a trace crossing a year boundary, "
                    f"spanning multiple years, or an out-of-order time axis); "
                    f"rendering anyway, but it may overplot itself on the shared "
                    f"day-of-year axis.",
                    file=sys.stderr,
                )
        else:
            xvals = da[tdim].values
        ax.plot(xvals, da.values, label=label)

        if units is None:
            units = da.attrs.get("units")
        if first_tdim is None:
            first_tdim = tdim

    ax.set_xlabel("day of year" if args.align_day_of_year else (first_tdim or "time"))
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
            "version": _SKILL_VERSION,
            "args": args_dict,
            "input": {"basename": src.name, "hash": _hash_zarr(src)},
        }
        if not upstream:
            print(
                f"Warning: no upstream weather_skills_history on {src.name}; "
                "embedding plot-timeseries step alone.",
                file=sys.stderr,
            )
        letter = chr(ord("a") + idx)
        png_metadata[f"weather_skills_history_{letter}"] = json.dumps(
            upstream + [entry], sort_keys=True
        )

    fig.savefig(out, dpi=150, metadata=png_metadata)
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
