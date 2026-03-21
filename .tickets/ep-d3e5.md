---
id: ep-d3e5
status: closed
deps: [ep-b2c4]
links: []
created: 2026-03-21T00:00:00Z
type: feature
priority: 1
parent: ep-a1f3
tags: [infrastructure, cdk, aws]
---
# CDK infrastructure stack

Implement `infrastructure/stacks/email_parser_stack.py` to provision all AWS resources needed for the pipeline.

## Design

Single CDK stack covering:

- **S3 bucket** — two logical prefixes: `raw-emails/` (30-day lifecycle rule) and `parsed-output/` (no expiry). S3 event notification triggers Lambda on `raw-emails/` object creation.
- **SES domain identity** — verifies the domain and creates a receipt rule that stores inbound mail to S3 under `raw-emails/`.
- **Route 53** — DKIM CNAME records (3), MX record pointing to SES inbound endpoint, and any required verification TXT records.
- **Lambda function** — Python 3.12, source from `lambda/`, bundled with dependencies from `lambda/requirements.txt`. Environment variables: `BUCKET_NAME`, `NOTIFY_EMAIL`, `SSM_API_KEY_PATH`, `SSM_PHONE_PATH`, `CLAUDE_MODEL`.
- **SSM Parameter Store** — two SecureString parameters: Claude API key and notify phone. Created as placeholders; values set by `deploy.sh` post-deploy.
- **SNS topic** — used for SMS. Lambda publishes to it; `deploy.sh` subscribes the phone number.
- **IAM** — Lambda execution role with least-privilege policies for S3, SES send, SSM get, SNS publish, CloudWatch logs.

All resource names derived from `config.py` values.

## Acceptance Criteria

- `cdk synth` produces a valid CloudFormation template with no errors
- `cdk deploy` creates all resources in the correct region
- S3 → Lambda trigger fires on object creation under `raw-emails/`
- Route 53 records match SES DKIM tokens returned by AWS
- Lambda environment variables resolve to correct config values
- IAM role grants only the permissions listed above
