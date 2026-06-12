# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Build the deployable weather-skills.org site from the template in site/.

Reads every `skills/*/SKILL.md` frontmatter (`name`, `description`, the
nested `metadata.catalog-group` key, and `metadata.openclaw.requires.env`),
renders the grouped skill catalog, the skill count, and the pipeline-diagram
example lists into `site/index.html` via marker comments, and writes the
complete deployable site (index.html, 404.html, style.css, CNAME) to the
output directory. A catalog entry whose frontmatter declares a non-empty
`metadata.openclaw.requires.env` list gets a small "requires credentials"
tag; skills without the key get no marker. The catalog, count,
and flow sample lists track the skills tree: adding, removing, or
regrouping a skill changes the page on the next build with no template
edit. The composition example block in the template is hand-written; the
build cross-checks that every skill name it references exists in the tree.

The output directory is created fresh on every build. An existing output
directory is cleaned only if it is empty or carries the marker file this
build writes; anything else is refused, as is an output path that is (or
contains) the `site/` template directory.

Marker comments in the template (each must appear exactly once):

    <!-- gen:skill-count -->     the number of skills
    <!-- gen:catalog -->         the grouped catalog sections
    <!-- gen:flow-fetch -->      example <li> items for the fetch stage
    <!-- gen:flow-transform -->  example <li> items for the transform stage
    <!-- gen:flow-output -->     example <li> items for the visualization stage

Usage:
    uv run --script tools/build_site.py                  # writes _site/
    uv run --script tools/build_site.py --output /tmp/site-build
