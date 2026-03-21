#!/usr/bin/env python3
import aws_cdk as cdk
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from stacks.email_parser_stack import EmailParserStack

app = cdk.App()

EmailParserStack(
    app,
    "EmailParserStack",
    env=cdk.Environment(account=config.AWS_ACCOUNT_ID, region=config.AWS_REGION),
)

app.synth()
