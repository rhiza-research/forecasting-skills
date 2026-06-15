# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cftime",
#   "xarray",
#   "zarr",
#   "pillow",
# ]
# ///
"""Inspect the weather_skills_history provenance chain stamped on a weather-skills artifact.

Read-only. Takes one artifact -- a weather-skills envelope Zarr (a directory) or a
plot PNG (a file ending .png) -- extracts its weather_skills_history chain(s), and
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
_SKILL_VERSION = "0.1.7"


def _parse_chain(raw: str) -> list:
    """Parse a JSON weather_skills_history value into a list of step dicts.

    Strict: raises ``ValueError`` when the value is not valid JSON or does not
    decode to an array. Used by ``--check``, which records the raised message as
    a violation rather than aborting. Non-check readers use ``_coerce_chain``.
    """
    try:
        chain = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("value is not valid JSON") from None
    if not isinstance(chain, list):
        raise ValueError("value is not a JSON array")
    return chain


def _coerce_chain(raw: str, label: str) -> list | None:
    """Lenient parse of a weather_skills_history value for the non-check render paths.

    A value that is absent has already been filtered out by the caller. A value
    that is present but not a JSON array (non-JSON, or a JSON object/scalar) is
    malformed under the weather_skills_history array contract; return ``None`` and emit a
    one-line stderr warning pointing at ``--check``, so the caller omits the
    branch. A valid array (including an empty one) passes through unchanged,
    even when its entries are imperfect.
    """
    try:
        chain = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        chain = None
    if not isinstance(chain, list):
        print(
            f"ignoring malformed weather_skills_history on {label}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return None
    return chain


def _load_zarr(path: Path) -> dict:
    """Return {chains, source, name} for a zarr store's weather_skills_history."""
    import xarray as xr

    try:
        with xr.open_zarr(path, consolidated=False) as ds:
            attrs = dict(ds.attrs)
    except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
        print(f"Error: could not open {path} as a zarr store: {exc}", file=sys.stderr)
        sys.exit(2)
    chains = {}
    raw = attrs.get("weather_skills_history")
    coerced = _coerce_chain(raw, path.name) if raw else None
    # compatibility read for the rhiza_ attr prefix; scheduled for removal
    if coerced is None and attrs.get("rhiza_history"):
        coerced = _coerce_chain(attrs["rhiza_history"], path.name)
    if coerced is not None:
        chains[path.name] = coerced
    # compatibility read for the rhiza_ attr prefix; scheduled for removal
    source = attrs.get("weather_skills_source") or attrs.get("rhiza_source")
    return {"chains": chains, "source": source, "name": path.name}


