# /// script
# requires-python = ">=3.11"
# ///
"""Static check: imports in a PEP 723 script must be declared in its dependencies block.

Catches the easy class of "forgot to add foo to deps" — e.g. a fresh `import h5py`
that hasn't been added to the inline metadata. Does *not* catch lazy/runtime backend
imports inside libraries (e.g. xr.open_mfdataset → dask); those need integration tests.

Usage:
    uv run tools/check_inline_deps.py skills/imerg-fetch/scripts/fetch.py
    uv run tools/check_inline_deps.py skills              # recurses on dir
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

# Import name → package name for cases where they don't match after normalization
# (case, dashes, or namespace prefixes — the latter is handled in `_is_declared`).
IMPORT_TO_PACKAGE = {
    "TAHMO": "tahmo",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "ecmwfapi": "ecmwf-api-client",
}


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
    # Strip version specifiers, extras, environment markers.
    out = []
    for d in deps:
        name = re.split(r"[<>=!~;\[\s]", d, maxsplit=1)[0].strip()
        if name:
            out.append(name)
    return out


def _imports(tree: ast.AST) -> set[str]:
    """Top-level package names imported anywhere in the AST (module + nested)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _normalize(name: str) -> str:
    return name.lower().replace("-", "_")


def _is_declared(imp_norm: str, declared: set[str]) -> bool:
    if imp_norm in declared:
        return True
    # Namespace package: `import ecmwf` is satisfied by `ecmwf-datastores-client`
    # (which installs into the `ecmwf.*` namespace). Accept any declared dep
    # that begins with the imported name as a prefix segment.
    prefix = f"{imp_norm}_"
    return any(d.startswith(prefix) for d in declared)


def check(path: Path) -> list[str]:
    src = path.read_text()
    deps = _parse_pep723_deps(src)
    if deps is None:
        return []
    tree = ast.parse(src, filename=str(path))
    declared = {_normalize(IMPORT_TO_PACKAGE.get(d, d)) for d in deps}

    missing = []
    for imp in sorted(_imports(tree)):
        if imp in sys.stdlib_module_names:
            continue
        if imp == path.stem:
            continue
        normalized = _normalize(IMPORT_TO_PACKAGE.get(imp, imp))
        if not _is_declared(normalized, declared):
            missing.append(imp)
    return missing


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: check_inline_deps.py <file-or-dir> [<file-or-dir> ...]",
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

    any_missing = False
    for f in files:
        try:
            missing = check(f)
        except Exception as e:  # noqa: BLE001 -- report the parse error and continue scanning other files
            print(f"{f}: parse error: {e}", file=sys.stderr)
            any_missing = True
            continue
        if missing:
            any_missing = True
            print(f"{f}:")
            for m in missing:
                print(f"  imported but not declared: {m}")

    sys.exit(1 if any_missing else 0)


if __name__ == "__main__":
    main()
