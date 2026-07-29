# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
# ]
# ///
"""Build a length-checked prefilled GitHub "new issue" URL for filing feedback."""

import math
import sys
from pathlib import Path
from urllib.parse import quote

from weather_skills_core import DataError, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.9"

# The target repository is fixed so feedback always lands in the right place and
# the caller cannot direct it elsewhere by guessing a slug.
REPO = "rhiza-research/forecasting-skills"

# The clean ceiling GitHub accepts for a prefilled new-issue URL. Above it GitHub
# starts erroring (500s) well before the hard 414, so this is the usable limit.
MAX_URL = 6800


def build_url(title: str, body: str) -> str:
    """Assemble the prefilled new-issue URL with title and body percent-encoded."""
    return (
        f"https://github.com/{REPO}/issues/new"
        f"?title={quote(title, safe='')}&body={quote(body, safe='')}"
    )


@weather_skill(
    name="submit-feedback",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--title", required=True, help="Issue title; must not be empty.")
@weather_skill.argument("--body", default=None, help="Issue body as a markdown string.")
@weather_skill.argument("--body-file", default=None, help="Path to a file holding the issue body.")
def submit_feedback(title, body, body_file):
    """Build a length-checked prefilled GitHub "new issue" URL for filing feedback.

    Stateless formatter and validator. The caller authors the title and body; this
    script URL-encodes them into a
    https://github.com/<repo>/issues/new?title=...&body=... link for a fixed target
    repository and checks the assembled URL against the clean length ceiling GitHub
    accepts. Within budget it prints the URL to stdout; over budget it reports the
    overage on stderr so the caller can trim and retry. It never creates an issue,
    holds a credential, or makes a network request -- the user files the issue by
    clicking the link and pressing Submit on github.com, which posts under their own
    GitHub account.
    """
    if not title.strip():
        raise UsageError("--title must not be empty.")

    if body is None and body_file is None:
        raise UsageError("one of --body or --body-file is required.")
    if body is not None and body_file is not None:
        raise UsageError("use only one of --body or --body-file.")

    if body is None:
        body_path = Path(body_file)
        if not body_path.is_file():
            raise UsageError(f"--body-file {body_path} not found.")
        try:
            body = body_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise UsageError(f"--body-file {body_path} is not valid UTF-8 text.") from None
        except OSError as exc:
            raise UsageError(f"cannot read --body-file {body_path}: {exc}") from None

    fixed = len(build_url(title, ""))
    if fixed >= MAX_URL:
        raise UsageError(
            f"the title alone encodes to {fixed} characters, at or above the "
            f"{MAX_URL}-character URL limit; shorten the title."
        )

    url = build_url(title, body)
    total = len(url)

    if total > MAX_URL:
        overage = total - MAX_URL
        # Estimate source characters to cut using this body's own measured encode
        # ratio rather than a generic factor, so the guidance fits the actual input.
        ratio = len(quote(body, safe="")) / len(body) if body else 1.0
        to_cut = math.ceil(overage / ratio)
        # The retryable over-budget signal: an unprefixed stderr message plus
        # exit 1, which the calling agent consumes to trim the body and retry.
        # prefix=False keeps the "Error: " prefix off the printed line.
        raise DataError(
            f"Body too long: assembled URL is {total} chars, max is {MAX_URL} "
            f"(over by {overage}). Cut roughly {to_cut} characters from the body and "
            "retry.",
            prefix=False,
        )

    print(url)
    print(
        f"Within budget: URL is {total}/{MAX_URL} chars. Give this link to the user "
        "and tell them to click it, review the prefilled issue, and press Submit "
        f"-- it posts to {REPO} under their own GitHub account (GitHub login "
        "required).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    submit_feedback()
