#!/usr/bin/env bash
set -euo pipefail

# Email Parser — deployment script
# Run from the repo root after sourcing your .env file:
#   source .env && bash deploy.sh

# ── Phase 1: Validate config ────────────────────────────────────────────────
echo "==> Validating environment..."
required_vars=(
  AWS_ACCOUNT_ID AWS_REGION
  DOMAIN PARSER_EMAIL_USER NOTIFY_EMAIL
  S3_BUCKET_NAME
)
for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: required environment variable '$var' is not set."
    exit 1
  fi
done
echo "    All required variables present."

# ── Phase 2: Install CDK dependencies ───────────────────────────────────────
echo "==> Installing CDK dependencies..."
pip install -q -r infrastructure/requirements.txt

# ── Phase 3: CDK bootstrap (idempotent) ─────────────────────────────────────
echo "==> Bootstrapping CDK..."
cd infrastructure
cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}"

# ── Phase 4: Deploy CDK stack ────────────────────────────────────────────────
echo "==> Deploying CDK stack..."
cdk deploy --require-approval never
cd ..

# ── Phase 5: Store secrets in SSM ───────────────────────────────────────────
echo "==> Storing secrets in SSM Parameter Store..."

SSM_API_KEY_PATH="${SSM_API_KEY_PATH:-/email-parser/claude-api-key}"
SSM_PHONE_PATH="${SSM_PHONE_PATH:-/email-parser/notify-phone}"

read -rsp "Enter your Claude API key: " CLAUDE_API_KEY
echo
aws ssm put-parameter \
  --name "$SSM_API_KEY_PATH" \
  --value "$CLAUDE_API_KEY" \
  --type SecureString \
  --region "$AWS_REGION" \
  --overwrite

read -rsp "Enter your notify phone number (E.164 format, e.g. +12125551234): " NOTIFY_PHONE
echo
aws ssm put-parameter \
  --name "$SSM_PHONE_PATH" \
  --value "$NOTIFY_PHONE" \
  --type SecureString \
  --region "$AWS_REGION" \
  --overwrite

echo "    SSM parameters written."

# ── Phase 6: SNS sandbox verification (if needed) ───────────────────────────
echo "==> Checking SNS sandbox..."
# TODO (ep-h5i7): call sns:CreateSMSSandboxPhoneNumber and prompt for OTP
#   aws sns create-sms-sandbox-phone-number --phone-number "$NOTIFY_PHONE"
echo "    If your account is in the SNS SMS sandbox, verify your phone number"
echo "    in the AWS console under SNS → Text messaging → Sandbox."

echo ""
echo "✓ Deployment complete."
