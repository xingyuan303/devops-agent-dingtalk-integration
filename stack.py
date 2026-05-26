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
    aws_ecs as ecs,
    aws_ec2 as ec2,
    aws_logs as logs,
)
from constructs import Construct


class DevOpsAgentDingTalkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ── Context values ─────────────────────────────────────────────────────
        agent_space_id = self.node.try_get_context("agent_space_id") or ""
        dingtalk_chat_id = self.node.try_get_context("dingtalk_chat_id") or ""

        # ── Secrets Manager ────────────────────────────────────────────────────
        dingtalk_secret = secretsmanager.Secret(self, "DingTalkBotSecret",
            secret_name="devops-agent/dingtalk-bot",
            description="DingTalk credentials: DINGTALK_APP_KEY, DINGTALK_APP_SECRET, DEVOPS_AGENT_SPACE_ID",
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
                "DINGTALK_CHAT_ID": dingtalk_chat_id,
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
                "DINGTALK_CHAT_ID": dingtalk_chat_id,
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

        # ══════════════════════════════════════════════════════════════════════════
        # ── ECS Fargate: DingTalk Bot (Stream bidirectional chat) ──────────────
        # ══════════════════════════════════════════════════════════════════════════

        # VPC — use default VPC to keep it simple; override with context if needed
        vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        # ECS Cluster
        cluster = ecs.Cluster(self, "BotCluster",
            cluster_name="dingtalk-bot-cluster",
            vpc=vpc,
        )

        # Task Role — permissions for DevOps Agent API
        task_role = iam.Role(self, "BotTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "aidevops:CreateChat",
                "aidevops:SendMessage",
                "aidevops:ListChats",
            ],
            resources=["*"],
        ))
        dingtalk_secret.grant_read(task_role)

        # Task Definition
        task_def = ecs.FargateTaskDefinition(self, "BotTaskDef",
            memory_limit_mib=512,
            cpu=256,
            task_role=task_role,
        )

        # Container — build from dingtalk-bot/ directory
        container = task_def.add_container("dingtalk-bot",
            image=ecs.ContainerImage.from_asset("dingtalk-bot"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="dingtalk-bot",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            secrets={
                "DINGTALK_APP_KEY": ecs.Secret.from_secrets_manager(dingtalk_secret, "DINGTALK_APP_KEY"),
                "DINGTALK_APP_SECRET": ecs.Secret.from_secrets_manager(dingtalk_secret, "DINGTALK_APP_SECRET"),
            },
            environment={
                "DEVOPS_AGENT_SPACE_ID": agent_space_id,
                "AWS_REGION": self.region,
            },
        )

        # Fargate Service — 1 task, no LB needed (outbound WebSocket only)
        service = ecs.FargateService(self, "BotService",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=True,  # needed for outbound to DingTalk + AWS APIs
            service_name="dingtalk-bot",
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "SNSTopicArn",
            value=topic.topic_arn,
            description="Point your CloudWatch Alarms AlarmActions here")
        CfnOutput(self, "DingTalkNotifierName",
            value=dingtalk_notifier.function_name)
        CfnOutput(self, "InvestigationNotifierName",
            value=investigation_notifier.function_name)
        CfnOutput(self, "ECSClusterName",
            value=cluster.cluster_name)
        CfnOutput(self, "BotServiceName",
            value=service.service_name)
