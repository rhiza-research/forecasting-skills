# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "pillow",
# ]
# ///
"""Inspect the rhiza_history provenance chain stamped on a Rhiza artifact.

Read-only. Takes one artifact -- a Rhiza Envelope Zarr (a directory) or a
plot PNG (a file ending .png) -- extracts its rhiza_history chain(s), and
renders one of three views: a human-readable lineage, the raw JSON chain,
or a runnable bash script that reproduces the artifact. All output goes to
stdout; diagnostics and errors go to stderr. Never writes or modifies any file.
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"


def _parse_chain(raw: str, path: Path) -> list:
    """Parse a JSON rhiza_history value into a list of step dicts, or exit 2."""
    try:
        chain = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        print(f"Error: {path} has a rhiza_history value that is not valid JSON.", file=sys.stderr)
        sys.exit(2)
    if not isinstance(chain, list):
        print(f"Error: {path} rhiza_history is not a JSON array.", file=sys.stderr)
        sys.exit(2)
    return chain


def _load_zarr(path: Path) -> dict:
    """Return {chains, source, name} for a zarr store's rhiza_history."""
    import xarray as xr

    try:
        with xr.open_zarr(path, consolidated=False) as ds:
            attrs = dict(ds.attrs)
    except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
        print(f"Error: could not open {path} as a zarr store: {exc}", file=sys.stderr)
        sys.exit(2)
    chains = {}
    raw = attrs.get("rhiza_history")
    if raw:
        chains[path.name] = _parse_chain(raw, path)
    return {"chains": chains, "source": attrs.get("rhiza_source"), "name": path.name}


def _load_png(path: Path) -> dict:
    """Return {chains, source, name} for a PNG's tEXt provenance keys.

    Single-input plotters write `rhiza_history`. Multi-input plotters write one
    `rhiza_history_<label>` per input branch (`_a`..`_z` for plot-compare and
    plot-timeseries; `_forecast`/`_mclimate` for plot-mediogram). Keys are
    discovered dynamically, so no branch is silently dropped.
    """
    from PIL import Image

    try:
        with Image.open(path) as img:
            info = dict(img.info)
    except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
        print(f"Error: could not open {path} as a PNG: {exc}", file=sys.stderr)
        sys.exit(2)
    chains = {}
    for key in sorted(info):
        if key == "rhiza_history":
            chains[path.name] = _parse_chain(info[key], path)
        elif key.startswith("rhiza_history_"):
            chains[key[len("rhiza_history_") :]] = _parse_chain(info[key], path)
    return {"chains": chains, "source": None, "name": path.name}