def _load_png(path: Path) -> dict:
    """Return {chains, source, name} for a PNG's tEXt provenance keys.

    Single-input plotters write `weather_skills_history`. Multi-input plotters
    write one `weather_skills_history_<label>` per input branch (`_a`..`_z` for
    plot-compare and plot-timeseries; `_forecast`/`_mclimate` for
    plot-mediogram). Keys are discovered dynamically, so no branch is silently
    dropped.
    """
    from PIL import Image

    try:
        with Image.open(path) as img:
            info = dict(img.info)
    except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
        print(f"Error: could not open {path} as a PNG: {exc}", file=sys.stderr)
        sys.exit(2)
    chains = {}

    def _add(slot: str, key: str, display: str | None = None) -> None:
        if info[key] and slot not in chains:
            coerced = _coerce_chain(info[key], f"{path.name} ({display or key})")
            if coerced is not None:
                chains[slot] = coerced

    for key in sorted(info):
        if key == "weather_skills_history":
            _add(path.name, key)
        elif key.startswith("weather_skills_history_"):
            _add(key[len("weather_skills_history_") :], key)
    # compatibility read for the rhiza_ attr prefix; scheduled for removal
    for key in sorted(info):
        if key == "rhiza_history":
            _add(path.name, key, display="weather_skills_history")
        elif key.startswith("rhiza_history_"):
            _add(
                key[len("rhiza_history_") :],
                key,
                display="weather_skills_" + key[len("rhiza_") :],
            )
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
# check (schema validation)
# --------------------------------------------------------------------------- #
def _read_raw_histories(path: Path) -> dict:
    """Return {location_key: raw_string} for every weather_skills_history value present.

    Reads the raw, un-parsed values so ``--check`` can validate them itself.
    A zarr contributes its single `weather_skills_history` attr (keyed
    `weather_skills_history`); a PNG contributes its `weather_skills_history`
    and any `weather_skills_history_<label>` tEXt keys (keyed by the
    corresponding `weather_skills_*` key name). Exits 2 cleanly when the path
    is missing, unopenable, or neither a zarr directory nor a .png file.
    """
    if not path.exists():
        print(f"Error: {path} not found.", file=sys.stderr)
        sys.exit(2)
    if path.is_dir():
        import xarray as xr

        try:
            with xr.open_zarr(path, consolidated=False) as ds:
                attrs = dict(ds.attrs)
        except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
            print(f"Error: could not open {path} as a zarr store: {exc}", file=sys.stderr)
            sys.exit(2)
        raw = {}
        # Only register a truthy value: an empty-string weather_skills_history
        # is treated as absent (consistent with how consumers read it), so
        # --check reports "no provenance found" (exit 1) rather than
        # "invalid" (exit 2).
        if attrs.get("weather_skills_history"):
            raw["weather_skills_history"] = attrs["weather_skills_history"]
        # compatibility read for the rhiza_ attr prefix; scheduled for removal
        elif attrs.get("rhiza_history"):
            raw["weather_skills_history"] = attrs["rhiza_history"]
        return raw
    if path.is_file() and path.suffix.lower() == ".png":
        from PIL import Image

        try:
            with Image.open(path) as img:
                info = dict(img.info)
        except Exception as exc:  # noqa: BLE001 -- any open failure becomes a clean exit 2
            print(f"Error: could not open {path} as a PNG: {exc}", file=sys.stderr)
            sys.exit(2)
        raw = {}
        for key in sorted(info):
            # Only register a truthy value: an empty-string tEXt value is treated
            # as absent (consistent with how consumers read it), so --check reports
            # "no provenance found" (exit 1) rather than "invalid" (exit 2).
            is_new = key == "weather_skills_history" or key.startswith("weather_skills_history_")
            if is_new and info[key]:
                raw[key] = info[key]
                continue
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            is_old = key == "rhiza_history" or key.startswith("rhiza_history_")
            if is_old and info[key]:
                mapped = "weather_skills_" + key[len("rhiza_") :]
                if not info.get(mapped):
                    raw[mapped] = info[key]
        return raw
    print(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        file=sys.stderr,
    )
    sys.exit(2)


_ENTRY_KNOWN_KEYS = {"skill", "version", "args", "input"}
_INPUT_ITEM_KNOWN_KEYS = {"basename", "hash", "history"}


def _validate_input(value, loc: str, violations: list, notes: list) -> None:
    """Validate an entry's `input` field against the array contract.

    `input` is one of: `null`; a `{basename, hash}` dict; or an array of
    `{basename, hash}` dicts, each of which may also carry a nested `history`
    chain (recursively validated). Appends violations and notes in place.
    """
    if value is None:
        return

    def _check_item(item, item_loc: str) -> None:
        if not isinstance(item, dict):
            violations.append(f"{item_loc}: input entry is not an object")
            return
        if "basename" not in item:
            violations.append(f"{item_loc}: missing required key 'basename'")
        elif not isinstance(item["basename"], str):
            violations.append(f"{item_loc}.basename: must be a string")
        if "hash" not in item:
            violations.append(f"{item_loc}: missing required key 'hash'")
        elif not isinstance(item["hash"], str):
            violations.append(f"{item_loc}.hash: must be a string")
        if "history" in item:
            _validate_chain(item["history"], f"{item_loc}.history", violations, notes)
        for key in item:
            if key not in _INPUT_ITEM_KNOWN_KEYS:
                notes.append(f"{item_loc}: unknown key {key!r}")

    if isinstance(value, list):
        for j, item in enumerate(value):
            _check_item(item, f"{loc}[{j}]")
        return
    if isinstance(value, dict):
        _check_item(value, loc)
        return
    violations.append(f"{loc}: must be null, an object, or an array of objects")


