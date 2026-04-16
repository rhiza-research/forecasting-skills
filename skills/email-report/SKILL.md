---
name: email-report
description: Assemble an email message with optional file attachments and write it to disk as a standards-compliant .eml file. Mocks actual SMTP delivery — does not send. Use at the end of a pipeline to materialize what would have been sent.
license: MIT
compatibility: Requires Python 3.10+ and uv. Stdlib only.
metadata:
  openclaw:
    requires:
      bins:
        - uv
---

# email-report

Produces an RFC 5322 message (`.eml`) from the provided metadata and attachments. No network calls; nothing is sent. The output file is a real deliverable that could later be handed to any SMTP gateway — it is the exact same object an SMTP client would transmit.

## When to use

- End-of-pipeline egress during testing / replay of the daily workflow, where the intent is to produce the email artifact but not actually send it.
- Anywhere the workflow would have called an SMTP action.

## Usage

```
uv run scripts/compose.py --from SENDER --to "a@x,b@y" --subject "..." \
    --body-file BODY.txt --output <mail.eml> [--attach f1 f2 ...] \
    [--reply-to ADDR] [--cc "c@z,..."]
```

### Arguments
- `--from` — From: header (e.g. `"Sender Name <sender@example.com>"`).
- `--to` — comma-separated recipient list.
- `--cc` — optional comma-separated cc list.
- `--reply-to` — optional Reply-To header.
- `--subject` — subject line.
- `--body` — inline body text. Mutually exclusive with `--body-file`.
- `--body-file` — path to body text file. Mutually exclusive with `--body`.
- `--attach` — zero or more file paths to attach. Missing files produce a warning and are skipped (so a pipeline failure partway through still yields a reviewable email).
- `--output`, `-o` — output `.eml` path.

### Output

A single `.eml` file on disk. It includes multipart/mixed boundaries, correctly encoded attachments, and a generated `Date:` and `Message-ID:` header.

## Example

```bash
uv run scripts/compose.py --from "Sender <sender@example.com>" \
    --to "recipient1@example.com,recipient2@example.com" \
    --subject "Daily Outlook" --body-file body.txt \
    --attach /tmp/weekly.png /tmp/dekadal.png \
    --output /tmp/daily.eml
```
