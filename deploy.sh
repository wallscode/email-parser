#!/usr/bin/env bash
set -euo pipefail

# Email Parser — deployment script
# Run from the repo root after sourcing your .env file.
#
#   Routine deploy (email-parser-deployer user):
#     source .env && bash deploy.sh
#   Updates only the Lambda function's code and configuration. Any change that touches
#   another resource is refused (by both IAM and the stack policy) and rolled back.
#
#   Initial / infrastructure deploy (admin credentials only):
#     source .env && bash deploy.sh --initial
#   Creates/configures the S3 bucket, deploys every resource, then does the one-time account
#   setup: receipt rule activation, SSM secrets, SES/SNS sandbox verification, SMS
#   subscription, and stack protection. Run scripts/setup-iam.sh first.

MODE="routine"
if [[ "${1:-}" == "--initial" ]]; then
  MODE="initial"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME="EmailParserStack"

# ── Phase 1: Validate config ───────────────────────────────────────────────
echo "==> Validating environment..."
required_vars=(
  AWS_ACCOUNT_ID AWS_REGION
  DOMAIN PARSER_EMAIL_USER NOTIFY_EMAIL
  S3_BUCKET_NAME HOSTED_ZONE_ID
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
DEPLOYER_CFN_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/email-parser-cloudformation"
ADMIN_CFN_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/cdk-hnb659fds-cfn-exec-role-${AWS_ACCOUNT_ID}-${AWS_REGION}"

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" > /dev/null 2>&1
}

# ── Phase 2: Install CDK dependencies ─────────────────────────────────────
echo "==> Installing CDK dependencies..."
pip install -q -r "$REPO_ROOT/infrastructure/requirements.txt"

# ── Phase 3: Build Lambda package ─────────────────────────────────────────
# Must happen before any CDK command since app.py references dist/lambda at synth time.
echo "==> Building Lambda package..."
LAMBDA_BUILD_DIR="$REPO_ROOT/dist/lambda"
rm -rf "$LAMBDA_BUILD_DIR"
mkdir -p "$LAMBDA_BUILD_DIR"
pip install -q -r "$REPO_ROOT/lambda/requirements.txt" -t "$LAMBDA_BUILD_DIR"
cp "$REPO_ROOT/lambda/"*.py "$LAMBDA_BUILD_DIR/"
echo "    Lambda package built at dist/lambda/"

# ── Routine deploy ────────────────────────────────────────────────────────
if [[ "$MODE" == "routine" ]]; then
  if ! stack_exists; then
    echo "ERROR: $STACK_NAME does not exist. An admin must run: bash deploy.sh --initial"
    exit 1
  fi
  echo "==> Deploying Lambda updates..."
  (cd "$REPO_ROOT/infrastructure" && \
    cdk deploy --require-approval never --role-arn "$DEPLOYER_CFN_ROLE_ARN")
  echo ""
  echo "✓ Deployment complete."
  exit 0
fi

# ── Initial deploy (admin) ────────────────────────────────────────────────

# Phase 4: CDK bootstrap — only the asset bucket is used, but CDK needs the bootstrap stack.
echo "==> Checking CDK bootstrap..."
if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$AWS_REGION" > /dev/null 2>&1; then
  echo "    CDK bootstrap found."
else
  (cd "$(mktemp -d)" && cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}")
fi

# Phase 5: S3 bucket. The stack only references the bucket (it predates the stack), so its
# configuration lives here. Creates it if missing, then enforces every setting — re-running
# resets any drift. Must run before the stack deploy, which attaches a policy and trigger to it.
echo "==> Configuring S3 bucket..."
if aws s3api head-bucket --bucket "$S3_BUCKET_NAME" > /dev/null 2>&1; then
  echo "    Bucket exists."
