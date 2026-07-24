# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Static check: a skill's SKILL.md `metadata.version` and its scripts'
`_SKILL_VERSION` must agree.

For each skill that carries BOTH:
  - a nested `metadata.version` field in `skills/<name>/SKILL.md` frontmatter, AND
  - at least one `.py` file under `skills/<name>/scripts/` with a top-level
    `_SKILL_VERSION = "..."` constant,

verify the two values match. Fails the check with a list of mismatches.

This guards against the failure mode where someone hand-edits one of the two
without the version-bump workflow's lockstep rewrite — for example, editing
`_SKILL_VERSION` directly in a script (which would shift the runtime
constant out of sync with the SKILL.md `metadata.version` that downstream
caches key on).

Usage:
    uv run tools/check_version_consistency.py skills/
"""

import sys
from pathlib import Path

# Sibling module under tools/. Both this script and bump-skill-version.py
# share these patterns so a change in one stays consistent with the other.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rhiza_version_re import (
    _VERSION_LINE_RE_VALUE,
    MetadataVersionParseError,
    find_metadata_version_line,
)


def _read_skill_md_version(skill_md: Path) -> str | None:
    """Return the SKILL.md frontmatter `metadata.version` value, or None if
    absent or empty (treated identically — whitespace-only counts as
    missing).

    A SKILL.md without a populated `metadata.version` field is a contract
    violation by the maintainer; the caller treats it as a hard error.
    """
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        if not (text.rstrip().endswith("---") and text.count("---", 0) >= 2):
            return None
        end = text.rstrip().rfind("---")
    front = text[3:end]
    try:
        _, raw, _ = find_metadata_version_line(front.split("\n"))
    except MetadataVersionParseError:
        return None
    # Strip surrounding quotes (single or double) if present. The
    # consistency check only cares about the value; preserving quote
    # style is the bump script's job.
    if (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2) or (
        raw.startswith("'") and raw.endswith("'") and len(raw) >= 2
    ):
        value = raw[1:-1].strip()
    else:
        value = raw.strip()
    if not value:
        # `version:` field exists but value is empty / whitespace only.
        # Treat as missing so the caller emits the same hard-error message.
        return None
    return value


def _read_script_constants(scripts_dir: Path) -> tuple[dict[Path, str], list[Path]]:
    """Return ({script_path: _SKILL_VERSION value}, scripts_missing_constant).

    `scripts_missing_constant` is empty when no script in the directory
    carries the constant (a skill that legitimately doesn't use it). It's
    populated only when partial coverage is detected — some scripts have it,
    some don't — which the caller flags as drift.
    """
    if not scripts_dir.is_dir():
        return {}, []
    have: dict[Path, str] = {}
    lacking: list[Path] = []
    for py in sorted(scripts_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        match = _VERSION_LINE_RE_VALUE.search(text)
        if match:
            have[py] = match.group("value")
        else:
            lacking.append(py)
    if not have:
        # Skill doesn't use the constant at all; not a drift case.
        return {}, []
    return have, lacking


def _check(root: Path) -> int:
    skills_dir = root if root.name == "skills" else root / "skills"
    if not skills_dir.is_dir():
        print(f"Error: {skills_dir} is not a directory", file=sys.stderr)
        return 2

    mismatches: list[str] = []
    drift_partials: list[str] = []
    missing_md_version: list[str] = []
    checked = 0
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill = skill_md.parent.name
        md_version = _read_skill_md_version(skill_md)
        if md_version is None:
            # SKILL.md has no populated `metadata.version` field. Every
            # skill in this repo is expected to carry one (it's the
            # standards-compliant identity for skillkit / rhiza-agents),
            # so missing values are a hard error.
            missing_md_version.append(
                f"{skill}: {skill_md.relative_to(root)} has no `metadata.version` "
                "field (or the field is empty)"
            )
            continue
        scripts, lacking = _read_script_constants(skill_md.parent / "scripts")
        if not scripts:
            # Skill has no scripts/*.py with the constant; nothing to compare.
            continue
        checked += 1
        for script_path, script_version in scripts.items():
            if script_version != md_version:
                mismatches.append(
                    f"{skill}: SKILL.md metadata.version {md_version!r} != "
                    f"{script_path.relative_to(root)} _SKILL_VERSION {script_version!r}"
                )
        # Partial-drift case: at least one script in the skill carries
        # the constant, at least one doesn't. Either the contributor
        # forgot to add it to a new script that should track the version,
        # or a non-dataset-emitting helper landed under scripts/ where
        # the bump workflow expects to rewrite it.
        for lacking_path in lacking:
            drift_partials.append(
                f"{skill}: {lacking_path.relative_to(root)} is missing "
                "_SKILL_VERSION but a sibling script in the same "
                "skill has it"
            )

    failed = mismatches or drift_partials or missing_md_version
    if failed:
        print("Version-consistency check failed:", file=sys.stderr)
        for line in mismatches:
            print(f"  mismatch:    {line}", file=sys.stderr)
        for line in drift_partials:
            print(f"  partial:     {line}", file=sys.stderr)
        for line in missing_md_version:
            print(f"  no version:  {line}", file=sys.stderr)
        print(
            "\nThese values are rewritten in lockstep by the version-bump "
            "workflow. If you edited one by hand, revert and let the workflow "
            "do the bump on the next merge to main. If a script genuinely "
            "shouldn't track the skill version, move it out of scripts/.",
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
