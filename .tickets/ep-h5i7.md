---
id: ep-h5i7
status: open
deps: [ep-d3e5, ep-f4g6]
links: []
created: 2026-03-21T00:00:00Z
type: feature
priority: 2
parent: ep-a1f3
tags: [deployment, shell]
---
# Deployment script (deploy.sh)

Implement `deploy.sh` to orchestrate the full deployment from a clean environment in a single command.

## Design

Phases:
1. **Validate config** — check that all required fields in `config.py` are non-default (exit with clear error if not)
2. **Install CDK deps** — `pip install -r infrastructure/requirements.txt`
3. **CDK bootstrap** — `cdk bootstrap aws://ACCOUNT_ID/REGION` (idempotent)
4. **CDK deploy** — `cdk deploy --require-approval never`
5. **Store secrets in SSM** — prompt for Claude API key and phone number (E.164), write to SSM via `aws ssm put-parameter --overwrite`
6. **SNS sandbox verification** — if account is in SNS sandbox, call `aws sns create-sms-sandbox-phone-number` and prompt user to enter the OTP

Script should be idempotent: re-running after a partial failure should pick up where it left off without duplicating resources.

## Acceptance Criteria

- Running `bash deploy.sh` on a correctly configured machine completes end-to-end without manual AWS console steps
- Config validation catches missing values before any AWS calls are made
- SSM parameters are written as `SecureString` type
- Script exits non-zero on any AWS CLI error
- Re-running the script does not create duplicate resources or error on already-existing SSM params
