#!/usr/bin/env bash
set -euo pipefail

# Email Parser — deployment script
# Run from the repo root after sourcing your .env file:
#   source .env && bash deploy.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Phase 1: Validate config ───────────────────────────────────────────────
echo "==> Validating environment..."
required_vars=(
  AWS_ACCOUNT_ID AWS_REGION
  DOMAIN PARSER_EMAIL_USER NOTIFY_EMAIL
  S3_BUCKET_NAME
)
for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: required environment variable '$var' is not set."
    echo "       Copy .env.example to .env, fill in your values, then: source .env"
    exit 1
  fi
done
echo "    All required variables present."

SSM_API_KEY_PATH="${SSM_API_KEY_PATH:-/email-parser/claude-api-key}"
SSM_PHONE_PATH="${SSM_PHONE_PATH:-/email-parser/notify-phone}"

# ── Phase 2: Install CDK dependencies ─────────────────────────────────────
echo "==> Installing CDK dependencies..."
pip install -q -r "$REPO_ROOT/infrastructure/requirements.txt"

# ── Phase 3: CDK bootstrap (idempotent) ───────────────────────────────────
echo "==> Bootstrapping CDK..."
(cd "$REPO_ROOT/infrastructure" && cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}")

# ── Phase 4: Build Lambda package ─────────────────────────────────────────
echo "==> Building Lambda package..."
LAMBDA_BUILD_DIR="$REPO_ROOT/dist/lambda"
rm -rf "$LAMBDA_BUILD_DIR"
mkdir -p "$LAMBDA_BUILD_DIR"
pip install -q -r "$REPO_ROOT/lambda/requirements.txt" -t "$LAMBDA_BUILD_DIR"
cp "$REPO_ROOT/lambda/"*.py "$LAMBDA_BUILD_DIR/"
echo "    Lambda package built at dist/lambda/"

# ── Phase 5: Deploy CDK stack ─────────────────────────────────────────────
echo "==> Deploying CDK stack..."
(cd "$REPO_ROOT/infrastructure" && cdk deploy --require-approval never)

# ── Phase 6: Store secrets in SSM ─────────────────────────────────────────
echo "==> Checking SSM parameters..."

ssm_param_exists() {
  aws ssm get-parameter --name "$1" --region "$AWS_REGION" \
    --query "Parameter.Value" --output text 2>/dev/null
}

# Claude API key
if ssm_param_exists "$SSM_API_KEY_PATH" > /dev/null 2>&1; then
  echo "    $SSM_API_KEY_PATH already exists — skipping (re-run with --force-ssm to overwrite)."
else
  read -rsp "Enter your Claude API key: " CLAUDE_API_KEY
  echo
  aws ssm put-parameter \
    --name "$SSM_API_KEY_PATH" \
    --value "$CLAUDE_API_KEY" \
    --type SecureString \
    --region "$AWS_REGION" \
    --overwrite
  echo "    Claude API key stored."
fi

# Notify phone number
if ssm_param_exists "$SSM_PHONE_PATH" > /dev/null 2>&1; then
  echo "    $SSM_PHONE_PATH already exists — skipping (re-run with --force-ssm to overwrite)."
  NOTIFY_PHONE="$(aws ssm get-parameter --name "$SSM_PHONE_PATH" \
    --with-decryption --region "$AWS_REGION" \
    --query "Parameter.Value" --output text)"
else
  read -rsp "Enter your notify phone number (E.164 format, e.g. +12125551234): " NOTIFY_PHONE
  echo
  aws ssm put-parameter \
    --name "$SSM_PHONE_PATH" \
    --value "$NOTIFY_PHONE" \
    --type SecureString \
    --region "$AWS_REGION" \
    --overwrite
  echo "    Notify phone stored."
fi

# Handle --force-ssm flag to overwrite existing SSM params
if [[ "${1:-}" == "--force-ssm" ]]; then
  echo "==> --force-ssm: overwriting SSM parameters..."
  read -rsp "Enter your Claude API key: " CLAUDE_API_KEY; echo
  aws ssm put-parameter --name "$SSM_API_KEY_PATH" --value "$CLAUDE_API_KEY" \
    --type SecureString --region "$AWS_REGION" --overwrite
  read -rsp "Enter your notify phone number (E.164): " NOTIFY_PHONE; echo
  aws ssm put-parameter --name "$SSM_PHONE_PATH" --value "$NOTIFY_PHONE" \
    --type SecureString --region "$AWS_REGION" --overwrite
  echo "    SSM parameters updated."
fi

# ── Phase 7: SNS sandbox verification ─────────────────────────────────────
echo "==> Checking SNS SMS sandbox status..."

SANDBOX_STATUS="$(aws sns get-sms-sandbox-account-status \
  --region "$AWS_REGION" \
  --query "IsInSandbox" --output text 2>/dev/null || echo "false")"

if [[ "$SANDBOX_STATUS" == "True" ]]; then
  echo "    Account is in SNS SMS sandbox."

  # Check if the phone number is already verified
  VERIFIED="$(aws sns list-sms-sandbox-phone-numbers \
    --region "$AWS_REGION" \
    --query "PhoneNumbers[?PhoneNumber=='${NOTIFY_PHONE}'].Status" \
    --output text 2>/dev/null || echo "")"

  if [[ "$VERIFIED" == "Verified" ]]; then
    echo "    ${NOTIFY_PHONE} is already verified in the sandbox."
  else
    echo "    Registering ${NOTIFY_PHONE} in the SNS sandbox..."
    aws sns create-sms-sandbox-phone-number \
      --phone-number "$NOTIFY_PHONE" \
      --region "$AWS_REGION" || true  # no-op if already pending

    echo ""
    echo "    A verification code has been sent to ${NOTIFY_PHONE}."
    read -rp "    Enter the OTP you received: " OTP
    aws sns verify-sms-sandbox-phone-number \
      --phone-number "$NOTIFY_PHONE" \
      --one-time-password "$OTP" \
      --region "$AWS_REGION"
    echo "    Phone number verified."
  fi
else
  echo "    Account is not in SNS sandbox — SMS can be sent to any number."
fi

echo ""
echo "✓ Deployment complete."
