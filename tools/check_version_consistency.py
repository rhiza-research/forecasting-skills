# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Static check: skill version lives only in Python `_SKILL_VERSION`.

For each skill under `skills/<name>/`:

  - SKILL.md must not carry a `metadata.version` field
  - every `.py` under `scripts/` must define a top-level `_SKILL_VERSION`
  - when multiple scripts define it, the values must agree

Usage:
    uv run tools/check_version_consistency.py skills/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rhiza_version_re import (
    _METADATA_VERSION_LINE_RE,
    _VERSION_LINE_RE_VALUE,
)


def _check(root: Path) -> int:
    skills_dir = root if root.name == "skills" else root / "skills"
    if not skills_dir.is_dir():
        print(f"Error: {skills_dir} is not a directory", file=sys.stderr)
        return 2

    errors: list[str] = []
    checked = 0
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill = skill_md.parent.name
        md_text = skill_md.read_text(encoding="utf-8")
        if _METADATA_VERSION_LINE_RE.search(md_text):
            errors.append(
                f"{skill}: {skill_md.relative_to(root)} still has `metadata.version`; "
                "version belongs only in scripts/_SKILL_VERSION"
            )

        scripts_dir = skill_md.parent / "scripts"
        if not scripts_dir.is_dir():
            errors.append(f"{skill}: missing scripts/ directory")
            continue

        have: dict[Path, str] = {}
        lacking: list[Path] = []
        for py in sorted(scripts_dir.rglob("*.py")):
            match = _VERSION_LINE_RE_VALUE.search(py.read_text(encoding="utf-8"))
            if match:
                have[py] = match.group("value")
            else:
                lacking.append(py)

        if not have:
            errors.append(
                f"{skill}: no scripts/*.py defines `_SKILL_VERSION` "
                "(version must be defined in Python)"
            )
            continue

        checked += 1
        for path in lacking:
            errors.append(f"{skill}: {path.relative_to(root)} is missing `_SKILL_VERSION`")

        distinct = set(have.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{p.relative_to(root)}={v!r}" for p, v in have.items())
            errors.append(f"{skill}: scripts disagree on `_SKILL_VERSION`: {detail}")

    if errors:
        print("Version-consistency check failed:", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nSkill version is defined only as `_SKILL_VERSION` in scripts/*.py. "
            "Do not put `version:` under SKILL.md metadata. Let the version-bump "
            "workflow rewrite the constant on merge to main.",
            file=sys.stderr,
        )
        return 1

    print(f"Version consistency OK across {checked} skill(s).")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: check_version_consistency.py <path-to-repo-root-or-skills-dir>",
            file=sys.stderr,
        )
        return 2
    return _check(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    sys.exit(main())
