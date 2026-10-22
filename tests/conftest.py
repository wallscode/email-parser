import os
import sys

# handler.py reads these at import time. Placeholders only: the unit tests never call AWS
# or Claude, and set (not setdefault) so values from a sourced .env never leak into tests.
os.environ.update({
    "BUCKET_NAME": "test-bucket",
    "NOTIFY_EMAIL": "notify@example.com",
    "SENDER_EMAIL": "parser@example.com",
    "SSM_API_KEY_PATH": "/email-parser/test-api-key",
    "CLAUDE_MODEL": "test-model",
    "ALLOWED_SENDERS": "me@example.com, Partner@Example.org",
})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
