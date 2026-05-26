#!/usr/bin/env python3
import aws_cdk as cdk
from stack import DevOpsAgentDingTalkStack

app = cdk.App()

# If use_codebuild=true, enable CDK's CodeBuild-based Docker builds (no local Docker needed)
if app.node.try_get_context("use_codebuild"):
    app.node.set_context("@aws-cdk/aws-ecr-assets:buildWithCodeBuild", True)

DevOpsAgentDingTalkStack(app, "DevOpsAgentDingTalkStack")
app.synth()
