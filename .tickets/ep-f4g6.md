---
id: ep-f4g6
status: open
deps: [ep-b2c4]
links: [ep-d3e5]
created: 2026-03-21T00:00:00Z
type: feature
priority: 1
parent: ep-a1f3
tags: [lambda, python, claude-api]
---
# Lambda email processing handler

Implement `lambda/handler.py` — the core business logic that processes inbound emails and produces analysis results.

## Design

Entry point: `handler(event, context)` triggered by S3 `ObjectCreated` event.

**Processing pipeline:**
1. Parse S3 event to get bucket + key of raw email
2. Fetch raw email bytes from S3; parse with Python `email` stdlib
3. Iterate MIME parts, extract attachments by content type:
   - PDF → `pypdf` text extraction
   - DOCX → `python-docx` text extraction
   - XLSX → `openpyxl` text extraction (all sheets)
   - TXT / CSV → decode UTF-8
   - PNG / JPG / JPEG → base64-encode for Claude vision
4. Build Claude API request: system prompt describing extraction task, user message with email body + all attachment content
5. Fetch Claude API key from SSM (`/email-parser/claude-api-key`); call `anthropic.Anthropic` client with model from env var
6. Save Claude response JSON to S3 at `parsed-output/YYYY-MM-DD/<message-id>.json`
7. Send HTML summary email via SES (`send_email`) to `NOTIFY_EMAIL`
8. Fetch phone from SSM (`/email-parser/notify-phone`); publish short SMS (≤160 chars) to SNS topic

**Error handling:** wrap steps 4-8 in try/except, log to CloudWatch, re-raise so Lambda marks invocation failed.

## Acceptance Criteria

- Each supported attachment type is correctly converted to text/base64
- Claude API called with a structured prompt; raw response saved to S3
- Summary email delivered to `NOTIFY_EMAIL` with subject and body
- SMS ≤ 160 characters published to SNS
- Unsupported attachment types are skipped with a log warning (not an error)
- Unit-testable helper functions for each extraction type (no AWS calls inside them)
