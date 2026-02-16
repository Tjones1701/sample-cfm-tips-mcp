#!/usr/bin/env python3
"""CDK app entry point for the CFM Tips MCP Lambda deployment."""

import aws_cdk as cdk

from cdk_stack import CfmTipsMcpStack

app = cdk.App()
CfmTipsMcpStack(app, "CfmTipsMcpStack")
app.synth()
