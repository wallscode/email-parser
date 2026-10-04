import os
import sys

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_ses as ses,
    aws_ses_actions as ses_actions,
)
from constructs import Construct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config

LAMBDA_BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "dist", "lambda")


class EmailParserStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── S3 Bucket ──────────────────────────────────────────────────────────
        # Import the existing bucket rather than creating a new one.
        # Lifecycle rules are managed separately via deploy.sh (AWS CLI).
        bucket = s3.Bucket.from_bucket_name(
            self,
            "EmailBucket",
            config.S3_BUCKET_NAME,
        )

        # SES needs permission to write inbound emails to the bucket.
        # from_bucket_name() returns an IBucket so we use CfnBucketPolicy directly.
        s3.CfnBucketPolicy(
            self,
            "EmailBucketPolicy",
            bucket=config.S3_BUCKET_NAME,
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowSESPut",
                        "Effect": "Allow",
                        "Principal": {"Service": "ses.amazonaws.com"},
                        "Action": "s3:PutObject",
                        "Resource": f"arn:aws:s3:::{config.S3_BUCKET_NAME}/raw-emails/*",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": self.account}
                        },
                    }
                ],
            },
        )

        # ── Lambda IAM Role ────────────────────────────────────────────────────
        lambda_role = iam.Role(
            self,
            "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[bucket.arn_for_objects("raw-emails/*")],
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("parsed-output/*")],
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],
                # Pin sender and recipient here (admin-managed) so that changing the
                # function's environment variables can't redirect summaries elsewhere.
                conditions={
                    "StringLike": {"ses:FromAddress": f"*@{config.DOMAIN}"},
                    "ForAllValues:StringEquals": {"ses:Recipients": [config.NOTIFY_EMAIL]},
                },
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:parameter{config.SSM_API_KEY_PATH}",
                ],
            )
        )
        # ── Lambda Function ────────────────────────────────────────────────────
        email_parser_fn = lambda_.Function(
            self,
            "EmailParserFunction",
            function_name="email-parser",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            role=lambda_role,
            timeout=Duration.seconds(300),
            memory_size=512,
            code=lambda_.Code.from_asset(os.path.realpath(LAMBDA_BUILD_DIR)),
            environment={
                "BUCKET_NAME": config.S3_BUCKET_NAME,
                "NOTIFY_EMAIL": config.NOTIFY_EMAIL,
                "SENDER_EMAIL": config.PARSER_EMAIL,
                "SSM_API_KEY_PATH": config.SSM_API_KEY_PATH,
                "CLAUDE_MODEL": config.CLAUDE_MODEL,
                "ALLOWED_SENDERS": config.ALLOWED_SENDERS,
            },
        )

        # ── S3 → Lambda trigger ────────────────────────────────────────────────
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(email_parser_fn),
            s3.NotificationKeyFilter(prefix="raw-emails/"),
        )

        # ── Route 53 ───────────────────────────────────────────────────────────
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=config.HOSTED_ZONE_ID,
            zone_name=config.DOMAIN,
        )

        # ── SES Domain Identity + DKIM ─────────────────────────────────────────
        # public_hosted_zone() creates the DKIM CNAME records in the zone for us.
        ses.EmailIdentity(
            self,
            "EmailIdentity",
            identity=ses.Identity.public_hosted_zone(hosted_zone),
        )

        # MX record pointing to SES inbound endpoint
        route53.MxRecord(
            self,
            "MxRecord",
            zone=hosted_zone,
            values=[
                route53.MxRecordValue(
                    host_name=f"inbound-smtp.{config.AWS_REGION}.amazonaws.com.",
                    priority=10,
                )
            ],
            ttl=Duration.seconds(300),
        )

        # ── SES Receipt Rule Set ───────────────────────────────────────────────
        rule_set = ses.ReceiptRuleSet(
            self,
            "ReceiptRuleSet",
            receipt_rule_set_name="email-parser-rules",
        )

        rule_set.add_rule(
            "StoreToS3",
            recipients=[config.PARSER_EMAIL],
            actions=[
                ses_actions.S3(
                    bucket=bucket,
                    object_key_prefix="raw-emails/",
                )
            ],
            scan_enabled=True,
            tls_policy=ses.TlsPolicy.REQUIRE,
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "BucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "LambdaFunctionName", value=email_parser_fn.function_name)
