# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
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
_SKILL_VERSION = "0.1.8"


@weather_skill(
    "email-report",
    _SKILL_VERSION,
    extra_args=[
        ("--from", {"dest": "sender", "required": True, "help": "From: header"}),
        ("--to", {"required": True, "help": "Comma-separated recipients"}),
        ("--cc",),
        ("--reply-to",),
        ("--subject", {"required": True}),
        ("--body",),
        ("--body-file",),
        ("--attach", {"nargs": "*", "default": []}),
        ("--output", "-o", {"required": True}),
    ],
    mutex_groups={"body_source": {"args": ("body", "body_file"), "required": True}},
)
def compose(args):
    """Compose an RFC 5322 email and write it to disk as a .eml file. No SMTP."""
    sender, to, cc, reply_to, subject, body, body_file, attach, output = (
        args["sender"],
        args["to"],
        args["cc"],
        args["reply_to"],
        args["subject"],
        args["body"],
        args["body_file"],
        args["attach"],
        args["output"],
    )
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
