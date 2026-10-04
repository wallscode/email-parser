import base64
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import parseaddr

import anthropic
import boto3
import openpyxl
import pypdf
from docx import Document

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ["BUCKET_NAME"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]
SSM_API_KEY_PATH = os.environ["SSM_API_KEY_PATH"]
CLAUDE_MODEL = os.environ["CLAUDE_MODEL"]
# Only mail from these addresses is processed (comma-separated, case-insensitive).
ALLOWED_SENDERS = {
    a.strip().lower() for a in os.environ["ALLOWED_SENDERS"].split(",") if a.strip()
}

SYSTEM_PROMPT = """You are a document analysis assistant. The user will provide the body and attachments
from an email. Extract and summarize the key information from all documents provided.

For each document, identify:
- Document type and purpose
- Key parties involved (names, companies, roles)
- Important dates and deadlines
- Financial figures or amounts
- Action items or required responses
- Any urgent or time-sensitive information

Return your response as a JSON object with these top-level keys:
- "summary": a 2-3 sentence plain-English summary of the email and all attachments
- "documents": an array of objects, one per attachment, each with "filename", "type", and "key_points"
- "action_items": an array of strings describing anything requiring the recipient's attention
- "urgent": boolean, true if anything requires a response within 48 hours"""


# ── Attachment extraction helpers (no AWS calls) ───────────────────────────


def extract_pdf(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_docx(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    return "\n".join(para.text for para in doc.paragraphs)


def extract_xlsx(data: bytes) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for sheet in wb.worksheets:
        lines.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            row_text = "\t".join("" if v is None else str(v) for v in row)
            if row_text.strip():
                lines.append(row_text)
    return "\n".join(lines)


def extract_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def image_to_content_block(data: bytes, content_type: str) -> dict:
    media_type = content_type.split(";")[0].strip()
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("utf-8"),
        },
    }


def extract_attachments(msg) -> list:
    """Return a list of Claude content blocks from all email attachments."""
    blocks = []
    for part in msg.walk():
        content_disposition = part.get_content_disposition()
        if content_disposition not in ("attachment", "inline"):
            continue

        filename = part.get_filename() or "unnamed"
        content_type = part.get_content_type()
        data = part.get_payload(decode=True)
        if not data:
            continue

        try:
            if content_type == "application/pdf":
                text = extract_pdf(data)
                blocks.append({"type": "text", "text": f"[Attachment: {filename}]\n{text}"})

            elif content_type in (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/msword",
            ):
                text = extract_docx(data)
                blocks.append({"type": "text", "text": f"[Attachment: {filename}]\n{text}"})

            elif content_type in (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
            ):
                text = extract_xlsx(data)
                blocks.append({"type": "text", "text": f"[Attachment: {filename}]\n{text}"})

            elif content_type in ("text/plain", "text/csv"):
                text = extract_text(data)
                blocks.append({"type": "text", "text": f"[Attachment: {filename}]\n{text}"})

            elif content_type in ("image/png", "image/jpeg"):
                blocks.append(image_to_content_block(data, content_type))

            else:
                logger.warning("Skipping unsupported attachment type: %s (%s)", filename, content_type)

        except Exception:
            logger.warning("Failed to extract attachment %s", filename, exc_info=True)

    return blocks


# ── AWS helpers ────────────────────────────────────────────────────────────


def get_ssm_parameter(name: str, ssm_client) -> str:
    response = ssm_client.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def save_to_s3(result: dict, message_id: str, s3_client) -> str:
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_id = message_id.strip("<>").replace("/", "_")
    key = f"parsed-output/{date_prefix}/{safe_id}.json"
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(result, indent=2),
        ContentType="application/json",
    )
    return key


def send_email(subject: str, body_html: str, ses_client) -> None:
    ses_client.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [NOTIFY_EMAIL]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": body_html}},
        },
    )


def build_email_body(result: dict, s3_key: str) -> str:
    # Everything from Claude is derived from the forwarded email, which may be written
    # by anyone — escape it so it can't inject HTML (e.g. phishing links) into the summary.
    summary = html.escape(str(result.get("summary", "No summary available.")))
    action_items = result.get("action_items", [])
    action_html = (
        "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in action_items) + "</ul>"
        if action_items
        else "<p>None</p>"
    )
    urgent_banner = (
        '<p style="color:red;font-weight:bold;">⚠ Urgent: response required within 48 hours.</p>'
        if result.get("urgent")
        else ""
    )
    return f"""
    <html><body>
    {urgent_banner}
    <h2>Email Parser Summary</h2>
    <p>{summary}</p>
    <h3>Action Items</h3>
    {action_html}
    <p style="color:grey;font-size:small;">Full analysis saved to S3: {html.escape(s3_key)}</p>
    </body></html>
    """


