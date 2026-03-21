---
id: ep-l7m9
status: open
deps: [ep-b2c4]
links: [ep-d3e5, ep-h5i7]
created: 2026-03-21T00:00:00Z
type: chore
priority: 0
parent: ep-a1f3
tags: [secrets, security, setup]
---
# Configure GitHub secrets and AWS SSM parameters

Store all deployment-specific and sensitive values in the correct secret stores before any code is deployed. Nothing in this list should ever appear in committed files.

## GitHub Actions Secrets

Set these at https://github.com/wallscode/email-parser/settings/secrets/actions

| Secret Name | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user access key for CI/CD deployments |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret key for CI/CD deployments |
| `AWS_ACCOUNT_ID` | 12-digit AWS account number |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) |
| `DOMAIN` | Your custom domain (e.g. `example.com`) |
| `PARSER_EMAIL_USER` | Mailbox prefix for inbound emails (e.g. `parser`) |
| `NOTIFY_EMAIL` | Email address to receive parsed summaries |
| `S3_BUCKET_NAME` | S3 bucket name for raw emails and parsed output |
| `CLAUDE_MODEL` | Claude model ID (e.g. `claude-sonnet-4-20250514`) |
| `SSM_API_KEY_PATH` | SSM path for Claude API key (e.g. `/email-parser/claude-api-key`) |
| `SSM_PHONE_PATH` | SSM path for notify phone (e.g. `/email-parser/notify-phone`) |

## AWS SSM Parameter Store

Set these via AWS console or CLI after the CDK stack is deployed.
All parameters must be created as **SecureString** type.

```bash
aws ssm put-parameter \
  --name "/email-parser/claude-api-key" \
  --value "YOUR_CLAUDE_API_KEY" \
  --type SecureString \
  --overwrite

aws ssm put-parameter \
  --name "/email-parser/notify-phone" \
  --value "+1XXXXXXXXXX" \
  --type SecureString \
  --overwrite
```

| Parameter Path | Description |
|---|---|
| `/email-parser/claude-api-key` | Claude API key from console.anthropic.com |
| `/email-parser/notify-phone` | Notify phone number in E.164 format (e.g. `+12125551234`) |

## Local Development

For local deploys (not CI), export the GitHub secret values as shell environment variables before running `deploy.sh`:

```bash
export AWS_ACCOUNT_ID=...
export DOMAIN=...
export NOTIFY_EMAIL=...
export S3_BUCKET_NAME=...
# etc.
```

Consider storing these in a local `.env` file that is listed in `.gitignore` — never commit it.

## Acceptance Criteria

- All GitHub Actions secrets listed above are set in the repo settings
- Both SSM parameters exist as SecureString in the correct region
- `.env` (or equivalent local config) is in `.gitignore`
- `deploy.sh` and CDK stack resolve all values from environment / SSM with no hardcoded fallbacks
- A fresh `cdk synth` in CI (using GitHub secrets) produces a valid template with no missing values