else
  BUCKET_NAMESPACE="global"
  if [[ "$S3_BUCKET_NAME" == *"-${AWS_ACCOUNT_ID}-${AWS_REGION}-an" ]]; then
    BUCKET_NAMESPACE="account-regional"
  fi
  create_args=(--bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" --bucket-namespace "$BUCKET_NAMESPACE")
  if [[ "$AWS_REGION" != "us-east-1" ]]; then
    create_args+=(--create-bucket-configuration "LocationConstraint=$AWS_REGION")
  fi
  aws s3api create-bucket "${create_args[@]}" > /dev/null
  echo "    Created bucket ($BUCKET_NAMESPACE namespace)."
fi

# No public access, ever.
aws s3api put-public-access-block --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# ACLs disabled — access is controlled only by IAM and the bucket policy.
aws s3api put-bucket-ownership-controls --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" \
  --ownership-controls '{"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}'

# SSE-S3 by default (SES's S3 action can't write to a bucket using SSE-KMS without extra
# key policy), with S3 Bucket Keys, and refuse client-supplied keys (SSE-C).
aws s3api put-bucket-encryption --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
      "BucketKeyEnabled": true,
      "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}
    }]
  }'

# Raw emails expire after 30 days; parsed output is kept.
aws s3api put-bucket-lifecycle-configuration --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "ExpireRawEmails",
      "Status": "Enabled",
      "Filter": {"Prefix": "raw-emails/"},
      "Expiration": {"Days": 30}
    }]
  }' > /dev/null
echo "    Public access blocked, ACLs disabled, SSE-S3 enforced, raw-emails/ expire after 30 days."

# Phase 6: Deploy the full stack. The stack policy normally only allows updates to the
# Lambda function, so lift it for this deploy and always put it back afterwards.
lock_stack() {
  aws cloudformation set-stack-policy --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --stack-policy-body "file://$REPO_ROOT/iam/stack-policy.json"
}
echo "==> Deploying full stack..."
if stack_exists; then
  aws cloudformation set-stack-policy --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --stack-policy-body "file://$REPO_ROOT/iam/stack-policy-admin-update.json"
fi
trap 'stack_exists && lock_stack' EXIT
(cd "$REPO_ROOT/infrastructure" && \
  cdk deploy --require-approval never --role-arn "$ADMIN_CFN_ROLE_ARN")
lock_stack
trap - EXIT

echo "    Stack policy set: routine deploys can only update the Lambda function."
echo "    (Termination protection is declared in infrastructure/app.py.)"

# Phase 7: Activate SES receipt rule set
echo "==> Activating SES receipt rule set..."
aws ses set-active-receipt-rule-set \
  --rule-set-name "email-parser-rules" \
  --region "$AWS_REGION"
echo "    Receipt rule set 'email-parser-rules' activated."

# Phase 8: Store secrets in SSM (prompts only for values that don't exist yet)
echo "==> Checking SSM parameters..."
ssm_param_exists() {
  aws ssm get-parameter --name "$1" --region "$AWS_REGION" > /dev/null 2>&1
}

if ssm_param_exists "$SSM_API_KEY_PATH"; then
  echo "    $SSM_API_KEY_PATH already exists — skipping."
else
  read -rsp "Enter your Claude API key: " CLAUDE_API_KEY
  echo
  aws ssm put-parameter --name "$SSM_API_KEY_PATH" --value "$CLAUDE_API_KEY" \
    --type SecureString --region "$AWS_REGION" --overwrite > /dev/null
  echo "    Claude API key stored."
fi

if ssm_param_exists "$SSM_PHONE_PATH"; then
  echo "    $SSM_PHONE_PATH already exists — skipping."
else
  read -rsp "Enter your notify phone number (E.164 format, e.g. +12125551234): " NEW_PHONE
  echo
  aws ssm put-parameter --name "$SSM_PHONE_PATH" --value "$NEW_PHONE" \
    --type SecureString --region "$AWS_REGION" --overwrite > /dev/null
  echo "    Notify phone stored."