# ── Sender allowlist ───────────────────────────────────────────────────────


def sender_is_allowed(msg) -> bool:
    """Process only mail from an allowlisted address that SES authenticated.

    Anyone can email the parser address, and each processed email costs a Claude
    call. The From header alone is trivially forged, so also require SES's own
    DMARC verdict for the sender's domain (SPF or DKIM passed and aligned with the
    From domain). SES prepends its Authentication-Results header, so only the
    first one is trusted; any further down could have been written by the sender.
    """
    _, address = parseaddr(str(msg.get("From", "")))
    address = address.lower()
    if address not in ALLOWED_SENDERS:
        logger.warning("Dropping email from non-allowlisted sender: %s", address or "(none)")
        return False

    if str(msg.get("X-SES-Virus-Verdict", "")).strip().upper() == "FAIL":
        logger.warning("Dropping email from %s: SES virus scan failed", address)
        return False

    results = msg.get_all("Authentication-Results") or []
    ses_result = str(results[0]) if results else ""
    domain = re.escape(address.rsplit("@", 1)[-1])
    if not (
        ses_result.strip().lower().startswith("amazonses.com")
        and re.search(rf"\bdmarc=pass\b[^;]*\bheader\.from={domain}\b", ses_result, re.IGNORECASE)
    ):
        logger.warning("Dropping email from %s: SES authentication did not pass", address)
        return False

    return True


# ── Lambda entry point ─────────────────────────────────────────────────────


def handler(event, context):
    s3_client = boto3.client("s3")
    ses_client = boto3.client("ses")
    ssm_client = boto3.client("ssm")

    record = event["Records"][0]["s3"]
    bucket = record["bucket"]["name"]
    key = record["object"]["key"]
    logger.info("Processing s3://%s/%s", bucket, key)

    # Fetch and parse raw email
    response = s3_client.get_object(Bucket=bucket, Key=key)
    raw_email = response["Body"].read()
    msg = BytesParser(policy=email_policy.default).parsebytes(raw_email)

    if not sender_is_allowed(msg):
        return

    message_id = msg.get("Message-ID", key.split("/")[-1])
    subject = msg.get("subject", "(no subject)")
    sender = msg.get("from", "unknown")

    # Build email body text block
    body_text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and part.get_content_disposition() is None:
            body_text = part.get_content()
            break

    content_blocks = [
        {
            "type": "text",
            "text": f"Email from: {sender}\nSubject: {subject}\n\nBody:\n{body_text}",
        }
    ]
    content_blocks.extend(extract_attachments(msg))

    try:
        # Call Claude
        api_key = get_ssm_parameter(SSM_API_KEY_PATH, ssm_client)
        client = anthropic.Anthropic(api_key=api_key)
        claude_response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            # Room for thinking plus the reply.
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
            # Summarizing and extracting from documents doesn't need deep reasoning.
            output_config={"effort": "low"},
            # If a safety classifier declines, retry server-side on another model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        if claude_response.stop_reason == "refusal":
            # Don't raise: Lambda would retry, and the retry would be declined too.
            details = claude_response.stop_details
            category = details.category if details else None
            logger.warning("Claude declined to analyze email %s (category: %s)", message_id, category)
            result = {
                "summary": f"Claude declined to analyze this email (category: {category}).",
                "documents": [], "action_items": [], "urgent": False,
            }
        else:
            # The response can begin with thinking blocks; the answer is in the text blocks.
            raw_text = "".join(b.text for b in claude_response.content if b.type == "text").strip()
            # Tolerate the JSON being wrapped in a Markdown code fence.
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError:
                result = {"summary": raw_text, "documents": [], "action_items": [], "urgent": False}

        result["_meta"] = {
            "message_id": message_id,
            "subject": subject,
            "sender": sender,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        # Save to S3
        s3_key = save_to_s3(result, message_id, s3_client)
        logger.info("Saved analysis to %s", s3_key)

        # Send email summary
        email_subject = f"[Email Parser] {subject}"
        send_email(email_subject, build_email_body(result, s3_key), ses_client)
        logger.info("Summary email sent to %s", NOTIFY_EMAIL)

    except Exception:
        logger.exception("Failed to process email %s", message_id)
        raise
