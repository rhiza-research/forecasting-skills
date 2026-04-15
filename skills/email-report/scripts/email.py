# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Compose an RFC 5322 email and write it to disk as a .eml file. No SMTP."""

import argparse
import mimetypes
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="sender", required=True, help="From: header")
    p.add_argument("--to", required=True, help="Comma-separated recipients")
    p.add_argument("--cc")
    p.add_argument("--reply-to")
    p.add_argument("--subject", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--body")
    grp.add_argument("--body-file")
    p.add_argument("--attach", nargs="*", default=[])
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    if args.body is not None:
        body = args.body
    else:
        body_path = Path(args.body_file)
        if not body_path.exists():
            print(f"Error: body file {body_path} not found.", file=sys.stderr)
            sys.exit(2)
        body = body_path.read_text()

    msg = EmailMessage()
    msg["From"] = args.sender
    msg["To"] = args.to
    if args.cc:
        msg["Cc"] = args.cc
    if args.reply_to:
        msg["Reply-To"] = args.reply_to
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="rhiza.local")
    msg.set_content(body)

    for path_str in args.attach:
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

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(msg))
    print(
        f"Wrote: {args.output} ({len(msg.get_payload())} parts, "
        f"{sum(1 for _ in out.open('rb'))} bytes)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
