"""Single CDK stack for the Polymarket momentum bot.

Resources:
    - ECR repository for the bot container image
    - ECS Fargate cluster + task definition
    - EventBridge Scheduler rule that runs the task on a cadence (default: 5 min)
    - DynamoDB tables: config, state, signals, orders  (all pay-per-request)
    - Secrets Manager secret placeholder for wallet creds (value set manually)
    - CloudWatch log group for the task
    - Lambda (Python) API handler + HTTP API Gateway
    - S3 static site + CloudFront distribution for the dashboard
    - IAM task role with least-privilege access

The GUI Lambda intentionally has NO access to Secrets Manager. Only the
ECS task role can read the wallet secret.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[3]
API_HANDLER_DIR = Path(__file__).resolve().parents[1] / "lambda" / "api"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "gui"


class BotStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bot_id = self.node.try_get_context("bot_id") or "default"
        schedule_expr = (
            self.node.try_get_context("schedule")
            or "rate(5 minutes)"
        )
        metrics_namespace = (
            self.node.try_get_context("metrics_namespace")
            or "PolymarketMomentumBot"
        )

        # --- DynamoDB tables --------------------------------------------------

        config_table = ddb.Table(
            self,
            "ConfigTable",
            partition_key=ddb.Attribute(name="bot_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        state_table = ddb.Table(
            self,
            "StateTable",
            partition_key=ddb.Attribute(name="bot_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        signals_table = ddb.Table(
            self,
            "SignalsTable",
            partition_key=ddb.Attribute(name="bot_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="ts", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        orders_table = ddb.Table(
            self,
            "OrdersTable",
            partition_key=ddb.Attribute(name="bot_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Secrets Manager placeholder -------------------------------------
        # The secret VALUE must be populated manually after deploy (see docs).
        # Here we only create an empty secret with a suggested JSON template so
        # no real key material ever ships in the template.
        wallet_secret = secretsmanager.Secret(
            self,
            "WalletSecret",
            description="Polymarket wallet credentials (PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE).",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {
                        "PRIVATE_KEY": "REPLACE_ME",
                        "FUNDER_ADDRESS": "REPLACE_ME",
                        "SIGNATURE_TYPE": "0",
                    }
                ),
                generate_string_key="_placeholder",
                exclude_characters='"\\/ ',
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- ECR + container image -------------------------------------------
        ecr_repo = ecr.Repository(
            self,
            "BotImageRepo",
            image_scan_on_push=True,
            lifecycle_rules=[
                ecr.LifecycleRule(max_image_count=10, description="Keep last 10 images.")
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Log group --------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "BotLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- VPC + ECS cluster -----------------------------------------------
        vpc = ec2.Vpc(
            self,
            "BotVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC
                )
            ],
        )
        cluster = ecs.Cluster(self, "BotCluster", vpc=vpc)

        # --- Task role (least privilege) -------------------------------------
        task_role = iam.Role(
            self,
            "BotTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Polymarket bot task role - reads wallet secret, writes DDB/logs/metrics.",
        )
        wallet_secret.grant_read(task_role)
        for table in (config_table, state_table, signals_table, orders_table):
            table.grant_read_write_data(task_role)
        log_group.grant_write(task_role)
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": metrics_namespace}
                },
            )
        )

        execution_role = iam.Role(
            self,
            "BotTaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # --- Task definition --------------------------------------------------
        task_def = ecs.FargateTaskDefinition(
            self,
            "BotTaskDef",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=execution_role,
        )
        container = task_def.add_container(
            "bot",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="bot", log_group=log_group
            ),
            environment={
                "DRY_RUN": "true",
                "BOT_ID": bot_id,
                "AWS_SECRET_ID": wallet_secret.secret_name,
                "CONFIG_TABLE": config_table.table_name,
                "STATE_TABLE": state_table.table_name,
                "SIGNALS_TABLE": signals_table.table_name,
                "ORDERS_TABLE": orders_table.table_name,
                "METRICS_NAMESPACE": metrics_namespace,
                "LOG_LEVEL": "INFO",
            },
        )

        # --- EventBridge scheduled one-shot task -----------------------------
        rule = events.Rule(
            self,
            "BotScheduleRule",
            schedule=events.Schedule.expression(schedule_expr),
            description="Run the momentum bot on a cadence (--once).",
        )
        rule.add_target(
            targets.EcsTask(
                cluster=cluster,
                task_definition=task_def,
                assign_public_ip=True,
                subnet_selection=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PUBLIC
                ),
                launch_type=ecs.LaunchType.FARGATE,
            )
        )

        # --- S3 + CloudFront static site -------------------------------------
        # Built before the API so the dashboard domain is known and can be
        # registered as an allowed Cognito callback/logout URL and as the
        # single CORS origin on the HTTP API.
        site_bucket = s3.Bucket(
            self,
            "GuiBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "GuiDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                )
            ],
        )
        dashboard_url = f"https://{distribution.distribution_domain_name}"

        # --- Cognito User Pool for dashboard login ---------------------------
        # Dashboard users are managed as Cognito users in the deploying
        # AWS account. This is NOT AWS IAM/root login - it is a separate
        # Cognito user directory intentionally distinct from the cloud
        # administrator identity.
        user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # App client for the browser SPA. No client secret (public client).
        # Callback/logout URLs point at the CloudFront distribution root.
        user_pool_client = user_pool.add_client(
            "DashboardClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=False),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[dashboard_url, f"{dashboard_url}/"],
                logout_urls=[dashboard_url, f"{dashboard_url}/"],
            ),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # Cognito Hosted UI domain. Prefix must be globally unique within
        # the region, so we derive it from the stack/account/region. Users
        # can override via CDK context "cognito_domain_prefix".
        domain_prefix = self.node.try_get_context("cognito_domain_prefix") or (
            f"polybot-{self.account}-{self.region}"
        )
        user_pool_domain = user_pool.add_domain(
            "DashboardUserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )
        hosted_ui_base = (
            f"https://{domain_prefix}.auth.{self.region}.amazoncognito.com"
        )
        hosted_ui_login_url = (
            f"{hosted_ui_base}/login"
            f"?client_id={user_pool_client.user_pool_client_id}"
            f"&response_type=code"
            f"&scope=openid+email+profile"
            f"&redirect_uri={dashboard_url}"
        )

        # --- API Lambda + HTTP API -------------------------------------------
        api_role = iam.Role(
            self,
            "ApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        for table in (config_table, state_table, signals_table, orders_table):
            table.grant_read_write_data(api_role)
        # Note: deliberately NO secretsmanager access for the API role.

        api_handler_code = lambda_.Code.from_asset(str(API_HANDLER_DIR))
        api_fn = lambda_.Function(
            self,
            "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=api_handler_code,
            role=api_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "BOT_ID": bot_id,
                "CONFIG_TABLE": config_table.table_name,
                "STATE_TABLE": state_table.table_name,
                "SIGNALS_TABLE": signals_table.table_name,
                "ORDERS_TABLE": orders_table.table_name,
            },
        )

        http_api = apigwv2.HttpApi(
            self,
            "BotHttpApi",
            description="Polymarket momentum bot GUI API (JWT-protected).",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=[
                    "authorization",
                    "content-type",
                    "x-amz-date",
                    "x-amz-security-token",
                    "x-api-key",
                ],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_origins=[dashboard_url],
                allow_credentials=False,
                max_age=Duration.hours(1),
            ),
        )

        # JWT authorizer validates Cognito-issued tokens on every request.
        jwt_authorizer = apigw_authorizers.HttpJwtAuthorizer(
            "CognitoJwtAuthorizer",
            jwt_issuer=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{user_pool.user_pool_id}"
            ),
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        integration = apigw_integrations.HttpLambdaIntegration(
            "ApiIntegration", api_fn
        )
        for path in ("/status", "/config", "/signals", "/orders", "/kill-switch"):
            http_api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod.ANY],
                integration=integration,
                authorizer=jwt_authorizer,
            )

        # Deploy static frontend and inject runtime config (API URL +
        # Cognito client details) via a generated config.js file.
        config_js = (
            "window.BOT_CONFIG = {\n"
            f"  apiUrl: '{http_api.api_endpoint}',\n"
            f"  region: '{self.region}',\n"
            f"  userPoolId: '{user_pool.user_pool_id}',\n"
            f"  userPoolClientId: '{user_pool_client.user_pool_client_id}',\n"
            f"  cognitoDomain: '{hosted_ui_base}',\n"
            f"  redirectUri: '{dashboard_url}',\n"
            f"  logoutUri: '{dashboard_url}'\n"
            "};\n"
            "window.BOT_API_URL = window.BOT_CONFIG.apiUrl;\n"
        )
        s3deploy.BucketDeployment(
            self,
            "GuiDeployment",
            sources=[
                s3deploy.Source.asset(str(FRONTEND_DIR)),
                s3deploy.Source.data("config.js", config_js),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # --- Outputs ----------------------------------------------------------
        CfnOutput(self, "EcrRepoUri", value=ecr_repo.repository_uri)
        CfnOutput(self, "EcrRepoName", value=ecr_repo.repository_name)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "TaskDefinitionArn", value=task_def.task_definition_arn)
        CfnOutput(self, "ConfigTableName", value=config_table.table_name)
        CfnOutput(self, "StateTableName", value=state_table.table_name)
        CfnOutput(self, "SignalsTableName", value=signals_table.table_name)
        CfnOutput(self, "OrdersTableName", value=orders_table.table_name)
        CfnOutput(self, "WalletSecretName", value=wallet_secret.secret_name)
        CfnOutput(self, "WalletSecretArn", value=wallet_secret.secret_arn)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "DashboardUrl", value=dashboard_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=hosted_ui_base)
        CfnOutput(self, "HostedUiLoginUrl", value=hosted_ui_login_url)
