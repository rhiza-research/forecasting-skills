# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
# ]
# ///
"""Compose an RFC 5322 email and write it to disk as a .eml file. No SMTP."""

import mimetypes
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

@weather_skill(
    name="email-report",
    version=_SKILL_VERSION
)
@weather_skill.argument("--from", dest="sender", required=True, help="From: header")
@weather_skill.argument("--to", required=True, help="Comma-separated recipients")
@weather_skill.argument("--cc")
@weather_skill.argument("--reply-to")
@weather_skill.argument("--subject", required=True)
@weather_skill.argument("--body")
@weather_skill.argument("--body-file")
@weather_skill.argument("--attach", nargs="*", default=[])
def compose(sender, to, cc, reply_to, subject, body, body_file, attach, output, **kwargs):
    """Compose an RFC 5322 email and write it to disk as a .eml file. No SMTP."""
    if (body is None) == (body_file is None):
        raise UsageError("pass exactly one of --body or --body-file.")

    if body is None:
        body_path = Path(body_file)
        if not body_path.exists():
            raise UsageError(f"body file {body_path} not found.")
        body = body_path.read_text()

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="rhiza.local")
    msg.set_content(body)

    for path_str in attach:
        path = Path(path_str)
        if not path.exists():
            print(f"Warning: attachment {path} not found, skipping.", file=sys.stderr)
            continue
        ctype, encoding = mimetypes.guess_type(path.name)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(msg))
    print(
        f"Wrote: {output} ({len(msg.get_payload())} parts, {out.stat().st_size} bytes)",
        file=sys.stderr,
    )

if __name__ == "__main__":
    compose()
