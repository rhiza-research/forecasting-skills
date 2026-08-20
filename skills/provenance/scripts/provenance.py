# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   "xarray",
#   "zarr",
#   "pillow",
# ]
# ///
"""Inspect weather_skills_history on a Zarr or plot PNG (stdout only; never writes)."""

import json
import shlex
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.provenance import (
    HISTORY_ATTR,
    SOURCE_ATTR,
    coerce_chain,
    parse_chain,
    validate_chain,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _load_zarr(path: Path) -> dict:
    import xarray as xr

    try:
        with xr.open_zarr(path, consolidated=False) as ds:
            attrs = dict(ds.attrs)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(
            f"Error: could not open {path} as a zarr store: {exc}", prefix=False
        ) from None
    chains = {}
    raw = attrs.get(HISTORY_ATTR)
    coerced = coerce_chain(raw, path.name) if raw else None
    if coerced is not None:
        chains[path.name] = coerced
    return {"chains": chains, "source": attrs.get(SOURCE_ATTR), "name": path.name}


def _load_png(path: Path) -> dict:
    from PIL import Image

    try:
        with Image.open(path) as img:
            info = dict(img.info)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(f"Error: could not open {path} as a PNG: {exc}", prefix=False) from None
    chains = {}
    for key in sorted(info):
        if key == HISTORY_ATTR:
            slot = path.name
        elif key.startswith(f"{HISTORY_ATTR}_"):
            slot = key[len(f"{HISTORY_ATTR}_") :]
        else:
            continue
        if info[key] and slot not in chains:
            coerced = coerce_chain(info[key], f"{path.name} ({key})")
            if coerced is not None:
                chains[slot] = coerced
    return {"chains": chains, "source": None, "name": path.name}


def _read_artifact(path: Path) -> dict:
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    if path.is_dir():
        return _load_zarr(path)
    if path.is_file() and path.suffix.lower() == ".png":
        return _load_png(path)
    raise UsageError(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        prefix=False,
    )


def _read_raw_histories(path: Path) -> dict:
    """Return {location_key: raw_string} for every weather_skills_history value present."""
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    if path.is_dir():
        import xarray as xr

        try:
            with xr.open_zarr(path, consolidated=False) as ds:
                attrs = dict(ds.attrs)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as a zarr store: {exc}", prefix=False
            ) from None
        return {HISTORY_ATTR: attrs[HISTORY_ATTR]} if attrs.get(HISTORY_ATTR) else {}
    if path.is_file() and path.suffix.lower() == ".png":
        from PIL import Image

        try:
            with Image.open(path) as img:
                info = dict(img.info)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as a PNG: {exc}", prefix=False
            ) from None
        raw = {}
        for key in sorted(info):
            if (key == HISTORY_ATTR or key.startswith(f"{HISTORY_ATTR}_")) and info[key]:
                raw[key] = info[key]
        return raw
    raise UsageError(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        prefix=False,
    )


def _run_check(path: Path) -> tuple[int, str]:
    """Validate schema: 0=valid, 1=absent, 2=invalid."""
    raw_histories = _read_raw_histories(path)
    if not raw_histories:
        return 1, f"no provenance found on {path}"

    violations, notes = [], []
    for key, raw in raw_histories.items():
        try:
            chain = parse_chain(raw)
        except ValueError as exc:
            violations.append(f"{key}: {exc}")
            continue
        chain_violations, chain_notes = validate_chain(chain, key)
        violations.extend(chain_violations)
        notes.extend(chain_notes)

    if violations:
        lines = [f"invalid weather_skills_history on {path}:"]
        lines += [f"  - {v}" for v in violations]
        if notes:
            lines.append("notes (not failures):")
            lines += [f"  - {n}" for n in notes]
        return 2, "\n".join(lines)

    lines = [f"valid weather_skills_history on {path}"]
    if notes:
        lines.append("notes (not failures):")
        lines += [f"  - {n}" for n in notes]
    return 0, "\n".join(lines)


def _print_step(n: int, step: dict, indent: str) -> None:
    if not isinstance(step, dict):
        print(f"{indent}{n}. (malformed entry: not an object)")
        return
    print(f"{indent}{n}. {step.get('skill', '?')} (v{step.get('version', '?')})")
    step_input = step.get("input")
    if step_input is None:
        inp = "(none -- fetcher)"
    elif isinstance(step_input, list):
        inp = "[" + ", ".join(i.get("basename", "?") for i in step_input) + "]"
    elif isinstance(step_input, dict):
        inp = step_input.get("basename", "?")
    else:
        inp = str(step_input)
    print(f"{indent}   input: {inp}")
    args = step.get("args") or {}
    if args:
        print(f"{indent}   args: " + ", ".join(f"{k}={v!r}" for k, v in sorted(args.items())))
    else:
        print(f"{indent}   args: (none)")


