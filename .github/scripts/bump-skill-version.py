# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bump a skill's `metadata.version` field in its SKILL.md frontmatter.

Reads `skills/<skill>/SKILL.md`, finds the YAML frontmatter block bounded by
the first two `---` lines, locates the `metadata:` block and its nested
`version:` key, and applies a semver bump per `--kind`:

  - patch: 3rd component +1
  - minor: 2nd component +1, 3rd component reset to 0
  - major: 1st component +1, 2nd and 3rd reset to 0

The script preserves frontmatter key order, surrounding whitespace, and the
markdown body byte-for-byte. It is intentionally a manual line-level parser
(not a full YAML round-trip) so that comments, quoting style, and key order
are not perturbed.

The `_SKILL_VERSION` constant rewritten in `scripts/**/*.py` must sit at
top-level module scope (a bare `_SKILL_VERSION = "..."` line, optionally
with a PEP 526 type annotation like `_SKILL_VERSION: str = "..."`). The
shared regex in `tools/rhiza_version_re.py` deliberately won't match the
constant inside a docstring, triple-quoted string, or any indented context,
to avoid rewriting incidental occurrences. Either single or double quotes
are accepted; the contributor's choice is preserved on rewrite.

Usage:
    python3 .github/scripts/bump-skill-version.py --skill <name> --kind {patch,minor,major}
