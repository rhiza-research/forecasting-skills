# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate the generated weather-skills.org site output directory.

Checks, per the site's stated constraints:

1. The deployable files exist (index.html, 404.html, style.css, CNAME).
2. Every .html file is well-formed: tags balance under a real parse
   (html.parser with an open-tag stack; void elements excluded), no end
   tag without a matching start tag, nothing left open at end of input.
3. Element ids are unique within each document, and every internal anchor
   (`href="#..."`) resolves to an id in the same document.
4. Third-party references are policed by reference kind:
   - resource loads (`<link href>`, `<script src>`, `<img src>`,
     `<source src/srcset>`, CSS `url()`) must be relative or same-site,
     in any scheme form — absolute, scheme-relative, or non-HTTP schemes
     all fail;
   - `<a href>` absolute URLs (including scheme-relative) must match the
     allowlist: the skills repository, the weather-skills-core references
     repository, and the two references the page makes by design (the
     Agent Skills site and skillkit);
   - plain-text URL mentions outside attributes are permitted.
   Attributes are read with the HTML parser, not regex; scheme and host
   matching is case-insensitive.

Files that do not decode as UTF-8 are skipped in the text checks with a
note rather than crashing the run.

Usage:
    uv run tools/check_site_output.py _site
"""

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_FILES = ("index.html", "404.html", "style.css", "CNAME")

# The custom domain the site is served from; references to it are same-site.
SITE_HOST = "weather-skills.org"

# Elements with no closing tag (HTML void elements).
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)

# Attributes whose value the browser fetches as a resource, per element.
RESOURCE_ATTRS: dict[str, tuple[str, ...]] = {
    "link": ("href",),
    "script": ("src",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
}

# URL prefixes <a href> hyperlinks may point at. Everything else absolute
# is a third-party link target and fails the check.
ALLOWED_LINK_PREFIXES = (
    "https://github.com/rhiza-research/forecasting-skills",
    "https://github.com/rhiza-research/weather-skills-core",
    "https://agentskills.io",
    "https://github.com/rohitg00/skillkit",
)

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)


def _split_srcset(value: str) -> list[str]:
    """Return the URL part of each srcset candidate."""
    urls = []
    for candidate in value.split(","):
        parts = candidate.split()
        if parts:
            urls.append(parts[0])
    return urls


def _is_same_site(url: str) -> bool:
    """True if a reference is relative or targets the site's own host.

    Catches every absolute form: explicit scheme (any case, any scheme)
    and scheme-relative (`//host/...`).
    """
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return True
    return parts.scheme.lower() in ("", "http", "https") and parts.netloc.lower() == SITE_HOST


def _is_allowed_link(url: str) -> bool:
    """True for relative hyperlinks and allowlisted absolute ones."""
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return True
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme not in ("http", "https") or not netloc:
        return False
    normalized = f"{scheme}://{netloc}{parts.path}"
    for prefix in ALLOWED_LINK_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix) and normalized[len(prefix)] in "/?#":
            return True
    return False


class _DocumentChecker(HTMLParser):
    """Collect ids, hrefs, resource references, and tag-balance errors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.fragment_hrefs: list[str] = []
        self.link_hrefs: list[tuple[int, str]] = []
        self.resource_refs: list[tuple[int, str]] = []

    def _record_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        for key, value in attrs:
            if not value:
                continue
            if key == "id":
                self.ids.append(value)
            if tag == "a" and key == "href":
                if value.startswith("#"):
                    self.fragment_hrefs.append(value)
                else:
                    self.link_hrefs.append((line, value))
            if key in RESOURCE_ATTRS.get(tag, ()):
                if key == "srcset":
                    for url in _split_srcset(value):
                        self.resource_refs.append((line, url))
                else:
                    self.resource_refs.append((line, value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_attrs(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_attrs(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            self.errors.append(f"line {self.getpos()[0]}: end tag for void element </{tag}>")
            return
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> with no open element")
            return
        if self.stack[-1] != tag:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> closes <{self.stack[-1]}>")
            return
        self.stack.pop()

    def finish(self) -> None:
        self.close()
        for tag in self.stack:
            self.errors.append(f"<{tag}> never closed")


def _check_html(rel: Path, text: str, errors: list[str]) -> None:
    checker = _DocumentChecker()
    checker.feed(text)
    checker.finish()
    for message in checker.errors:
        errors.append(f"{rel}: malformed HTML: {message}")
    id_set = set(checker.ids)
    for element_id in sorted(id_set):
        occurrences = checker.ids.count(element_id)
        if occurrences > 1:
            errors.append(f"{rel}: duplicate id {element_id!r} ({occurrences} occurrences)")
    for href in checker.fragment_hrefs:
        if href[1:] not in id_set:
            errors.append(f"{rel}: anchor {href!r} has no matching id")
    for line, url in checker.resource_refs:
        if not _is_same_site(url):
            errors.append(f"{rel}: line {line}: third-party resource reference {url}")
    for line, url in checker.link_hrefs:
        if not _is_allowed_link(url):
            errors.append(f"{rel}: line {line}: hyperlink not in allowlist: {url}")


def _check_css(rel: Path, text: str, errors: list[str]) -> None:
    for match in _CSS_URL_RE.finditer(text):
        url = match.group(2).strip()
        if not _is_same_site(url):
            errors.append(f"{rel}: third-party url() reference {url}")


def _check(out_dir: Path) -> int:
    errors: list[str] = []

    for filename in REQUIRED_FILES:
        if not (out_dir / filename).is_file():
            errors.append(f"missing required file: {filename}")

    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(out_dir)
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            print(f"note: {rel}: not UTF-8; skipping text checks")
            continue
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            _check_html(rel, text, errors)
        elif suffix == ".css":
            _check_css(rel, text, errors)

    if errors:
        print("Site output check failed:", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"Site output OK: {out_dir}")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: check_site_output.py <output-dir>", file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1]).resolve()
    if not out_dir.is_dir():
        print(f"Error: {out_dir} is not a directory", file=sys.stderr)
        return 2
    return _check(out_dir)


if __name__ == "__main__":
    sys.exit(main())