fi
NOTIFY_PHONE="$(aws ssm get-parameter --name "$SSM_PHONE_PATH" --with-decryption \
  --region "$AWS_REGION" --query "Parameter.Value" --output text)"

# Phase 9: SES sandbox — summary emails can only go to verified addresses
echo "==> Checking SES sending status..."
SES_PRODUCTION="$(aws sesv2 get-account --region "$AWS_REGION" \
  --query "ProductionAccessEnabled" --output text)"
if [[ "$SES_PRODUCTION" == "True" ]]; then
  echo "    SES production access enabled — any recipient allowed."
elif [[ "$(aws sesv2 get-email-identity --email-identity "$NOTIFY_EMAIL" --region "$AWS_REGION" \
          --query VerifiedForSendingStatus --output text 2>/dev/null || echo missing)" == "True" ]]; then
  echo "    SES sandbox: notify address already verified."
else
  aws sesv2 create-email-identity --email-identity "$NOTIFY_EMAIL" --region "$AWS_REGION" \
    > /dev/null 2>&1 || true   # already pending
  echo "    SES sandbox: verification email sent to the notify address — click the link in it."
fi

# Phase 10: SNS SMS sandbox — the phone must be verified before it can receive SMS.
# Re-run with SMS_OTP=<code> to finish verification.
echo "==> Checking SNS SMS sandbox status..."
SANDBOX_STATUS="$(aws sns get-sms-sandbox-account-status --region "$AWS_REGION" \
  --query "IsInSandbox" --output text)"
if [[ "$SANDBOX_STATUS" == "True" ]]; then
  PHONE_STATUS="$(aws sns list-sms-sandbox-phone-numbers --region "$AWS_REGION" \
    --query "PhoneNumbers[?PhoneNumber=='${NOTIFY_PHONE}'].Status | [0]" --output text)"
  if [[ "$PHONE_STATUS" == "Verified" ]]; then
    echo "    Notify phone already verified."
  elif [[ -n "${SMS_OTP:-}" ]]; then
    aws sns verify-sms-sandbox-phone-number --phone-number "$NOTIFY_PHONE" \
      --one-time-password "$SMS_OTP" --region "$AWS_REGION"
    echo "    Notify phone verified."
  else
    if [[ "$PHONE_STATUS" == "Pending" ]]; then
      echo "    Phone verification is pending. If a code arrived, finish with:"
      echo "      SMS_OTP=<code> bash deploy.sh --initial"
      echo "    If none arrived, the account likely has no SMS origination number yet."
    elif aws sns create-sms-sandbox-phone-number \
        --phone-number "$NOTIFY_PHONE" --region "$AWS_REGION"; then
      echo "    A verification code was sent to the notify phone."
      echo "    Finish with: SMS_OTP=<code> bash deploy.sh --initial"
    else
      # Most often: no origination identity (e.g. a registered toll-free number) exists yet,
      # which AWS requires for sending SMS to US numbers.
      echo "    WARNING: could not start phone verification — SMS won't be delivered until this is fixed."
    fi
  fi
else
  echo "    Account is not in the SMS sandbox."
fi

# Phase 11: Subscribe the notify phone to the SMS topic
echo "==> Checking SMS topic subscription..."
TOPIC_ARN="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='SnsTopicArn'].OutputValue | [0]" --output text)"
SUBSCRIBED="$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" --region "$AWS_REGION" \
  --query "length(Subscriptions[?Protocol=='sms' && Endpoint=='${NOTIFY_PHONE}'])" --output text)"
if [[ "$SUBSCRIBED" != "0" ]]; then
  echo "    Notify phone already subscribed."
else
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol sms \
    --notification-endpoint "$NOTIFY_PHONE" --region "$AWS_REGION" > /dev/null
  echo "    Notify phone subscribed to the SMS topic."
fi

echo ""
echo "✓ Initial deployment complete."
