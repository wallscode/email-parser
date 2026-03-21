import aws_cdk as cdk
from constructs import Construct


class EmailParserStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # TODO (ep-d3e5): implement full CDK stack
        # Resources to provision:
        #   - S3 bucket (raw-emails/ + parsed-output/ prefixes, 30-day lifecycle on raw-emails/)
        #   - SES domain identity + receipt rule → S3
        #   - Route 53 DKIM CNAMEs + MX record
        #   - Lambda function (Python 3.12, lambda/handler.py)
        #   - SSM parameters (placeholders; values set by deploy.sh)
        #   - SNS topic for SMS notifications
        #   - IAM execution role for Lambda (least-privilege)
        #   - S3 event notification → Lambda trigger on raw-emails/
