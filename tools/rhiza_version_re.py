"""Shared regular expressions and parsers for the skill versioning toolchain.

This module is the single source of truth for the patterns used by both
`.github/scripts/bump-skill-version.py` (which rewrites the constant in
lockstep with SKILL.md) and `tools/check_version_consistency.py` (which
verifies they agree). It lives under `tools/` — outside `skills/` — so
the "no shared helper module" rule in `CONVENTIONS.md` (which scopes to
skill scripts) does not apply.

The Python-constant patterns intentionally tolerate several stylistic
variations that a contributor might use without changing semantics:

  - quote style around the value: single OR double quotes
  - optional PEP 526 type annotation between the name and `=`
    (`_SKILL_VERSION: str = "0.1.0"`)
  - optional trailing comment after the closing quote

The `_VERSION_LINE_RE_REWRITE` flavor adds capture groups (prefix /
suffix) so the bump script can reconstruct the line preserving the
contributor's choice of quote style, annotation, whitespace, and any
trailing comment. The `_VERSION_LINE_RE_VALUE` flavor exposes only the
captured value, for the consistency checker.

For SKILL.md frontmatter, the `version:` field lives nested under the
top-level `metadata:` key per the Agent Skills specification — i.e.
`metadata.version`. The `find_metadata_version_line` helper locates the
line index, raw value, and detected child indentation so callers can
either read the value (consistency check) or rewrite it (bump script)
without re-implementing the YAML walk.
"""

import re

# Components reused between the rewrite and value-only flavors so a
# change to one flavor can't drift from the other.
_NAME = r"_SKILL_VERSION"
# Optional type annotation: `: str`, `: Final`, `: Final[str]`, etc. We
# don't lock the annotation form; any non-`=` characters between `:` and
# `=` are allowed. The pattern stops at the assignment `=` so the rest
# of the line shape stays under our control.
_ANNOTATION = r"(?:[ \t]*:[ \t]*[^=\n]+)?"
_ASSIGN = r"[ \t]*=[ \t]*"
# Quoted value: single OR double quotes. The pair must match.
_QUOTED_VALUE = r"""(?P<q>['"])(?P<value>[^'"\n]*)(?P=q)"""
# Trailing whitespace and optional inline comment.
_TRAILER = r"([ \t]*(?:#[^\n]*)?)"

# Rewrite flavor: used by bump-skill-version.py. Captures the prefix
# (everything up through the opening quote position) and the trailer so
# `re.sub` can swap only the value while preserving annotation, quote
# choice, whitespace, and comment.
_VERSION_LINE_RE_REWRITE = re.compile(
    rf"^(?P<prefix>{_NAME}{_ANNOTATION}{_ASSIGN}){_QUOTED_VALUE}(?P<trailer>{_TRAILER})$",
    re.MULTILINE,
)

# Value flavor: used by check_version_consistency.py. Only the value
# needs to be extracted; the named `value` group is what the caller
# reads.
_VERSION_LINE_RE_VALUE = re.compile(
    rf"^{_NAME}{_ANNOTATION}{_ASSIGN}{_QUOTED_VALUE}",
    re.MULTILINE,
)


# Top-level `metadata:` key on a SKILL.md frontmatter line. The colon
# must be at end-of-line (a mapping value, not a scalar) and the line
# must have zero indentation.
_METADATA_KEY_RE = re.compile(r"^metadata:[ \t]*$")

# A `version:` key on a frontmatter line. The colon must be followed by
# whitespace, value, or end-of-line; this excludes URL-shaped values
# like `version:http://...` and keys like `versionX:` (which a bare
# `startswith("version:")` would otherwise accept). The leading
# whitespace is captured so the caller can detect the parent's child
# indentation.
_VERSION_KEY_RE = re.compile(r"^(?P<indent>[ \t]+)version:(?:[ \t].*|)$")


class MetadataVersionParseError(ValueError):
    """Raised when `metadata.version` cannot be located or parsed.

    Subclass of ValueError so existing `except ValueError` handlers in
    callers (e.g. bump-skill-version.py's main) keep working.
    """


def find_metadata_version_line(frontmatter_lines: list[str]) -> tuple[int, str, str]:
    """Locate the `version:` line nested under a top-level `metadata:` block.

    `frontmatter_lines` is the SKILL.md YAML frontmatter split on `\\n`,
    excluding the opening and closing `---` markers (the caller is
    expected to have already separated those out).

    Returns `(line_index, raw_value, indent)` where:
      - `line_index` is the 0-based index of the `version:` line within
        `frontmatter_lines`.
      - `raw_value` is everything after `version:` on that line,
        stripped of surrounding whitespace (still quoted if the source
        was quoted — caller strips the quotes to detect style).
      - `indent` is the exact whitespace prefix (spaces and/or tabs)
        used on the `version:` line; the bump script preserves this
        verbatim when rewriting.

    Raises MetadataVersionParseError if:
      - No top-level `metadata:` block exists.
      - The `metadata:` block exists but has no `version:` child key
        before the first sibling key (or end of frontmatter).
    """
    metadata_idx = None
    for i, line in enumerate(frontmatter_lines):
        if _METADATA_KEY_RE.match(line):
            metadata_idx = i
            break
    if metadata_idx is None:
        raise MetadataVersionParseError(
            "SKILL.md frontmatter has no top-level `metadata:` block; "
            "cannot locate `metadata.version`"
        )

    # Walk forward from the `metadata:` line. The block ends at the
    # first non-blank line whose indentation is at column 0 (a sibling
    # key) or at end-of-frontmatter, whichever comes first. Within the
    # block, any `version:` key is a candidate; we accept the first one
    # whose indent matches the typical child level.
    for j in range(metadata_idx + 1, len(frontmatter_lines)):
        line = frontmatter_lines[j]
        # Blank lines are tolerated inside the block.
        if line.strip() == "":
            continue
        # A line that starts at column 0 (no indent) terminates the
        # `metadata:` mapping.
        if line.lstrip() == line:
            break
        match = _VERSION_KEY_RE.match(line)
        if match is None:
            continue
        raw = line[match.end("indent") + len("version:") :].strip()
        return j, raw, match.group("indent")

    raise MetadataVersionParseError(
        "SKILL.md frontmatter has a `metadata:` block but no `version:` key nested inside it"
    )