"""

import argparse
import re
import sys
from pathlib import Path

# Import the shared regex module from tools/. Sibling-directory import via
# sys.path so this script stays runnable as `python3 .github/scripts/...`
# from the repo root without requiring an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from rhiza_version_re import (
    _VERSION_LINE_RE_REWRITE,
    MetadataVersionParseError,
    find_metadata_version_line,
)

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Validate `--skill` to reject path-escape characters. Skill names live under
# skills/<name>/; any name with `..`, `/`, `\`, or a leading `.` could let the
# script write outside the intended directory.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _bump(version: str, kind: str) -> str:
    match = SEMVER_RE.match(version)
    if not match:
        hint = ""
        # Pre-release suffixes (e.g. 0.1.0-rc1, 0.1.0+build.1) are a common
        # near-miss; mention them explicitly so the maintainer knows the
        # tooling is intentionally 3-component-only, not just a regex bug.
        if "-" in version or "+" in version:
            hint = (
                " (pre-release suffixes like '0.1.0-rc1' and build metadata like "
                "'0.1.0+build.1' are not supported; this script uses 3-component "
                "semver only)"
            )
        raise ValueError(
            f"version {version!r} is not a 3-component semver string "
            f"(X.Y.Z with non-negative integers){hint}"
        )
    major, minor, patch = (int(x) for x in match.groups())
    if kind == "patch":
        patch += 1
    elif kind == "minor":
        minor += 1
        patch = 0
    elif kind == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"unknown bump kind {kind!r}; expected one of patch/minor/major")
    return f"{major}.{minor}.{patch}"


def _bump_skill_md(text: str, kind: str) -> tuple[str, str, str]:
    """Bump the `metadata.version` field inside `text`'s YAML frontmatter.

    Returns (new_text, old_version, new_version).
    Raises ValueError if the frontmatter, the `metadata:` block, or the
    nested `version:` key is missing.
    """
    if not text.startswith("---"):
        raise ValueError("SKILL.md does not start with `---`; no YAML frontmatter detected")
    # The opening `---` must be followed by a newline; otherwise the file is
    # malformed (e.g. `---name: foo\n...` with no separator between the
    # opening fence and the first key). Reject early with a specific message
    # rather than letting the downstream parser produce a confusing error.
    # Allow exact `---` only (a frontmatter-only file is handled below).
    # Anything else with content directly after the opening `---` is
    # malformed.
    if (
        not text.startswith("---\n")
        and text.strip() != "---"
        and len(text) > 3
        and text[3] not in ("\n", "\r")
    ):
        raise ValueError(
            "SKILL.md frontmatter is malformed: opening `---` is not followed by a newline"
        )
    # Locate the closing `---` line. Look for `\n---` first (standard case
    # with a trailing newline after the closing marker). If absent, also
    # tolerate end-of-file: a SKILL.md whose last bytes are `---` with no
    # trailing newline still has a well-formed frontmatter block.
    end = text.find("\n---", 3)
    body_starts_at_end = False
    if end < 0:
        if text.rstrip().endswith("---") and text.count("---", 0) >= 2:
            # The whole file is frontmatter; treat the trailing `---` as the close.
            end = text.rstrip().rfind("---")
            body_starts_at_end = True
        else:
            raise ValueError("SKILL.md frontmatter has no closing `---` line")

    if body_starts_at_end:
        front = text[3:end]
        rest = text[end:]
    else:
        front = text[3:end]  # between the opening `---` and the closing `\n---`
        rest = text[end:]  # starts with `\n---`

    lines = front.split("\n")
    try:
        version_line_idx, raw, indent = find_metadata_version_line(lines)
    except MetadataVersionParseError as exc:
        # Re-raise as plain ValueError so the existing main() handler
        # produces the same exit-2 / stderr message shape.
        raise ValueError(str(exc)) from exc

    # Preserve the original quoting style: unquoted stays unquoted, single-
    # quoted stays single-quoted, double-quoted stays double-quoted. The
    # default for the no-quote case is to emit no quotes too.
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        old_version = raw[1:-1]
        quote = '"'
    elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        old_version = raw[1:-1]
        quote = "'"
    else:
        old_version = raw
        quote = ""

    if not old_version.strip():
        # `version:` field exists but is empty (`version:`, `version: ""`,
        # `version: ''`, or whitespace only). Without a baseline to bump
        # from, semver-parse would fail with a confusing message about
        # parsing an empty string; raise an explicit error here instead.
        raise ValueError(
            "SKILL.md frontmatter has an empty `metadata.version` field; "
            'set it to an initial version like "0.1.0" before running the bump'
        )

    new_version = _bump(old_version, kind)
    # Reconstruct the line preserving the original indentation. The
    # canonical shape is `<indent>version: <quote><value><quote>`.
    lines[version_line_idx] = f"{indent}version: {quote}{new_version}{quote}"

    new_front = "\n".join(lines)
    return "---" + new_front + rest, old_version, new_version


def _bump_script_constants(scripts_dir: Path, new_version: str) -> tuple[list[Path], list[Path]]:
    """Rewrite `_SKILL_VERSION = "..."` in every `.py` file under `scripts_dir`
    (recursively) that already carries the constant.

    Returns (updated, missing) where:
      - `updated` lists files whose content was rewritten.
      - `missing` lists files that DID NOT carry the constant despite at least
        one sibling carrying it. This indicates partial drift within a single
        skill: some scripts track the version, some don't. The caller warns on
        these. If NO file in the skill carries the constant, this function
        returns ([], []) — the skill simply doesn't use the constant (typical
        of skills that don't emit `weather_skills_history`) and there's nothing to do.

    Use `_skill_uses_constant` first to decide whether to call this function;
    that keeps the silent-skip-vs-warn-on-drift policy in one place.
    """
    if not scripts_dir.is_dir():
        return [], []
    updated: list[Path] = []
    missing: list[Path] = []
    for py in sorted(scripts_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if not _VERSION_LINE_RE_REWRITE.search(text):
            missing.append(py)
            continue

        # Build a substitution callable that preserves the contributor's
        # choice of annotation, whitespace, quote style, and trailing
        # comment. Only the value between the quotes is rewritten.
        def _sub(match: re.Match[str]) -> str:
            prefix = match.group("prefix")
            quote = match.group("q")
            trailer = match.group("trailer")
            return f"{prefix}{quote}{new_version}{quote}{trailer}"

        new_text = _VERSION_LINE_RE_REWRITE.sub(_sub, text)
        if new_text != text:
            py.write_text(new_text, encoding="utf-8")
            updated.append(py)
    return updated, missing


def _skill_uses_constant(scripts_dir: Path) -> bool:
    """Return True if any `.py` under `scripts_dir` carries `_SKILL_VERSION`.

    Skills that don't emit `weather_skills_history` legitimately don't carry the constant
    in any of their scripts; the bump workflow must silently skip them, not warn.
    Skills that emit `weather_skills_history` carry it in every script that writes a
    dataset; partial coverage is a drift case and gets warned about by
    `_bump_script_constants`'s `missing` return.
    """
    if not scripts_dir.is_dir():
        return False
    for py in scripts_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if _VERSION_LINE_RE_REWRITE.search(text):
            return True
    return False


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
    skill_md = repo_root / "skills" / args.skill / "SKILL.md"
    if not skill_md.is_file():
        print(f"Error: {skill_md} does not exist", file=sys.stderr)
        return 2

    text = skill_md.read_text(encoding="utf-8")
    try:
        new_text, old_version, new_version = _bump_skill_md(text, args.kind)
    except ValueError as exc:
        print(f"Error bumping {skill_md}: {exc}", file=sys.stderr)
        return 2

    skill_md.write_text(new_text, encoding="utf-8")
    print(f"{args.skill}: {old_version} -> {new_version}")

    scripts_dir = repo_root / "skills" / args.skill / "scripts"
    if not _skill_uses_constant(scripts_dir):
        # No script in this skill carries `_SKILL_VERSION`, so the constant-
        # rewrite step is a no-op: SKILL.md is the only artifact to bump.
        # This happens only for a skill whose scripts never stamp a version
        # into their output (i.e. none of them emit `weather_skills_history`).
        return 0

    updated, missing = _bump_script_constants(scripts_dir, new_version)
    for path in updated:
        print(f"  updated {path.relative_to(repo_root)}: _SKILL_VERSION = {new_version!r}")
    for path in missing:
        # Warn only on drift WITHIN a skill: some scripts carry the constant,
        # this one doesn't. Either the contributor forgot to add it to a new
        # script that should track version, or it intentionally doesn't write
        # a dataset and should be excluded — either way the maintainer needs
        # to look. (Skills with zero usage are silently skipped above.)
        print(
            f"warning: {path.relative_to(repo_root)} has no _SKILL_VERSION constant "
            "but a sibling script in the same skill does; "
            "add the constant if this script should track skill version, "
            "or move it out of scripts/ if it shouldn't.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
