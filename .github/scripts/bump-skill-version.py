# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bump a skill's `_SKILL_VERSION` constant in `scripts/**/*.py`.

Skill version lives only in Python: a top-level `_SKILL_VERSION = "..."` in
each skill script. SKILL.md does not carry a version field.

Applies a semver bump per `--kind`:

  - patch: 3rd component +1
  - minor: 2nd component +1, 3rd component reset to 0
  - major: 1st component +1, 2nd and 3rd reset to 0

The `_SKILL_VERSION` constant must sit at top-level module scope (a bare
`_SKILL_VERSION = "..."` line, optionally with a PEP 526 type annotation like
`_SKILL_VERSION: str = "..."`). The shared regex in `tools/rhiza_version_re.py`
deliberately won't match the constant inside a docstring, triple-quoted string,
or any indented context. Either single or double quotes are accepted; the
contributor's choice is preserved on rewrite.

Usage:
    python3 .github/scripts/bump-skill-version.py --skill <name> --kind {patch,minor,major}
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from rhiza_version_re import _VERSION_LINE_RE_REWRITE, _VERSION_LINE_RE_VALUE

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _bump(version: str, kind: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"not a semver X.Y.Z value: {version!r}")
    major, minor, patch = (int(p) for p in match.groups())
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind: {kind!r}")


def _read_script_versions(scripts_dir: Path) -> dict[Path, str]:
    """Return {path: version} for every script that defines `_SKILL_VERSION`."""
    found: dict[Path, str] = {}
    if not scripts_dir.is_dir():
        return found
    for py in sorted(scripts_dir.rglob("*.py")):
        match = _VERSION_LINE_RE_VALUE.search(py.read_text(encoding="utf-8"))
        if match:
            found[py] = match.group("value")
    return found


def _bump_script_constants(scripts_dir: Path, new_version: str) -> list[Path]:
    """Rewrite `_SKILL_VERSION` in every script that already carries it."""
    updated: list[Path] = []
    if not scripts_dir.is_dir():
        return updated
    for py in sorted(scripts_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if not _VERSION_LINE_RE_REWRITE.search(text):
            continue

        def _sub(match: re.Match[str]) -> str:
            return (
                f"{match.group('prefix')}"
                f"{match.group('q')}{new_version}{match.group('q')}"
                f"{match.group('trailer')}"
            )

        new_text = _VERSION_LINE_RE_REWRITE.sub(_sub, text)
        if new_text != text:
            py.write_text(new_text, encoding="utf-8")
            updated.append(py)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Skill directory name under skills/")
    parser.add_argument("--kind", required=True, choices=["patch", "minor", "major"])
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (defaults to two levels up from this script)",
    )
    args = parser.parse_args()

    if not _SKILL_NAME_RE.match(args.skill):
        print(
            f"Error: --skill {args.skill!r} is not a valid skill name "
            "(must start with alphanumeric and contain only [A-Za-z0-9_-]; "
            "names with '..', '/', '\\', or a leading '.' are rejected)",
            file=sys.stderr,
        )
        return 2

    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent.parent
    )
    skill_dir = repo_root / "skills" / args.skill
    if not skill_dir.is_dir():
        print(f"Error: {skill_dir} does not exist", file=sys.stderr)
        return 2

    scripts_dir = skill_dir / "scripts"
    versions = _read_script_versions(scripts_dir)
    if not versions:
        print(
            f"Error: {args.skill} has no `_SKILL_VERSION` constant under scripts/; "
            "every skill must define version in Python",
            file=sys.stderr,
        )
        return 2

    distinct = set(versions.values())
    if len(distinct) != 1:
        print(
            f"Error: {args.skill} scripts disagree on `_SKILL_VERSION`: "
            + ", ".join(f"{p.relative_to(repo_root)}={v!r}" for p, v in versions.items()),
            file=sys.stderr,
        )
        return 2

    old_version = next(iter(distinct))
    try:
        new_version = _bump(old_version, args.kind)
    except ValueError as exc:
        print(f"Error bumping {args.skill}: {exc}", file=sys.stderr)
        return 2

    updated = _bump_script_constants(scripts_dir, new_version)
    print(f"{args.skill}: {old_version} -> {new_version}")
    for path in updated:
        print(f"  updated {path.relative_to(repo_root)}: _SKILL_VERSION = {new_version!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
