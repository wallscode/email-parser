import json


def handler(event, context):
    # TODO (ep-f4g6): implement email processing pipeline
    # Steps:
    #   1. Parse S3 event to get bucket + key of raw email
    #   2. Fetch and parse raw email from S3 (Python email stdlib)
    #   3. Extract attachments: PDF (pypdf), DOCX (python-docx),
    #      XLSX (openpyxl), TXT/CSV (utf-8), images (base64)
    #   4. Call Claude API with email body + attachment content
    #   5. Save Claude response JSON to S3 parsed-output/YYYY-MM-DD/<message-id>.json
    #   6. Send HTML summary email via SES to NOTIFY_EMAIL
    #   7. Publish SMS notification via SNS (≤160 chars)
    print(json.dumps({"status": "not implemented", "event": event}))