def _render_human(data: dict) -> None:
    chains = data["chains"]
    source = data.get("source")
    if source:
        print(f"weather_skills_source: {source}")
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
            if (
                isinstance(step, dict)
                and step.get("skill") == "concat"
                and isinstance(step.get("input"), list)
            ):
                for idx, item in enumerate(step["input"]):
                    if not isinstance(item, dict) or "history" not in item:
                        continue
                    history = item.get("history")
                    if not isinstance(history, list):
                        history = []
                    print(
                        f"{indent}   input branch {chr(ord('a') + idx)} ({item.get('basename', '?')}):"
                    )
                    if not history:
                        print(f"{indent}     (no recorded history)")
                        continue
                    for bn, bstep in enumerate(history, start=1):
                        _print_step(bn, bstep, indent + "     ")


def _command(skill: str, args: dict, inputs: list, output: str) -> str:
    parts = [
        "uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills",
        skill,
    ]
    for dest, value in sorted(args.items()):
        flag = "--" + dest.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            parts.append(flag)
        elif isinstance(value, list):
            for item in value:
                parts += [flag, shlex.quote(str(item))]
        else:
            parts += [flag, shlex.quote(str(value))]
    for flag, path in inputs:
        parts += [flag, shlex.quote(path)]
    parts += ["--output", shlex.quote(output)]
    return " ".join(parts)


def _emit_linear(chain: list, prefix: str, final_output: str) -> list:
    lines = []
    prev = None
    n = len(chain)
    for i, step in enumerate(chain, start=1):
        if not isinstance(step, dict):
            lines.append(f"# (malformed entry skipped: not an object) -- step {i}")
            continue
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
    """Expand a terminal multi-input concat into ``{letter: history + [concat]}``."""
    if not chain or not isinstance(chain[-1], dict):
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
        history = item.get("history")
        if not isinstance(history, list):
            history = []
        branches[chr(ord("a") + idx)] = list(history) + [terminal]
    return branches


def _render_script(data: dict) -> None:
    chains = data["chains"]
    name = data.get("name", "artifact")
    lines = [
        "#!/usr/bin/env bash",
        "# Reproduction script generated by the `provenance` skill.",
        "set -eo pipefail",
        "",
    ]

    if len(chains) <= 1:
        chain = next(iter(chains.values()))
        branches = _concat_branches(chain)
        if branches is None:
            lines += _emit_linear(chain, prefix="step", final_output=name)
            print("\n".join(lines))
            return
        chains = branches

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
        if not isinstance(terminal, dict):
            lines.append("# (malformed terminal step skipped: not an object)")
        else:
            labels = [lab for lab, _ in branch_outputs]
            if set(labels) <= {"forecast", "mclimate"}:
                order = {"forecast": 0, "mclimate": 1}
                ordered = sorted(branch_outputs, key=lambda x: order.get(x[0], 99))
                ins = [("--" + lab, out) for lab, out in ordered]
            else:
                ordered = sorted(branch_outputs, key=lambda x: x[0])
                ins = [("--input", out) for _, out in ordered]
            lines.append(
                _command(terminal.get("skill", "?"), terminal.get("args") or {}, ins, name)
            )
    print("\n".join(lines))


@weather_skill(
    name="provenance",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "-i",
    "--input",
    required=True,
    help="Artifact to inspect: a zarr dir or a .png file.",
)
@weather_skill.argument(
    "--format",
    choices=["human", "json", "script"],
    default="human",
    help="Output view: human-readable lineage, raw JSON chain, or a reproduction script.",
)
@weather_skill.argument(
    "--check",
    action="store_true",
    help="Validate weather_skills_history schema (exit 0/1/2).",
)
def provenance(input, format, check, **kwargs):
    """Inspect weather_skills_history on a Zarr or plot PNG (stdout only; never writes)."""
    if check:
        code, report = _run_check(Path(input))
        if code == 0:
            print(report)
            return
        if code == 1:
            raise DataError(report, prefix=False)
        raise UsageError(report, prefix=False)

    data = _read_artifact(Path(input))
    if not data["chains"]:
        print(f"no provenance recorded on {input}")
        return

    if format == "human":
        _render_human(data)
    elif format == "json":
        chains = data["chains"]
        payload = next(iter(chains.values())) if len(chains) == 1 else chains
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _render_script(data)


if __name__ == "__main__":
    provenance()
