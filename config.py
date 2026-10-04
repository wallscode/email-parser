import os

# All values are read from environment variables.
# Copy .env.example to .env, fill in your values, and source it before deploying locally.
# In CI/CD, these are injected from GitHub Actions secrets/variables.

DOMAIN = os.environ["DOMAIN"]
PARSER_EMAIL_USER = os.environ.get("PARSER_EMAIL_USER", "parser")
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.environ["AWS_ACCOUNT_ID"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
HOSTED_ZONE_ID = os.environ["HOSTED_ZONE_ID"]
SSM_API_KEY_PATH = os.environ.get("SSM_API_KEY_PATH", "/email-parser/claude-api-key")
SSM_PHONE_PATH = os.environ.get("SSM_PHONE_PATH", "/email-parser/notify-phone")

# Derived values
PARSER_EMAIL = f"{PARSER_EMAIL_USER}@{DOMAIN}"
