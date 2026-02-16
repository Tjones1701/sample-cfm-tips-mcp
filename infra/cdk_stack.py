"""CDK stack for deploying the CFM Tips MCP server to AWS Lambda with API Gateway.

Cross-Account Invocation:
    When ``remote_account_id`` is provided via CDK context, the stack creates an
    ``AvatarIntegration-CFMTipsMCP`` IAM role in this account.  That role:
      - Trusts the remote account (with an auto-generated external ID)
      - Has ``execute-api:Invoke`` permission on the API Gateway ``/mcp`` route

    The remote account's principals assume this role, then sign API Gateway
    requests with SigV4.  No changes are needed in the remote account beyond
    configuring the assume-role call with the external ID output by this stack.
"""

import hashlib
import os

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    Fn,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from aws_cdk.aws_apigatewayv2_authorizers import HttpIamAuthorizer
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct


class CfmTipsMcpStack(Stack):
    """Stack that deploys the CFM Tips MCP server as a Lambda behind API Gateway."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Read configurable parameters from CDK context
        memory_size = self.node.try_get_context("lambda_memory_size") or 512
        timeout = self.node.try_get_context("lambda_timeout") or 60

        # --- IAM Execution Role (least-privilege, grouped by service) ---
        execution_role = iam.Role(
            self,
            "McpLambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for CFM Tips MCP Lambda function",
        )

        # Cost Analysis: Cost Explorer, Cost Optimization Hub, Compute Optimizer
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CostAnalysis",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ce:Get*",
                    "cost-optimization-hub:List*",
                    "cost-optimization-hub:Get*",
                    "compute-optimizer:Get*",
                    "compute-optimizer:Describe*",
                ],
                resources=["*"],
            )
        )

        # Monitoring: CloudWatch, Performance Insights, Trusted Advisor
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Monitoring",
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudwatch:Get*",
                    "cloudwatch:List*",
                    "cloudwatch:Describe*",
                    "pi:Get*",
                    "pi:Describe*",
                    "support:Describe*",
                    "support:Refresh*",
                ],
                resources=["*"],
            )
        )

        # Resources: EC2, RDS, Lambda, S3, CloudTrail (read-only)
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Resources",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:Describe*",
                    "rds:Describe*",
                    "rds:ListTagsForResource",
                    "lambda:List*",
                    "lambda:Get*",
                    "s3:List*",
                    "s3:Get*",
                    "cloudtrail:Describe*",
                    "cloudtrail:Get*",
                    "cloudtrail:LookupEvents",
                ],
                resources=["*"],
            )
        )

        # STS
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="STS",
                effect=iam.Effect.ALLOW,
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            )
        )

        # Pricing
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Pricing",
                effect=iam.Effect.ALLOW,
                actions=["pricing:GetProducts", "pricing:DescribeServices"],
                resources=["*"],
            )
        )

        # CloudWatch Logs (required for Lambda execution)
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Logs",
                effect=iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        # --- Lambda Function ---
        # Use Code.fromAsset with explicit exclude so the jsii copyDirectory
        # step skips heavy/recursive directories (sample-cfm-tips-mcp, infra,
        # cdk.out, .venv).  Docker bundling then installs pip dependencies.
        project_root = os.path.join(os.path.dirname(__file__), "..")

        asset_exclude = [
            ".venv",
            ".git",
            "__pycache__",
            ".pytest_cache",
            "tests",
            "sample-cfm-tips-mcp",
            "infra",
            "cdk.out",
            "cdk-outputs.json",
            "deploy.sh",
            "logs",
            ".DS_Store",
            "*.pyc",
            "*.pyo",
            ".gitignore",
            ".dockerignore",
            ".amazonq",
            "sessions",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "README.md",
            "RUNBOOKS_GUIDE.md",
            "setup.py",
            "diagnose_cost_optimization_hub_v2.py",
            "mcp_runbooks.json",
        ]

        lambda_fn = lambda_.Function(
            self,
            "CfmTipsMcpFunction",
            code=lambda_.Code.from_asset(
                project_root,
                exclude=asset_exclude,
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_13.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output"
                        " && tar -cf - "
                        "--exclude='./.venv' "
                        "--exclude='./.git' "
                        "--exclude='./__pycache__' "
                        "--exclude='./.pytest_cache' "
                        "--exclude='./tests' "
                        "--exclude='./sample-cfm-tips-mcp' "
                        "--exclude='./infra' "
                        "--exclude='./cdk.out' "
                        "--exclude='./logs' "
                        "--exclude='./.amazonq' "
                        "--exclude='./sessions' "
                        "--exclude='*.pyc' "
                        ". | tar -xf - -C /asset-output",
                    ],
                ),
            ),
            handler="lambda_handler.handler",
            runtime=lambda_.Runtime.PYTHON_3_13,
            memory_size=int(memory_size),
            timeout=Duration.seconds(int(timeout)),
            role=execution_role,
        )

        # --- API Gateway HTTP API with IAM auth ---
        # The HttpIamAuthorizer validates SigV4 signatures without restricting
        # to a specific AWS account.  Cross-account principals can invoke the
        # API as long as they have execute-api:Invoke permission on the resource.
        # To add an explicit cross-account resource policy later, use:
        #   http_api.add_routes(..., authorizer=HttpIamAuthorizer())
        #   and attach a CfnApi resource policy allowing the remote account.
        authorizer = HttpIamAuthorizer()
        integration = HttpLambdaIntegration("McpIntegration", lambda_fn)

        http_api = apigwv2.HttpApi(self, "McpHttpApi")

        http_api.add_routes(
            path="/mcp",
            methods=[
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=integration,
            authorizer=authorizer,
        )

        # --- Stack Outputs ---
        CfnOutput(self, "ApiEndpoint", value=http_api.url or "")
        CfnOutput(self, "LambdaFunctionArn", value=lambda_fn.function_arn)

        # --- Cross-Account AvatarIntegration Role (optional) ---
        remote_account_id = self.node.try_get_context("remote_account_id")
        if remote_account_id:
            # Generate a deterministic but unique external ID from the stack name
            # and remote account ID so it's stable across deploys.
            raw = f"{self.stack_name}:{remote_account_id}:avatar-integration"
            external_id = hashlib.sha256(raw.encode()).hexdigest()[:43]

            avatar_role = iam.Role(
                self,
                "AvatarIntegrationCFMTipsMCP",
                role_name="AvatarIntegration-CFMTipsMCP",
                assumed_by=iam.AccountPrincipal(remote_account_id),
                external_ids=[external_id],
                description=(
                    f"Allows account {remote_account_id} to invoke the "
                    "CFM Tips MCP API Gateway endpoint"
                ),
            )

            # Grant execute-api:Invoke on the /mcp route (all methods)
            avatar_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeMCPApiGateway",
                    effect=iam.Effect.ALLOW,
                    actions=["execute-api:Invoke"],
                    resources=[
                        Fn.join("", [
                            "arn:aws:execute-api:",
                            self.region,
                            ":",
                            self.account,
                            ":",
                            http_api.http_api_id,
                            "/*/*/mcp",
                        ]),
                    ],
                )
            )

            CfnOutput(self, "AvatarIntegrationRoleArn", value=avatar_role.role_arn)
            CfnOutput(self, "AvatarIntegrationExternalId", value=external_id)
