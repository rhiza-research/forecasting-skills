# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
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

import argparse
import math
import sys
from pathlib import Path
from urllib.parse import quote

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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--title", required=True, help="Issue title; must not be empty.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--body", help="Issue body as a markdown string.")
    grp.add_argument("--body-file", help="Path to a file holding the issue body.")
    args = p.parse_args()

    if not args.title.strip():
        print("Error: --title must not be empty.", file=sys.stderr)
        sys.exit(2)

    if args.body is not None:
        body = args.body
    else:
        body_path = Path(args.body_file)
        if not body_path.is_file():
            print(f"Error: --body-file {body_path} not found.", file=sys.stderr)
            sys.exit(2)
        try:
            body = body_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(
                f"Error: --body-file {body_path} is not valid UTF-8 text.",
                file=sys.stderr,
            )
            sys.exit(2)
        except OSError as exc:
            print(f"Error: cannot read --body-file {body_path}: {exc}", file=sys.stderr)
            sys.exit(2)

    fixed = len(build_url(args.title, ""))
    if fixed >= MAX_URL:
        print(
            f"Error: the title alone encodes to {fixed} characters, at or above the "
            f"{MAX_URL}-character URL limit; shorten the title.",
            file=sys.stderr,
        )
        sys.exit(2)

    url = build_url(args.title, body)
    total = len(url)

    if total > MAX_URL:
        overage = total - MAX_URL
        # Estimate source characters to cut using this body's own measured encode
        # ratio rather than a generic factor, so the guidance fits the actual input.
        ratio = len(quote(body, safe="")) / len(body) if body else 1.0
        to_cut = math.ceil(overage / ratio)
        print(
            f"Body too long: assembled URL is {total} chars, max is {MAX_URL} "
            f"(over by {overage}). Cut roughly {to_cut} characters from the body and "
            "retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(url)
    print(
        f"Within budget: URL is {total}/{MAX_URL} chars. Give this link to the user "
        "and tell them to click it, review the prefilled issue, and press Submit "
        f"-- it posts to {REPO} under their own GitHub account (GitHub login "
        "required).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