"""

import argparse
import html
import re
import shutil
import sys
from pathlib import Path

import yaml

# Catalog groups in page order. Every SKILL.md must carry one of these keys
# in `metadata.catalog-group`. The note must be literally true of every
# member skill; a skill that doesn't fit a note belongs in another group.
GROUPS: list[tuple[str, str, str | None]] = [
    ("fetchers", "Fetchers", "ingress — source → envelope"),
    ("transforms", "Transforms", "envelope → envelope"),
    ("visualization", "Visualization", "envelope → PNG"),
    (
        "agent-tooling",
        "Agent capabilities",
        "no envelope output — capabilities the agent uses alongside pipelines",
    ),
]

# Files copied verbatim from site/ into the output directory.
STATIC_FILES = ("style.css", "CNAME", "404.html")

# Written into the output directory so a later build can recognize the
# directory as its own prior output and clean it safely.
MARKER_FILE = ".weather-skills-build"

_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Skill names referenced by the hand-written composition example in the
# template, e.g. `forecasting-skills ecmwf-fetch \`.
_EXAMPLE_BLOCK_RE = re.compile(r'<pre class="example">.*?</pre>', re.DOTALL)
_EXAMPLE_SKILL_RE = re.compile(r"forecasting-skills[ \t]+([a-z][a-z0-9-]*)")


def _parse_frontmatter(skill_md: Path) -> dict:
    """Return the YAML frontmatter mapping of a SKILL.md, or raise ValueError."""
    text = skill_md.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError(f"{skill_md}: no YAML frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            block = "\n".join(lines[1:index])
            break
    else:
        raise ValueError(f"{skill_md}: frontmatter has no closing `---` line")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ValueError(f"{skill_md}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{skill_md}: frontmatter is not a mapping")
    return data


def _render_description(description: str) -> str:
    """HTML-escape a SKILL.md description, rendering `backtick` spans as <code>."""
    escaped = html.escape(description.strip())
    return _INLINE_CODE_RE.sub(r"<code>\1</code>", escaped)


def _requires_credentials(skill_md: Path, metadata: dict) -> bool:
    """True when frontmatter `metadata.openclaw.requires.env` is a non-empty list.

    An absent `openclaw`, `requires`, or `env` key means no credentials. A key
    that is present with the wrong shape (`openclaw`/`requires` not a mapping,
    `env` not a list) raises ValueError naming the skill.
    """
    if "openclaw" not in metadata:
        return False
    openclaw = metadata["openclaw"]
    if not isinstance(openclaw, dict):
        raise ValueError(f"{skill_md}: `metadata.openclaw` is not a mapping: {openclaw!r}")
    if "requires" not in openclaw:
        return False
    requires = openclaw["requires"]
    if not isinstance(requires, dict):
        raise ValueError(f"{skill_md}: `metadata.openclaw.requires` is not a mapping: {requires!r}")
    if "env" not in requires:
        return False
    env = requires["env"]
    if not isinstance(env, list):
        raise ValueError(f"{skill_md}: `metadata.openclaw.requires.env` is not a list: {env!r}")
    return len(env) > 0


def _collect_skills(skills_dir: Path) -> dict[str, list[tuple[str, str, bool]]]:
    """Read all SKILL.md files; return {group_key: [(name, description, creds), ...]}.

    `creds` is True when the skill's frontmatter declares a non-empty
    `metadata.openclaw.requires.env` list. Entries are sorted by skill name
    within each group. Raises ValueError on a skill directory without a
    SKILL.md, a missing/unknown group key, a name that doesn't match its
    directory, a missing description, or a malformed
    `metadata.openclaw.requires.env` shape.
    """
    known = {key for key, _, _ in GROUPS}
    grouped: dict[str, list[tuple[str, str, bool]]] = {key: [] for key, _, _ in GROUPS}
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir() and not (entry / "SKILL.md").is_file():
            raise ValueError(f"{entry}: skill directory has no SKILL.md")
    skill_mds = sorted(skills_dir.glob("*/SKILL.md"), key=lambda p: p.parent.name)
    if not skill_mds:
        raise ValueError(f"no skills/*/SKILL.md found under {skills_dir}")
    for skill_md in skill_mds:
        front = _parse_frontmatter(skill_md)
        name = front.get("name")
        if name != skill_md.parent.name:
            raise ValueError(
                f"{skill_md}: frontmatter name {name!r} != directory name {skill_md.parent.name!r}"
            )
        description = front.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{skill_md}: frontmatter has no description")
        if description.count("`") % 2:
            raise ValueError(f"{skill_md}: description has an unpaired backtick")
        metadata = front.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{skill_md}: frontmatter has no `metadata:` map")
        group = metadata.get("catalog-group")
        if group not in known:
            raise ValueError(
                f"{skill_md}: `metadata.catalog-group` is {group!r}; "
                f"expected one of {sorted(known)}"
            )
        grouped[group].append((name, description, _requires_credentials(skill_md, metadata)))
    for key, _, _ in GROUPS:
        if not grouped[key]:
            raise ValueError(f"catalog group {key!r} has no member skills")
    return grouped


def _render_catalog(grouped: dict[str, list[tuple[str, str, bool]]]) -> str:
    """Render the grouped catalog as group headings + <dl> entry lists."""
    parts: list[str] = []
    for key, label, note in GROUPS:
        note_html = f' <span class="group-note">{html.escape(note)}</span>' if note else ""
        parts.append(f'<h3 class="group-head">{html.escape(label)}{note_html}</h3>')
        parts.append('<dl class="catalog">')
        for name, description, requires_credentials in grouped[key]:
            cred_html = (
                ' <span class="cred-tag">requires credentials</span>'
                if requires_credentials
                else ""
            )
            parts.append(
                f'    <div class="skill"><dt>{html.escape(name)}{cred_html}</dt>'
                f"<dd>{_render_description(description)}</dd></div>"
            )
        parts.append("</dl>")
    return "\n  ".join(parts)


def _render_flow_items(names: list[str], total: int) -> str:
    """Render sample <li> items; append a "+N more" link when truncated."""
    items = [f"<li>{html.escape(name)}</li>" for name in names]
    extra = total - len(names)
    if extra > 0:
        items.append(f'<li><a href="#skills">+{extra} more</a></li>')
    return "\n        ".join(items)


def _check_example_names(template: str, skill_names: set[str]) -> None:
    """Verify every skill the composition example invokes exists in the tree."""
    match = _EXAMPLE_BLOCK_RE.search(template)
    if not match:
        raise ValueError('template has no <pre class="example"> composition block')
    for name in _EXAMPLE_SKILL_RE.findall(match.group(0)):
        if name not in skill_names:
            raise ValueError(f"composition example references unknown skill {name!r}")


def _prepare_output_dir(out_dir: Path, site_dir: Path) -> None:
    """Create out_dir fresh; refuse unsafe targets.

    Refuses an out_dir that is, or contains, the site/ template directory,
    and refuses to clean an existing non-empty out_dir unless it carries the
    marker file a previous build wrote.
    """
    if out_dir == site_dir or site_dir.is_relative_to(out_dir):
        raise ValueError(f"output directory {out_dir} is or contains the site/ template directory")
    if out_dir.exists():
        if not out_dir.is_dir():
            raise ValueError(f"output path {out_dir} exists and is not a directory")
        if any(out_dir.iterdir()) and not (out_dir / MARKER_FILE).is_file():
            raise ValueError(
                f"output directory {out_dir} is not empty and has no "
                f"{MARKER_FILE} marker; refusing to clean it"
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)


def _substitute(template: str, replacements: dict[str, str]) -> str:
    """Replace each marker exactly once; fail on missing or leftover markers."""
    out = template
    for marker, value in replacements.items():
        occurrences = out.count(marker)
        if occurrences != 1:
            raise ValueError(f"template marker {marker!r} appears {occurrences} times; expected 1")
        out = out.replace(marker, value)
    leftovers = re.findall(r"<!--\s*gen:[^>]*-->", out)
    if leftovers:
        raise ValueError(f"unsubstituted generator markers remain: {leftovers}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory for the deployable site (default: <repo>/_site)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    skills_dir = repo_root / "skills"
    site_dir = repo_root / "site"
    out_dir = Path(args.output).resolve() if args.output else repo_root / "_site"

    try:
        grouped = _collect_skills(skills_dir)
        count = sum(len(members) for members in grouped.values())
        fetch_names = [name for name, _, _ in grouped["fetchers"][:3]]
        transform_names = [name for name, _, _ in grouped["transforms"][:3]]
        output_names = [name for name, _, _ in grouped["visualization"][:3]]
        output_total = len(grouped["visualization"])
        template = (site_dir / "index.html").read_text(encoding="utf-8")
        _check_example_names(
            template,
            {name for members in grouped.values() for name, _, _ in members},
        )
        page = _substitute(
            template,
            {
                "<!-- gen:skill-count -->": str(count),
                "<!-- gen:catalog -->": _render_catalog(grouped),
                "<!-- gen:flow-fetch -->": _render_flow_items(
                    fetch_names, len(grouped["fetchers"])
                ),
                "<!-- gen:flow-transform -->": _render_flow_items(
                    transform_names, len(grouped["transforms"])
                ),
                "<!-- gen:flow-output -->": _render_flow_items(output_names, output_total),
            },
        )
        _prepare_output_dir(out_dir, site_dir)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    (out_dir / "index.html").write_text(page, encoding="utf-8")
    for filename in STATIC_FILES:
        shutil.copy2(site_dir / filename, out_dir / filename)
    (out_dir / MARKER_FILE).write_text("weather-skills.org site build output\n", encoding="utf-8")
    print(f"Built site with {count} skills -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
