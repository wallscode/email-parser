import base64
import io
import json
import logging
import os
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser

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
SSM_PHONE_PATH = os.environ["SSM_PHONE_PATH"]
CLAUDE_MODEL = os.environ["CLAUDE_MODEL"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

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


def send_sms(message: str, sns_client) -> None:
    # Truncate to 160 characters
    if len(message) > 160:
        message = message[:157] + "..."
    sns_client.publish(TopicArn=SNS_TOPIC_ARN, Message=message)


def build_email_body(result: dict, s3_key: str) -> str:
    summary = result.get("summary", "No summary available.")
    action_items = result.get("action_items", [])
    action_html = (
        "<ul>" + "".join(f"<li>{item}</li>" for item in action_items) + "</ul>"
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
    <p style="color:grey;font-size:small;">Full analysis saved to S3: {s3_key}</p>
    </body></html>
    """


# ── Lambda entry point ─────────────────────────────────────────────────────


def handler(event, context):
    s3_client = boto3.client("s3")
    ses_client = boto3.client("ses")
    sns_client = boto3.client("sns")
    ssm_client = boto3.client("ssm")

    record = event["Records"][0]["s3"]
    bucket = record["bucket"]["name"]
    key = record["object"]["key"]
    logger.info("Processing s3://%s/%s", bucket, key)

    # Fetch and parse raw email
    response = s3_client.get_object(Bucket=bucket, Key=key)
    raw_email = response["Body"].read()
    msg = BytesParser(policy=email_policy.default).parsebytes(raw_email)

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
        claude_response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
        )
        raw_text = claude_response.content[0].text

        # Parse JSON from Claude response
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

        # Send SMS
        sms_body = f"Email parsed: {subject[:80]} — {result.get('summary', '')}"
        send_sms(sms_body, sns_client)
        logger.info("SMS notification sent")

    except Exception:
        logger.exception("Failed to process email %s", message_id)
        raise
