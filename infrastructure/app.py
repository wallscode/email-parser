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
    # Deploy with the caller's own credentials instead of CDK's account-wide bootstrap
    # roles. CloudFormation runs as the role passed via `cdk deploy --role-arn` (see deploy.sh).
    # Assets go under a project prefix so the deployer can only write there.
    synthesizer=cdk.CliCredentialsStackSynthesizer(bucket_prefix="email-parser/"),
    # Declared here so CDK never tries to change it: the deployer isn't allowed to.
    termination_protection=True,
)

app.synth()
