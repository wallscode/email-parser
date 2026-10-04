#!/usr/bin/env bash
set -euo pipefail

# Email Parser — least-privilege IAM setup
#
# Run with ADMIN credentials (e.g. root via `aws login`), from the repo root:
#   source .env && bash scripts/setup-iam.sh
#
# Creates:
#   - email-parser-cloudformation  role CloudFormation runs as for routine deploys;
#                                  can only update the email-parser function's code/config
#   - EmailParserDeploy            policy: deploy EmailParserStack using the role above, nothing else
#   - email-parser-deployer        IAM user, console sign-in only (for `aws login`), no access keys;
#                                  EmailParserDeploy + testing and its own password/MFA
#   - email-parser-github-deploy   role for GitHub Actions (OIDC, no stored keys): EmailParserDeploy
#                                  only, and only from this repo's "production" environment
#
# Safe to re-run: policies get a new default version, an existing user/login is kept.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IAM_DIR="$REPO_ROOT/iam"

export SSM_API_KEY_PATH="${SSM_API_KEY_PATH:-/email-parser/claude-api-key}"
export SSM_PHONE_PATH="${SSM_PHONE_PATH:-/email-parser/notify-phone}"
export DEPLOYER_USER="${DEPLOYER_USER:-email-parser-deployer}"
export CFN_ROLE="email-parser-cloudformation"
export STACK_NAME="EmailParserStack"
export QUALIFIER="hnb659fds"   # CDK default bootstrap qualifier (asset bucket name)
CFN_POLICY="EmailParserCloudFormation"
DEPLOY_POLICY="EmailParserDeploy"
DEPLOYER_POLICY="EmailParserDeployer"
GITHUB_ROLE="email-parser-github-deploy"
# owner/repo, from the origin remote unless set explicitly
export GITHUB_REPO="${GITHUB_REPO:-$(git -C "$REPO_ROOT" remote get-url origin | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')}"

# ── Validate inputs ────────────────────────────────────────────────────────
# These values are written into IAM policies, so reject anything that could
# widen a policy (wildcards, quotes) rather than trusting the environment.
validate() {
  local name="$1" pattern="$2" value="${!1:-}"
  if [[ -z "$value" ]]; then
    echo "ERROR: required environment variable '$name' is not set. Run: source .env"
    exit 1
  fi
  if ! [[ "$value" =~ $pattern ]]; then
    echo "ERROR: '$name' has an unexpected format."
    exit 1
  fi
}
validate AWS_ACCOUNT_ID   '^[0-9]{12}$'
validate AWS_REGION       '^[a-z]{2}(-[a-z]+)+-[0-9]$'
validate S3_BUCKET_NAME   '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
validate DEPLOYER_USER    '^[A-Za-z0-9+=,.@_-]{1,64}$'
validate GITHUB_REPO      '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'

echo "==> Checking credentials..."
CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CALLER_ACCOUNT" != "$AWS_ACCOUNT_ID" ]]; then
  echo "ERROR: logged-in account does not match AWS_ACCOUNT_ID."
  exit 1
fi
export ACCOUNT_ID="$AWS_ACCOUNT_ID" REGION="$AWS_REGION" BUCKET="$S3_BUCKET_NAME"

# ── Helpers ────────────────────────────────────────────────────────────────

# Fill {{PLACEHOLDER}} values in a policy template from the environment,
# and confirm the result is valid JSON.
render() {
  python3 - "$1" <<'PY'
import json, os, re, sys
text = open(sys.argv[1]).read()
def sub(m):
    name = m.group(1)
    if name not in os.environ:
        sys.exit(f"ERROR: template variable {name} is not set")
    return os.environ[name]
out = re.sub(r"\{\{([A-Z_]+)\}\}", sub, text)
json.loads(out)
print(out)
PY
}

# Create a managed policy, or publish a new default version if it exists.
upsert_policy() {
  local name="$1" template="$2"
  local arn="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${name}"
  local doc
  doc="$(render "$template")"

  if aws iam get-policy --policy-arn "$arn" > /dev/null 2>&1; then
    # IAM keeps at most 5 versions; drop the oldest non-default one if full.
    local count
    count="$(aws iam list-policy-versions --policy-arn "$arn" --query 'length(Versions)' --output text)"
    if (( count >= 5 )); then
      local oldest
      oldest="$(aws iam list-policy-versions --policy-arn "$arn" \
        --query 'sort_by(Versions[?!IsDefaultVersion], &CreateDate)[0].VersionId' --output text)"
      aws iam delete-policy-version --policy-arn "$arn" --version-id "$oldest"
    fi
    aws iam create-policy-version --policy-arn "$arn" --policy-document "$doc" --set-as-default > /dev/null
    echo "    Updated policy $name"
  else
    aws iam create-policy --policy-name "$name" --policy-document "$doc" > /dev/null
    echo "    Created policy $name"
  fi
}

