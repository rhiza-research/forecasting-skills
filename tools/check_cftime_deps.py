# /// script
# requires-python = ">=3.11"
# ///
"""Static check: a script that opens an xarray dataset must declare `cftime`.

A valid CF Zarr whose time axis uses a non-standard model calendar (`noleap`,
`360_day`) decodes to object-dtype cftime datetimes; xarray cannot decode such
a file at open unless `cftime` is installed. So any skill script that opens a
dataset must list `cftime` in its PEP 723 dependencies, or it will fail to read
a perfectly valid CF Zarr. This check enforces that invariant statically.

A script "opens a dataset" when its AST contains a call to one of
`open_zarr` / `open_dataset` / `open_mfdataset`, either as an attribute on the
xarray import alias (`import xarray as xr` -> `xr.open_zarr(...)`) or as a bare
name imported from xarray (`from xarray import open_dataset` -> `open_dataset(...)`).

Usage:
    uv run tools/check_cftime_deps.py skills/plot/scripts/plot.py
    uv run tools/check_cftime_deps.py skills              # recurses on dir
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

OPENERS = {"open_zarr", "open_dataset", "open_mfdataset"}


def _parse_pep723_deps(src: str) -> list[str] | None:
    """Return the list of declared deps, or None if this isn't a PEP 723 script."""
    m = re.search(r"^# /// script\n(?P<body>(?:^#.*\n)*?)^# ///", src, re.MULTILINE)
    if not m:
        return None
    toml_lines = []
    for line in m.group("body").splitlines():
        if line.startswith("# "):
            toml_lines.append(line[2:])
        elif line == "#":
            toml_lines.append("")
        else:
            toml_lines.append(line.lstrip("#"))
    data = tomllib.loads("\n".join(toml_lines))
    deps = data.get("dependencies") or []
    out = []
    for d in deps:
        name = re.split(r"[<>=!~;\[\s]", d, maxsplit=1)[0].strip()
        if name:
            out.append(name)
    return out


def _normalize(name: str) -> str:
    return name.lower().replace("-", "_")


def _xarray_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the `xarray` module by `import xarray [as X]`."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "xarray" or alias.name.split(".")[0] == "xarray":
                    aliases.add(alias.asname or alias.name.split(".")[0])
    return aliases


def _xarray_bare_openers(tree: ast.AST) -> set[str]:
    """Local names bound to an opener via `from xarray import open_* [as Y]`."""
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "xarray":
            for alias in node.names:
                if alias.name in OPENERS:
                    bare.add(alias.asname or alias.name)
    return bare


def _opens_dataset(tree: ast.AST) -> bool:
    aliases = _xarray_aliases(tree)
    bare = _xarray_bare_openers(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `xr.open_zarr(...)` — attribute call on the xarray alias.
        if (
            isinstance(func, ast.Attribute)
            and func.attr in OPENERS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            return True
        # `open_zarr(...)` — bare name imported from xarray.
        if isinstance(func, ast.Name) and func.id in bare:
            return True
    return False


def check(path: Path) -> str | None:
    """Return an offender message for `path`, or None if it's clean."""
    src = path.read_text()
    deps = _parse_pep723_deps(src)
    if deps is None:
        return None
    tree = ast.parse(src, filename=str(path))
    if not _opens_dataset(tree):
        return None
    declared = {_normalize(d) for d in deps}
    if "cftime" in declared:
        return None
    return f"{path}: opens a dataset but does not declare cftime"


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: check_cftime_deps.py <file-or-dir> [<file-or-dir> ...]",
            file=sys.stderr,
        )
        sys.exit(2)

    files: list[Path] = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"Not found: {arg}", file=sys.stderr)
            sys.exit(2)

    any_offender = False
    for f in files:
        try:
            offense = check(f)
        except Exception as e:  # noqa: BLE001 -- report the parse error and continue scanning other files
            print(f"{f}: parse error: {e}", file=sys.stderr)
            any_offender = True
            continue
        if offense:
            any_offender = True
            print(offense)

    sys.exit(1 if any_offender else 0)


if __name__ == "__main__":
    main()