def _read_artifact(path: Path) -> dict:
    """Detect zarr (directory) vs PNG (.png file) and read it; else exit 2."""
    if not path.exists():
        print(f"Error: {path} not found.", file=sys.stderr)
        sys.exit(2)
    if path.is_dir():
        return _load_zarr(path)
    if path.is_file() and path.suffix.lower() == ".png":
        return _load_png(path)
    print(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        file=sys.stderr,
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# human
# --------------------------------------------------------------------------- #
def _format_input(step_input) -> str:
    if step_input is None:
        return "(none -- fetcher)"
    if isinstance(step_input, list):
        return "[" + ", ".join(i.get("basename", "?") for i in step_input) + "]"
    if isinstance(step_input, dict):
        return step_input.get("basename", "?")
    return str(step_input)


def _print_step(n: int, step: dict, indent: str) -> None:
    print(f"{indent}{n}. {step.get('skill', '?')} (v{step.get('version', '?')})")
    print(f"{indent}   input: {_format_input(step.get('input'))}")
    args = step.get("args") or {}
    if args:
        print(f"{indent}   args: " + ", ".join(f"{k}={v!r}" for k, v in sorted(args.items())))
    else:
        print(f"{indent}   args: (none)")


def _print_concat_branches(step: dict, indent: str) -> None:
    """Under a concat step, list each input branch's recorded lineage. Letters
    a, b, c… by input order match the reproduction-script branch labels."""
    items = step.get("input")
    if not isinstance(items, list):
        return
    for idx, item in enumerate(items):
        if not isinstance(item, dict) or "history" not in item:
            continue
        label = chr(ord("a") + idx)
        history = item.get("history") or []
        print(f"{indent}   input branch {label} ({item.get('basename', '?')}):")
        if not history:
            print(f"{indent}     (no recorded history)")
            continue
        for bn, bstep in enumerate(history, start=1):
            _print_step(bn, bstep, indent + "     ")


def _render_human(data: dict) -> None:
    chains = data["chains"]
    source = data.get("source")
    if source:
        print(f"rhiza_source: {source}")
        print()
    multi = len(chains) > 1
    first = True
    for label, chain in chains.items():
        if not first:
            print()
        first = False
        if multi:
            print(f"branch {label}:")
        indent = "  " if multi else ""
        for n, step in enumerate(chain, start=1):
            _print_step(n, step, indent)
            if step.get("skill") == "concat" and isinstance(step.get("input"), list):
                _print_concat_branches(step, indent)


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #
def _render_json(data: dict) -> None:
    chains = data["chains"]
    payload = next(iter(chains.values())) if len(chains) == 1 else chains
    print(json.dumps(payload, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# script
# --------------------------------------------------------------------------- #
def _args_to_flags(args: dict) -> list:
    """Argparse-dest dict -> shell-quoted CLI flag tokens.

    Dest names are underscored, flags hyphenated (`time_dim` -> `--time-dim`).
    None / False -> omitted; True -> bare flag; list -> one repeated flag per
    element. Values are run through shlex.quote so spaces/specials are safe.
    """
    tokens = []
    for dest, value in sorted(args.items()):
        flag = "--" + dest.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            tokens.append(flag)
        elif isinstance(value, list):
            for item in value:
                tokens += [flag, shlex.quote(str(item))]
        else:
            tokens += [flag, shlex.quote(str(value))]
    return tokens


def _command(skill: str, args: dict, inputs: list, output: str) -> str:
    """Render one reproduction command. `inputs` is a list of (flag, path)."""
    parts = [
        "uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills",
        skill,
    ] + _args_to_flags(args)
    for flag, path in inputs:
        parts += [flag, shlex.quote(path)]
    parts += ["--output", shlex.quote(output)]
    return " ".join(parts)


def _emit_linear(chain: list, prefix: str, final_output: str) -> list:
    """Commands for one linear chain. Intermediate steps write `<prefix>N.zarr`;
    the last step writes `final_output`. A fetcher (input null) takes no
    --input; a multi-input step replays each recorded input; a non-fetcher head
    is flagged for the user to supply."""
    lines = []
    prev = None
    n = len(chain)
    for i, step in enumerate(chain, start=1):
        skill = step.get("skill", "?")
        args = step.get("args") or {}
        step_input = step.get("input")
        output = final_output if i == n else f"{prefix}{i}.zarr"

        if isinstance(step_input, list):
            ins = []
            for j, item in enumerate(step_input):
                if j == 0 and prev is not None:
                    ins.append(("--input", prev))
                else:
                    ins.append(("--input", item.get("basename", "?")))
            lines.append(_command(skill, args, ins, output))
            prev = output
            continue

        if step_input is None:
            inputs = []
        elif prev is not None:
            inputs = [("--input", prev)]
        else:
            lines.append(f"# step {i} ({skill})'s input is an artifact outside this chain;")
            lines.append("# reproduce it separately and replace <UPSTREAM> below.")
            inputs = [("--input", "<UPSTREAM>")]

        lines.append(_command(skill, args, inputs, output))
        prev = output
    return lines


def _concat_branches(chain: list) -> dict | None:
    """If `chain`'s terminal entry is a multi-input `concat` carrying per-input
    `history`, expand it into a `{letter: history + [concat_entry]}` chains dict
    (letters a, b, c… by input order, matching plot-timeseries) suitable for the
    multi-branch reproduction path. Returns None when the chain is not such a
    concat, so linear chains fall through to `_emit_linear` unchanged."""
    if not chain:
        return None
    terminal = chain[-1]
    if terminal.get("skill") != "concat":
        return None
    items = terminal.get("input")
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and "history" in i for i in items
    ):
        return None
    branches = {}
    for idx, item in enumerate(items):
        label = chr(ord("a") + idx)
        branches[label] = list(item.get("history") or []) + [terminal]
    return branches


def _branch_input_flags(branch_outputs: list) -> list:
    """Map (label, output) branch pairs to the final plotter's input flags."""
    labels = [lab for lab, _ in branch_outputs]
    if set(labels) <= {"forecast", "mclimate"}:
        order = {"forecast": 0, "mclimate": 1}
        ordered = sorted(branch_outputs, key=lambda x: order.get(x[0], 99))
        return [("--" + lab, out) for lab, out in ordered]
    ordered = sorted(branch_outputs, key=lambda x: x[0])
    return [("--input", out) for _, out in ordered]


def _render_script(data: dict) -> None:
    chains = data["chains"]
    name = data.get("name", "artifact")
    lines = [
        "#!/usr/bin/env bash",
        "# Reproduction script generated by the `provenance` skill: re-runs the",
        "# recorded pipeline to regenerate this artifact. Needs nothing installed",
        "# -- uvx fetches the forecasting-skills CLI on demand. Fetch steps read",
        "# credentials from the environment at run time; no secrets are embedded.",
        "set -eo pipefail",
        "",
    ]

    if len(chains) <= 1:
        # zarr, or single-input PNG: one linear chain ending in this artifact.
        # A concat zarr's terminal entry carries each input's full chain under
        # `input[*].history`; expand it into one branch per input so the
        # multi-branch reproduction path below threads every branch through the
        # final concat. Otherwise it's a plain linear chain.
        chain = next(iter(chains.values()))
        branches = _concat_branches(chain)
        if branches is None:
            lines += _emit_linear(chain, prefix="step", final_output=name)
            print("\n".join(lines))
            return
        chains = branches

    # Multi-input merge or plot. Each branch chain ends in the same shared
    # terminal step (a concat or a plotter). Reproduce each branch up to (but
    # excluding) that step to build its input, then run the terminal step once
    # with every branch's output as an input.
    terminal = None
    branch_outputs = []
    for label, chain in chains.items():
        if not chain:
            continue
        terminal = chain[-1]
        sub = chain[:-1]
        branch_out = f"{label}.zarr"
        lines.append(f"# --- input branch {label} ---")
        if sub:
            lines += _emit_linear(sub, prefix=f"{label}_", final_output=branch_out)
        else:
            lines.append(
                f"# branch {label} records no steps before the final step; "
                f"supply {branch_out} yourself."
            )
        branch_outputs.append((label, branch_out))
        lines.append("")

    lines.append("# --- combine into the final step ---")
    if terminal is not None:
        ins = _branch_input_flags(branch_outputs)
        lines.append(_command(terminal["skill"], terminal.get("args") or {}, ins, name))
    print("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input", "-i", required=True, help="Artifact to inspect: a zarr dir or a .png file."
    )
    p.add_argument(
        "--format",
        choices=["human", "json", "script"],
        default="human",
        help="Output view: human-readable lineage, raw JSON chain, or a reproduction script.",
    )
    args = p.parse_args()

    data = _read_artifact(Path(args.input))

    if not data["chains"]:
        print(f"no provenance recorded on {args.input}")
        sys.exit(0)

    if args.format == "human":
        _render_human(data)
    elif args.format == "json":
        _render_json(data)
    else:
        _render_script(data)


if __name__ == "__main__":
    main()
