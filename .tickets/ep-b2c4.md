---
id: ep-b2c4
status: open
deps: []
links: []
created: 2026-03-21T00:00:00Z
type: chore
priority: 0
parent: ep-a1f3
tags: [setup, config]
---
# Project scaffolding and configuration

Create the base project structure: `config.py`, `infrastructure/`, `lambda/`, `deploy.sh`, and dependency files. This is the prerequisite for all other tickets.

## Design

`config.py` reads all deployment-specific values from environment variables — no real values are ever hardcoded. The file committed to the repo must contain only `os.environ` calls with safe placeholder defaults. No secrets in code — API key and phone number go into SSM at deploy time.

Directory layout:
```
email-parser/
├── config.py
├── deploy.sh
├── infrastructure/
│   ├── app.py
│   ├── requirements.txt       # aws-cdk-lib>=2.100.0, constructs>=10.0.0
│   └── stacks/
│       └── __init__.py
└── lambda/
    ├── handler.py             # placeholder
    └── requirements.txt       # anthropic, boto3, pypdf, python-docx, openpyxl
```

## Acceptance Criteria

- `config.py` exists with all required fields documented
- `infrastructure/requirements.txt` and `lambda/requirements.txt` present with pinned versions
- `infrastructure/app.py` creates CDK app and imports the stack
- `deploy.sh` is executable and contains phase stubs with clear TODOs
- `pip install -r infrastructure/requirements.txt` succeeds