# ── Phase 1: CloudFormation role for routine deploys ───────────────────────
echo "==> CloudFormation role..."
upsert_policy "$CFN_POLICY" "$IAM_DIR/cfn-execution-policy.json"
if aws iam get-role --role-name "$CFN_ROLE" > /dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$CFN_ROLE" \
    --policy-document "file://$IAM_DIR/cfn-trust-policy.json"
  echo "    Role $CFN_ROLE already exists — trust policy refreshed."
else
  aws iam create-role --role-name "$CFN_ROLE" \
    --description "CloudFormation role for email-parser routine deploys" \
    --assume-role-policy-document "file://$IAM_DIR/cfn-trust-policy.json" > /dev/null
  echo "    Created role $CFN_ROLE"
fi
aws iam attach-role-policy --role-name "$CFN_ROLE" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${CFN_POLICY}"

# ── Phase 2: Shared deploy policy ──────────────────────────────────────────
echo "==> Deploy policy..."
upsert_policy "$DEPLOY_POLICY" "$IAM_DIR/deploy-policy.json"

# ── Phase 3: Deployer user (console sign-in only, no access keys) ──────────
echo "==> Deployer user..."
if aws iam get-user --user-name "$DEPLOYER_USER" > /dev/null 2>&1; then
  echo "    User $DEPLOYER_USER already exists."
else
  aws iam create-user --user-name "$DEPLOYER_USER" > /dev/null
  echo "    Created user $DEPLOYER_USER"
fi
# Attach the deploy policy before narrowing the user's own policy, so the user
# never loses deploy access partway through a re-run.
aws iam attach-user-policy --user-name "$DEPLOYER_USER" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${DEPLOY_POLICY}"
upsert_policy "$DEPLOYER_POLICY" "$IAM_DIR/deployer-policy.json"
aws iam attach-user-policy --user-name "$DEPLOYER_USER" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${DEPLOYER_POLICY}"

CREDS_FILE="$HOME/${DEPLOYER_USER}-initial-password.txt"
if aws iam get-login-profile --user-name "$DEPLOYER_USER" > /dev/null 2>&1; then
  echo "    Console login already exists — password unchanged."
else
  # The password goes only into files readable by you: never printed, and never
  # passed as a command-line argument (which other processes could see).
  ( umask 077
    REQUEST_FILE="$(mktemp)"
    trap 'rm -f "$REQUEST_FILE"' EXIT
    python3 - "$DEPLOYER_USER" "$REQUEST_FILE" "$CREDS_FILE" "$AWS_ACCOUNT_ID" <<'PY'
import json, secrets, string, sys
user, request_file, creds_file, account = sys.argv[1:]
pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#%^*-_=+"]
chars = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(20)]
secrets.SystemRandom().shuffle(chars)
password = "".join(chars)
json.dump({"UserName": user, "Password": password, "PasswordResetRequired": True}, open(request_file, "w"))
open(creds_file, "w").write(
    f"Sign-in URL: https://{account}.signin.aws.amazon.com/console\n"
    f"User name:   {user}\n"
    f"Password:    {password}\n\n"
    "You'll be asked to change this password on first sign-in. Delete this file afterwards.\n"
)
PY
    aws iam create-login-profile --cli-input-json "file://$REQUEST_FILE" > /dev/null
  )
  echo "    Initial password written to $CREDS_FILE (readable only by you)."
fi

# ── Phase 4: GitHub Actions deploy role (OIDC) ─────────────────────────────
# One GitHub OIDC provider exists per account and other projects may share it,
# so only create it when missing.
echo "==> GitHub Actions role..."
OIDC_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" > /dev/null 2>&1; then
  echo "    GitHub OIDC provider already exists (shared)."
else
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com > /dev/null
  echo "    Created GitHub OIDC provider."
fi
TRUST_DOC="$(render "$IAM_DIR/github-trust-policy.json")"
if aws iam get-role --role-name "$GITHUB_ROLE" > /dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$GITHUB_ROLE" --policy-document "$TRUST_DOC"
  echo "    Role $GITHUB_ROLE already exists — trust policy refreshed."
else
  aws iam create-role --role-name "$GITHUB_ROLE" --max-session-duration 3600 \
    --description "email-parser deploys from GitHub Actions (production environment only)" \
    --assume-role-policy-document "$TRUST_DOC" > /dev/null
  echo "    Created role $GITHUB_ROLE (trusts ${GITHUB_REPO}, environment: production)"
fi
aws iam attach-role-policy --role-name "$GITHUB_ROLE" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${DEPLOY_POLICY}"

echo ""
echo "✓ IAM setup complete."
echo "    CloudFormation role: arn:aws:iam::${AWS_ACCOUNT_ID}:role/${CFN_ROLE}"
echo "    Deployer user:       arn:aws:iam::${AWS_ACCOUNT_ID}:user/${DEPLOYER_USER}"
echo "    GitHub deploy role:  arn:aws:iam::${AWS_ACCOUNT_ID}:role/${GITHUB_ROLE}"
