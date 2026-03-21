---
id: ep-j6k8
status: closed
deps: [ep-h5i7]
links: []
created: 2026-03-21T00:00:00Z
type: task
priority: 2
parent: ep-a1f3
tags: [testing, verification]
---
# End-to-end verification

Verify the deployed pipeline works correctly by running a test email through every stage.

## Design

Test using a synthetic `.eml` file uploaded directly to S3, bypassing the need to send a real email through SES during testing.

**Verification checklist:**
1. SES domain identity status is `Active` in AWS console / CLI
2. Route 53 has correct DKIM CNAME records and MX record for the domain
3. Lambda function exists with correct environment variables set
4. S3 bucket has event notification configured for `raw-emails/` prefix
5. Upload a test `.eml` file to `s3://BUCKET/raw-emails/test.eml`
6. Invoke Lambda with a synthetic S3 event JSON pointing to that object
7. Check `s3://BUCKET/parsed-output/` for a result JSON file
8. Verify summary email received at `NOTIFY_EMAIL`
9. Verify SMS received on configured phone number
10. Check CloudWatch log group `/aws/lambda/email-parser` for no errors

Create a `test-event.json` and a sample `test.eml` (with a small PDF or text attachment) to make this repeatable.

## Acceptance Criteria

- All 10 checklist items pass
- `test-event.json` and `test.eml` committed to repo for future re-use
- CloudWatch logs show no unhandled exceptions
- Result JSON contains Claude's structured analysis of the attachment
