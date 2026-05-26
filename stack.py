from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class DevOpsAgentDingTalkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ── Context values ─────────────────────────────────────────────────────
        agent_space_id = self.node.try_get_context("agent_space_id") or ""

        # ── Secrets Manager (create empty, fill via CLI after deploy) ──────────
        dingtalk_secret = secretsmanager.Secret(self, "DingTalkBotSecret",
            secret_name="devops-agent/dingtalk-bot",
            description="DingTalk Bot credentials: DINGTALK_WEBHOOK_URL, DINGTALK_SECRET",
        )

        webhook_sm = secretsmanager.Secret(self, "WebhookSecret",
            secret_name="devops-agent/webhook",
            description="Agent Space Webhook: WEBHOOK_URL, WEBHOOK_SECRET",
        )

        # ── SNS Topic ──────────────────────────────────────────────────────────
        topic = sns.Topic(self, "AlertsTopic",
            topic_name="devops-agent-alerts",
        )

        # ── Lambda Role ────────────────────────────────────────────────────────
        lambda_role = iam.Role(self, "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        dingtalk_secret.grant_read(lambda_role)
        webhook_sm.grant_read(lambda_role)
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=["aidevops:ListJournalRecords", "aidevops:GetBacklogTask"],
            resources=["*"],
        ))

        # ── Lambda: dingtalk-notifier ──────────────────────────────────────────
        dingtalk_notifier = _lambda.Function(self, "DingTalkNotifier",
            function_name="devops-agent-dingtalk-notifier",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="dingtalk_notifier.handler",
            code=_lambda.Code.from_asset("lambda/dingtalk_notifier"),
            timeout=Duration.seconds(30),
            role=lambda_role,
            environment={
                "DINGTALK_SECRET_NAME": dingtalk_secret.secret_name,
                "WEBHOOK_SECRET_NAME": webhook_sm.secret_name,
            },
        )
        topic.add_subscription(subs.LambdaSubscription(dingtalk_notifier))

        # ── Lambda: investigation-notifier ─────────────────────────────────────
        investigation_notifier = _lambda.Function(self, "InvestigationNotifier",
            function_name="devops-agent-investigation-notifier",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="investigation_notifier.handler",
            code=_lambda.Code.from_asset("lambda/investigation_notifier"),
            timeout=Duration.seconds(90),
            role=lambda_role,
            environment={
                "DINGTALK_SECRET_NAME": dingtalk_secret.secret_name,
                "DEVOPS_AGENT_SPACE_ID": agent_space_id,
            },
        )

        # ── EventBridge Rule ───────────────────────────────────────────────────
        rule = events.Rule(self, "DevOpsAgentEvents",
            rule_name="devops-agent-to-dingtalk",
            event_pattern=events.EventPattern(
                source=["aws.aidevops"],
                detail_type=[{"prefix": "Investigation"}, {"prefix": "Mitigation"}],
            ),
        )
        rule.add_target(targets.LambdaFunction(investigation_notifier))

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "SNSTopicArn",
            value=topic.topic_arn,
            description="Point your CloudWatch Alarms AlarmActions here")
        CfnOutput(self, "DingTalkNotifierName",
            value=dingtalk_notifier.function_name)
        CfnOutput(self, "InvestigationNotifierName",
            value=investigation_notifier.function_name)
