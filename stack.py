from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
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
    aws_sqs as sqs,
)
from constructs import Construct


class DevOpsAgentDingTalkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ── Context values ─────────────────────────────────────────────────────
        agent_space_id = self.node.try_get_context("agent_space_id") or ""
        dingtalk_chat_id = self.node.try_get_context("dingtalk_chat_id") or ""
        prefix = self.node.try_get_context("resource_prefix") or "devops-agent"
        existing_vpc_id = self.node.try_get_context("vpc_id") or ""
        # Whether to allow `cdk destroy` to remove secrets (false by default to protect creds)
        destroy_secrets = bool(self.node.try_get_context("destroy_secrets"))

        # ── Validation ─────────────────────────────────────────────────────────
        if not agent_space_id:
            raise ValueError(
                "Missing required context: agent_space_id. "
                "Set it in cdk.json or pass via -c agent_space_id=xxx"
            )

        secret_removal = RemovalPolicy.DESTROY if destroy_secrets else RemovalPolicy.RETAIN

        # ── Secrets Manager ────────────────────────────────────────────────────
        dingtalk_secret = secretsmanager.Secret(self, "DingTalkBotSecret",
            secret_name=f"{prefix}/dingtalk-bot",
            description="DingTalk credentials: DINGTALK_APP_KEY, DINGTALK_APP_SECRET",
            removal_policy=secret_removal,
        )

        webhook_sm = secretsmanager.Secret(self, "WebhookSecret",
            secret_name=f"{prefix}/webhook",
            description="Agent Space Webhook: WEBHOOK_URL, WEBHOOK_SECRET",
            removal_policy=secret_removal,
        )

        # ── Dead Letter Queue ──────────────────────────────────────────────────
        dlq = sqs.Queue(self, "DLQ",
            queue_name=f"{prefix}-dlq",
            retention_period=Duration.days(14),
        )

        # ── SNS Topic ──────────────────────────────────────────────────────────
        topic = sns.Topic(self, "AlertsTopic",
            topic_name=f"{prefix}-alerts",
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
        dlq.grant_send_messages(lambda_role)
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=["aidevops:ListJournalRecords", "aidevops:GetBacklogTask"],
            resources=["*"],
        ))

        # ── Lambda: dingtalk-notifier ──────────────────────────────────────────
        dingtalk_notifier = _lambda.Function(self, "DingTalkNotifier",
            function_name=f"{prefix}-dingtalk-notifier",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="dingtalk_notifier.handler",
            code=_lambda.Code.from_asset("lambda/dingtalk_notifier"),
            timeout=Duration.seconds(30),
            role=lambda_role,
            dead_letter_queue=dlq,
            environment={
                "DINGTALK_SECRET_NAME": dingtalk_secret.secret_name,
                "WEBHOOK_SECRET_NAME": webhook_sm.secret_name,
                "DINGTALK_CHAT_ID": dingtalk_chat_id,
            },
        )
        topic.add_subscription(subs.LambdaSubscription(dingtalk_notifier))

        # ── Lambda: investigation-notifier ─────────────────────────────────────
        investigation_notifier = _lambda.Function(self, "InvestigationNotifier",
            function_name=f"{prefix}-investigation-notifier",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="investigation_notifier.handler",
            code=_lambda.Code.from_asset("lambda/investigation_notifier"),
            timeout=Duration.seconds(90),
            role=lambda_role,
            dead_letter_queue=dlq,
            retry_attempts=2,
            environment={
                "DINGTALK_SECRET_NAME": dingtalk_secret.secret_name,
                "DEVOPS_AGENT_SPACE_ID": agent_space_id,
                "DINGTALK_CHAT_ID": dingtalk_chat_id,
            },
        )

        # ── EventBridge Rule (with DLQ) ────────────────────────────────────────
        rule = events.Rule(self, "DevOpsAgentEvents",
            rule_name=f"{prefix}-to-dingtalk",
            event_pattern=events.EventPattern(
                source=["aws.aidevops"],
                detail_type=[{"prefix": "Investigation"}, {"prefix": "Mitigation"}],
            ),
        )
        rule.add_target(targets.LambdaFunction(investigation_notifier,
            dead_letter_queue=dlq,
            retry_attempts=2,
        ))

        # ══════════════════════════════════════════════════════════════════════════
        # ── ECS Fargate: DingTalk Bot (Stream bidirectional chat) ──────────────
        # ══════════════════════════════════════════════════════════════════════════

        # VPC: use existing if provided, otherwise create new VPC with NAT Gateway
        # (avoids exposing public IP on the bot task)
        if existing_vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=existing_vpc_id)
        else:
            vpc = ec2.Vpc(self, "Vpc",
                max_azs=2,
                nat_gateways=1,
            )

        cluster = ecs.Cluster(self, "BotCluster",
            cluster_name=f"{prefix}-bot-cluster",
            vpc=vpc,
        )

        # Task Role
        task_role = iam.Role(self, "BotTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["aidevops:CreateChat", "aidevops:SendMessage", "aidevops:ListChats"],
            resources=["*"],
        ))

        # Task Definition
        task_def = ecs.FargateTaskDefinition(self, "BotTaskDef",
            memory_limit_mib=512,
            cpu=256,
            task_role=task_role,
        )
        # Grant secrets to execution role (needed for ECS secret injection)
        dingtalk_secret.grant_read(task_def.execution_role)

        task_def.add_container("dingtalk-bot",
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
            # Health check: verify main process is running (procps installed in Dockerfile)
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "pgrep -f 'python app.py' || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(15),
            ),
        )

        # Fargate Service — single instance only
        # (DingTalk Stream protocol delivers each message at-least-once to ALL subscribers,
        #  running multiple replicas would cause duplicate processing)
        ecs.FargateService(self, "BotService",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=False,  # bot uses NAT Gateway for outbound
            service_name=f"{prefix}-bot",
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "SNSTopicArn", value=topic.topic_arn,
            description="Point your CloudWatch Alarms AlarmActions here")
        CfnOutput(self, "DLQUrl", value=dlq.queue_url,
            description="Dead letter queue for failed notifications")
        CfnOutput(self, "DingTalkNotifierName", value=dingtalk_notifier.function_name)
        CfnOutput(self, "InvestigationNotifierName", value=investigation_notifier.function_name)
