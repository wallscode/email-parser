#!/usr/bin/env bash
set -euo pipefail

# Tells whether a change can ship with a routine deploy (push to main), or needs an admin
# to run `deploy.sh --initial` first. Routine deploys may only change the resources the
# stack policy allows (the email-parser function); anything else fails and rolls back.
#
#   bash scripts/check-infra-change.sh [BASE_REF]     # default: origin/main
#
# Synthesizes the stack at BASE_REF and at HEAD with the same placeholder settings and
# compares the templates. Needs no AWS credentials or secrets, so it is safe to run on any
# pull request, including Dependabot's. Both sides use HEAD's pinned CDK CLI, which also
# checks that it supports HEAD's aws-cdk-lib. Exits 1 if an admin deploy is needed.

BASE_REF="${1:-origin/main}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$WORK/base" > /dev/null 2>&1 || true
  git -C "$REPO_ROOT" worktree remove --force "$WORK/head" > /dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

git -C "$REPO_ROOT" worktree add -q --detach "$WORK/base" "$BASE_REF"
git -C "$REPO_ROOT" worktree add -q --detach "$WORK/head" HEAD

# Placeholder settings, identical on both sides, so only code differences show up.
export AWS_ACCOUNT_ID=123456789012 AWS_REGION=us-east-1 DOMAIN=example.com
export PARSER_EMAIL_USER=parser NOTIFY_EMAIL=notify@example.com ALLOWED_SENDERS=sender@example.com
export S3_BUCKET_NAME=example-bucket HOSTED_ZONE_ID=Z0000000000EXAMPLE
export SSM_API_KEY_PATH=/email-parser/claude-api-key CLAUDE_MODEL=claude-sonnet-5-5
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1

echo "==> Installing the CDK CLI pinned at HEAD..."
(cd "$WORK/head/infrastructure" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error > /dev/null)
CDK="$WORK/head/infrastructure/node_modules/.bin/cdk"

synth() {  # synth <base|head>
  local src="$WORK/$1"
  echo "==> Synthesizing $1..."
  python3 -m venv "$WORK/$1-venv"
  "$WORK/$1-venv/bin/pip" install -q --disable-pip-version-check -r "$src/infrastructure/requirements.txt"
  # Routine deploys may change the function's code, so it isn't compared: both sides get
  # the same stub instead of a real Lambda build.
  mkdir -p "$src/dist/lambda"
  echo "# stub" > "$src/dist/lambda/handler.py"
  (cd "$src/infrastructure" && PATH="$WORK/$1-venv/bin:$PATH" \
    "$CDK" synth --quiet --output "$WORK/$1-out" > /dev/null)
}
synth base
synth head

echo "==> Comparing templates..."
python3 -I - "$WORK/base/iam/stack-policy.json" \
  "$WORK/base-out/EmailParserStack.template.json" \
  "$WORK/head-out/EmailParserStack.template.json" <<'EOF'
import json, os, sys

# The trusted (base) stack policy says which resources a routine deploy may modify.
policy = json.load(open(sys.argv[1]))
allowed = {
    resource.removeprefix("LogicalResourceId/")
    for statement in policy["Statement"]
    if statement["Effect"] == "Allow" and statement["Action"] == "Update:Modify"
    for resource in (
        [statement["Resource"]] if isinstance(statement["Resource"], str) else statement["Resource"]
    )
}
base, head = (json.load(open(path)) for path in sys.argv[2:4])

problems = []
for section in sorted(set(base) | set(head)):
    if section != "Resources":
        if base.get(section) != head.get(section):
            problems.append(f"template section {section} changes")
        continue
    for rid in sorted(set(base["Resources"]) | set(head["Resources"])):
        old, new = base["Resources"].get(rid), head["Resources"].get(rid)
        if old == new:
            continue
        kind = (new or old)["Type"]
        if old is None:
            problems.append(f"{rid} ({kind}) is added")
        elif new is None:
            problems.append(f"{rid} ({kind}) is removed")
        elif rid not in allowed:
            problems.append(f"{rid} ({kind}) changes")
        elif old["Type"] != new["Type"] or (
            old.get("Properties", {}).get("FunctionName") != new.get("Properties", {}).get("FunctionName")
        ):
            problems.append(f"{rid} ({kind}) would be replaced")

if problems:
    lines = ["Needs an admin deploy: routine deploys can't make these changes.", ""]
    lines += [f"- {p}" for p in problems]
    lines += ["", "Before merging, an admin runs `source .env && bash deploy.sh --initial` from this "
              "branch. The push to main then has nothing left to change."]
else:
    lines = ["Routine deploy OK: only the email-parser function changes (or nothing does)."]
print("\n".join(lines))
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("\n".join(lines) + "\n")
sys.exit(1 if problems else 0)
EOF