def _validate_chain(chain, loc: str, violations: list, notes: list) -> None:
    """Validate one weather_skills_history chain (an array of entries) against the schema.

    Records every violation with its location into `violations`; records
    unknown/extra keys (which do not fail validation) into `notes`. Recurses
    into a concat entry's `input[*].history`.
    """
    if not isinstance(chain, list):
        violations.append(f"{loc}: value is not a JSON array")
        return
    for i, entry in enumerate(chain):
        eloc = f"{loc}[{i}]"
        if not isinstance(entry, dict):
            violations.append(f"{eloc}: entry is not an object")
            continue
        if "skill" not in entry:
            violations.append(f"{eloc}: missing required key 'skill'")
        elif not isinstance(entry["skill"], str):
            violations.append(f"{eloc}.skill: must be a string")
        elif not entry["skill"]:
            violations.append(f"{eloc}.skill: must be a non-empty string")
        if "version" not in entry:
            violations.append(f"{eloc}: missing required key 'version'")
        elif not isinstance(entry["version"], str):
            violations.append(f"{eloc}.version: must be a string")
        if "args" not in entry:
            violations.append(f"{eloc}: missing required key 'args'")
        elif not isinstance(entry["args"], dict):
            violations.append(f"{eloc}.args: must be an object")
        if "input" not in entry:
            violations.append(f"{eloc}: missing required key 'input'")
        else:
            _validate_input(entry["input"], f"{eloc}.input", violations, notes)
        for key in entry:
            if key not in _ENTRY_KNOWN_KEYS:
                notes.append(f"{eloc}: unknown key {key!r}")


def _run_check(path: Path) -> int:
    """Validate the weather_skills_history schema on `path` and return the exit code.

    `0` = valid provenance present; `1` = no provenance found; `2` = present
    but invalid (every violation is printed with its location). Never raises a
    traceback on malformed input -- reporting that input is the point.
    """
    raw_histories = _read_raw_histories(path)
    if not raw_histories:
        print(f"no provenance found on {path}")
        return 1

    violations: list = []
    notes: list = []
    for key, raw in raw_histories.items():
        try:
            chain = _parse_chain(raw)
        except ValueError as exc:
            violations.append(f"{key}: {exc}")
            continue
        _validate_chain(chain, key, violations, notes)

    if violations:
        print(f"invalid weather_skills_history on {path}:")
        for v in violations:
            print(f"  - {v}")
        if notes:
            print("notes (not failures):")
            for n in notes:
                print(f"  - {n}")
        return 2

    print(f"valid weather_skills_history on {path}")
    if notes:
        print("notes (not failures):")
        for n in notes:
            print(f"  - {n}")
    return 0


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
    if not isinstance(step, dict):
        # A non-dict chain entry is a malformed/opaque step; render a placeholder
        # rather than calling .get on it.
        print(f"{indent}{n}. (malformed entry: not an object)")
        return
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
        history = item.get("history")
        if not isinstance(history, list):
            # A nested history that is not a list is malformed; treat as empty.
            history = []
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
        if not isinstance(step, dict):
            # A non-dict chain entry is a malformed/opaque step; emit a comment
            # and skip it rather than calling .get on it.
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
    """If `chain`'s terminal entry is a multi-input `concat` carrying per-input
    `history`, expand it into a `{letter: history + [concat_entry]}` chains dict
    (letters a, b, c… by input order, matching plot-timeseries) suitable for the
    multi-branch reproduction path. Returns None when the chain is not such a
    concat, so linear chains fall through to `_emit_linear` unchanged."""
    if not chain:
        return None
    terminal = chain[-1]
    if not isinstance(terminal, dict):
        # A non-dict terminal is not a concat to expand; fall through to linear.
        return None
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
        history = item.get("history")
        if not isinstance(history, list):
            # A nested history that is not a list is malformed; treat as empty.
            history = []
        branches[label] = list(history) + [terminal]
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
        if not isinstance(terminal, dict):
            # A non-dict terminal is a malformed/opaque step; skip emitting the
            # final combine command rather than calling .get on it.
            lines.append("# (malformed terminal step skipped: not an object)")
        else:
            ins = _branch_input_flags(branch_outputs)
            lines.append(
                _command(terminal.get("skill", "?"), terminal.get("args") or {}, ins, name)
            )
    print("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument(
        "--input", "-i", required=True, help="Artifact to inspect: a zarr dir or a .png file."
    )
    p.add_argument(
        "--format",
        choices=["human", "json", "script"],
        default="human",
        help="Output view: human-readable lineage, raw JSON chain, or a reproduction script.",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Validate the weather_skills_history schema instead of rendering it. "
            "Exit 0 = valid provenance present, 1 = none found, 2 = present but invalid."
        ),
    )
    args = p.parse_args()

    if args.check:
        sys.exit(_run_check(Path(args.input)))

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
