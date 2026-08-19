"""Shared regular expressions for the skill versioning toolchain.

This module is the single source of truth for the patterns used by both
`.github/scripts/bump-skill-version.py` (which rewrites `_SKILL_VERSION`) and
`tools/check_version_consistency.py` (which verifies every skill script
carries a consistent constant). It lives under `tools/` — outside `skills/` —
so the "no shared helper module" rule in `CONVENTIONS.md` (which scopes to
skill scripts) does not apply.

Skill version is defined **only** as a module-level `_SKILL_VERSION` in
`scripts/*.py`. SKILL.md does not carry a version field.

The Python-constant patterns intentionally tolerate several stylistic
variations that a contributor might use without changing semantics:

  - quote style around the value: single OR double quotes
  - optional PEP 526 type annotation between the name and `=`
    (`_SKILL_VERSION: str = "0.0.1"`)
  - optional trailing comment after the closing quote

The `_VERSION_LINE_RE_REWRITE` flavor adds capture groups (prefix /
suffix) so the bump script can reconstruct the line preserving the
contributor's choice of quote style, annotation, whitespace, and any
trailing comment. The `_VERSION_LINE_RE_VALUE` flavor exposes only the
captured value, for the consistency checker.
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

# Direct child of the top-level `metadata:` block — must not reappear.
_METADATA_VERSION_LINE_RE = re.compile(r"^  version:[ \t]*.*$", re.MULTILINE)
