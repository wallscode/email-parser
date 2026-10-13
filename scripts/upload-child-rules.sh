#!/usr/bin/env bash
set -euo pipefail

# Upload the per-child filtering rules to S3, where the Lambda reads them on every email.
# Run as email-parser-deployer after editing child-rules.json (gitignored; start from
# child-rules.example.json). Takes effect immediately — no deploy needed.
#
#   source .env && bash scripts/upload-child-rules.sh [path/to/child-rules.json]

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULES_FILE="${1:-$REPO_ROOT/child-rules.json}"
: "${S3_BUCKET_NAME:?set S3_BUCKET_NAME (source .env)}"
: "${AWS_REGION:?set AWS_REGION (source .env)}"

if [[ ! -f "$RULES_FILE" ]]; then
  echo "ERROR: $RULES_FILE not found. Copy child-rules.example.json to child-rules.json and edit it."
  exit 1
fi

# Same checks the Lambda makes, so a typo fails here instead of silently disabling filtering.
python3 -I - "$RULES_FILE" <<'EOF'
import json, sys
children = json.load(open(sys.argv[1])).get("children", [])
for child in children:
    if not isinstance(child.get("name"), str) or not child["name"].strip():
        sys.exit("ERROR: every child needs a \"name\"")
    for field in ("domains", "senders", "activities", "excluded_activities"):
        if not isinstance(child.get(field, []), list):
            sys.exit(f"ERROR: {child['name']}: \"{field}\" must be a list")
    year = child.get("graduation_year")
    if year is not None and (type(year) is not int or not 2000 <= year <= 2100):
        sys.exit(f"ERROR: {child['name']}: \"graduation_year\" must be a year, e.g. 2030")
print(f"    {len(children)} children: " + ", ".join(c["name"] for c in children))
EOF

aws s3 cp "$RULES_FILE" "s3://$S3_BUCKET_NAME/config/child-rules.json" \
  --region "$AWS_REGION" --content-type application/json --only-show-errors
echo "✓ Child rules uploaded. They apply to the next email processed."
