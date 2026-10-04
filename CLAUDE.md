# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Serverless AWS pipeline that receives forwarded emails (from Outlook), extracts attachments, analyzes documents using Claude API, saves structured JSON results to S3, and notifies the user via email and SMS.

The full implementation plan is in `email-parser-project-plan-v2.docx`.

## Architecture

```
Outlook → SES (parser@yourdomain.com) → S3 (raw-emails/) → Lambda
    Lambda:
        ├─ Extracts attachments (PDF, DOCX, XLSX, TXT, CSV, PNG, JPG)
        ├─ Converts to text (or base64 for images)
        ├─ Calls Claude API for document analysis
        ├─ Saves JSON to S3 (parsed-output/YYYY-MM-DD/MESSAGE_ID.json)
        ├─ Sends email summary via SES
        └─ Sends SMS via SNS
```

**AWS services:** SES, S3, Lambda, Route 53, SNS, SSM Parameter Store, CloudWatch
**Python runtime:** 3.12
**Infrastructure:** AWS CDK (Python)

## Project Structure

```
email-parser/
├── config.py                          # Reads all settings from env vars (placeholders only)
├── deploy.sh                          # Routine deploy (deployer) or --initial (admin)
├── scripts/setup-iam.sh               # Admin-only: creates deployer user + CloudFormation role
├── iam/                               # Policy templates ({{PLACEHOLDERS}} filled at run time)
│   ├── deployer-policy.json           # email-parser-deployer user
│   ├── cfn-execution-policy.json      # email-parser-cloudformation role
│   ├── cfn-trust-policy.json
│   ├── stack-policy.json              # Stack policy: only the Lambda function may be updated
│   └── stack-policy-admin-update.json # Temporarily applied during admin deploys
├── infrastructure/
│   ├── app.py                         # CDK app entry point (CliCredentialsStackSynthesizer)
│   ├── requirements.txt               # Pinned CDK deps
│   └── stacks/email_parser_stack.py   # All AWS resources defined here
└── lambda/
    ├── handler.py                      # Core Lambda logic
    └── requirements.txt               # Lambda deps (anthropic, pypdf, python-docx, openpyxl)
```

## IAM Model (least privilege)

All routine AWS work uses the `email-parser-deployer` IAM user, signed in with `aws login`. It has no access keys. It can only:
- deploy `EmailParserStack` through change sets that use the `email-parser-cloudformation` role. That role can only update the `email-parser` function's code and configuration.
- upload CDK assets under the `email-parser/` prefix
- run tests: put to `raw-emails/`, read `parsed-output/`, invoke the function, read its logs

Everything else is admin-only and done with `deploy.sh --initial`: S3 bucket settings, IAM roles, DNS, SES, the bucket policy, SNS, SSM secrets and the stack policy. A stack policy and termination protection back this up. A routine deploy that touches any other resource fails and rolls back, which is intended.

If a deploy fails with AccessDenied, add the narrowest possible action to the matching file in `iam/`. Applying it needs an admin to re-run `scripts/setup-iam.sh`.

## Key Commands

```bash
# Routine deploy (email-parser-deployer)
source .env && bash deploy.sh

# Admin only: create/refresh IAM, then full infrastructure deploy + one-time setup
source .env && bash scripts/setup-iam.sh
source .env && bash deploy.sh --initial
SMS_OTP=123456 bash deploy.sh --initial   # finish SMS sandbox phone verification
```

## Public Repo Safety

**This repo is public. Never commit personal or account-specific information.**

The following must never appear in any committed file:
- Real domain names, email addresses, or phone numbers
- AWS account IDs, ARNs, or resource names that include account info
- API keys, tokens, or credentials of any kind
- S3 bucket names, Lambda function names, or any names that reveal personal infrastructure

### How configuration is handled

All deployment-specific values are supplied at runtime via environment variables — never stored in `config.py` or any committed file. `config.py` must only contain placeholder/example values (e.g. `os.environ.get('DOMAIN', 'example.com')`).

Sensitive runtime values (Claude API key, notify phone number) are stored in AWS SSM Parameter Store and injected into Lambda via environment variables at deploy time.

For CI/CD, all values are stored as GitHub Actions secrets and passed to the CDK deploy step — no values are hardcoded in workflow files.

**If you are ever about to write a real domain, email, account ID, or any personally identifying string into a file, stop and use an environment variable or placeholder instead.**

## Configuration

`config.py` reads all deployment-specific values from environment variables:

```python
import os

DOMAIN = os.environ['DOMAIN']                    # e.g. export DOMAIN=example.com
PARSER_EMAIL_USER = os.environ.get('PARSER_EMAIL_USER', 'parser')
NOTIFY_EMAIL = os.environ['NOTIFY_EMAIL']
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
AWS_ACCOUNT_ID = os.environ['AWS_ACCOUNT_ID']
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-20250514')
S3_BUCKET_NAME = os.environ['S3_BUCKET_NAME']
HOSTED_ZONE_ID = os.environ['HOSTED_ZONE_ID']
SSM_API_KEY_PATH = os.environ.get('SSM_API_KEY_PATH', '/email-parser/claude-api-key')
SSM_PHONE_PATH = os.environ.get('SSM_PHONE_PATH', '/email-parser/notify-phone')
```

Set these in your local shell before deploying (or via GitHub Actions secrets for CI). Never put real values in the file itself.

## Testing

End-to-end test by uploading a `.eml` file to the S3 `raw-emails/` prefix and invoking Lambda with a synthetic S3 event. Check `parsed-output/` for the result JSON and CloudWatch logs at `/aws/lambda/email-parser`.

```bash
# Invoke Lambda directly with a test event
aws lambda invoke --function-name email-parser \
  --payload file://test-event.json \
  --log-type Tail output.json
```

Test fixtures (`test-event.json`, `test.eml`) must use placeholder values only — no real bucket names, account IDs, or email addresses.

## Prerequisites

- AWS CLI configured with admin access
- AWS CDK installed: `npm install -g aws-cdk`
- Python 3.12+
- Domain hosted in Route 53 (no existing MX record)
- For SMS to US numbers: an SNS/End User Messaging origination identity (e.g. registered toll-free number)
- Claude API key from console.anthropic.com
