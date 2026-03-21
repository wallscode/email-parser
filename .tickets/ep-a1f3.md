---
id: ep-a1f3
status: open
deps: []
links: []
created: 2026-03-21T00:00:00Z
type: epic
priority: 0
tags: [infrastructure, pipeline]
---
# Email Parser AWS Pipeline

End-to-end serverless pipeline that receives forwarded emails via AWS SES, extracts and analyzes attachments using the Claude API, stores structured JSON results in S3, and notifies the user via email and SMS.

## Design

See `email-parser-project-plan-v2.docx` for the complete specification. Architecture:

```
Outlook → SES → S3 (raw-emails/) → Lambda → Claude API
    Lambda outputs: S3 (parsed-output/), SES email, SNS SMS
```

AWS services: SES, S3, Lambda, Route 53, SNS, SSM Parameter Store, CloudWatch.
Infrastructure managed via AWS CDK (Python).

## Acceptance Criteria

- Forwarded emails received at `parser@<domain>` and stored in S3
- Attachments (PDF, DOCX, XLSX, TXT, CSV, PNG, JPG) extracted and analyzed by Claude
- Structured JSON result saved to `parsed-output/YYYY-MM-DD/<message-id>.json`
- Summary email sent to configured notify address
- SMS notification sent to configured phone number
- All infrastructure deployed via `bash deploy.sh` from a clean environment
