#!/usr/bin/env python3
import aws_cdk as cdk
from stack import DevOpsAgentDingTalkStack

app = cdk.App()
DevOpsAgentDingTalkStack(app, "DevOpsAgentDingTalkStack")
app.synth()
