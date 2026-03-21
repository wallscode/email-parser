import os
import subprocess
import sys

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_ses as ses,
    aws_ses_actions as ses_actions,
    aws_sns as sns,
)
from constructs import Construct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")


class _LocalBundling:
    """Bundles Lambda dependencies locally with pip — no Docker required.

    CDK tries this first; falls back to the Docker command only if it raises.
    All Lambda deps are pure Python so this works on any platform.
    """

    def __init__(self, source_dir: str) -> None:
        self._source_dir = os.path.realpath(source_dir)

    def try_bundle(self, output_dir: str, *_options) -> bool:
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "-r", os.path.join(self._source_dir, "requirements.txt"),
                    "-t", output_dir,
                    "--quiet",
                ],
                check=True,
            )
            # Copy source files into the output directory
            for fname in os.listdir(self._source_dir):
                src = os.path.join(self._source_dir, fname)
                dst = os.path.join(output_dir, fname)
                if os.path.isfile(src):
                    import shutil
                    shutil.copy2(src, dst)
            return True
        except Exception:
            return False


class EmailParserStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── S3 Bucket ──────────────────────────────────────────────────────────
        bucket = s3.Bucket(
            self,
            "EmailBucket",
            bucket_name=config.S3_BUCKET_NAME,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireRawEmails",
                    prefix="raw-emails/",
                    expiration=Duration.days(30),
                )
            ],
        )

        # SES needs permission to write inbound emails to the bucket
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSESPut",
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("raw-emails/*")],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account}
                },
            )
        )

        # ── SNS Topic for SMS ──────────────────────────────────────────────────
        sms_topic = sns.Topic(
            self,
            "SmsTopic",
            display_name="Email Parser SMS Notifications",
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
                conditions={
                    "StringLike": {"ses:FromAddress": f"*@{config.DOMAIN}"}
                },
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:parameter{config.SSM_API_KEY_PATH}",
                    f"arn:aws:ssm:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:parameter{config.SSM_PHONE_PATH}",
                ],
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[sms_topic.topic_arn],
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
            code=lambda_.Code.from_asset(
                os.path.realpath(LAMBDA_DIR),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=_LocalBundling(LAMBDA_DIR),
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output --quiet && cp -r . /asset-output",
                    ],
                ),
            ),
            environment={
                "BUCKET_NAME": config.S3_BUCKET_NAME,
                "NOTIFY_EMAIL": config.NOTIFY_EMAIL,
                "SENDER_EMAIL": config.PARSER_EMAIL,
                "SSM_API_KEY_PATH": config.SSM_API_KEY_PATH,
                "SSM_PHONE_PATH": config.SSM_PHONE_PATH,
                "CLAUDE_MODEL": config.CLAUDE_MODEL,
                "SNS_TOPIC_ARN": sms_topic.topic_arn,
            },
        )

        # ── S3 → Lambda trigger ────────────────────────────────────────────────
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(email_parser_fn),
            s3.NotificationKeyFilter(prefix="raw-emails/"),
        )

        # ── SES Domain Identity + DKIM ─────────────────────────────────────────
        email_identity = ses.EmailIdentity(
            self,
            "EmailIdentity",
            identity=ses.Identity.domain(config.DOMAIN),
        )

        # ── Route 53 ───────────────────────────────────────────────────────────
        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=config.DOMAIN,
        )

        # DKIM CNAME records
        for i, dkim_record in enumerate(email_identity.dkim_records):
            route53.CnameRecord(
                self,
                f"DkimRecord{i}",
                zone=hosted_zone,
                record_name=dkim_record.name,
                domain_name=dkim_record.value,
                ttl=Duration.seconds(1800),
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

        # Activate the receipt rule set
        cdk.CfnResource(
            self,
            "ActiveReceiptRuleSet",
            type="AWS::SES::ReceiptRuleSet",
            properties={"RuleSetName": rule_set.receipt_rule_set_name},
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "BucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "LambdaFunctionName", value=email_parser_fn.function_name)
        cdk.CfnOutput(self, "SnsTopicArn", value=sms_topic.topic_arn)
