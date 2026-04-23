"""PolymarketScannerStack - Stage 1 + Stage 2 scanner-only infrastructure.

Resources:
    - DynamoDB tables: config, scans, opportunities, paper_positions,
      paper_trades (pay-per-request)
    - Scanner Lambda (Python 3.12) scheduled every 5 minutes via EventBridge
    - API Lambda (Python 3.12) behind JWT-protected HTTP API Gateway
    - S3 + CloudFront static dashboard
    - Cognito User Pool + Hosted UI with Authorization Code + PKCE
    - CloudWatch log groups
    - Least-privilege IAM roles; NO wallet, NO secrets manager, NO trading

Stage 2 adds a paper-trading simulation: the scanner persists simulated
positions and fills to the two new tables after each scan, and the API
surfaces them under /paper/*. Paper trading is disabled by default and
must be enabled via ConfigTable (see docs/AWS_DEPLOYMENT.md).

Outputs:
    DashboardUrl, ApiUrl, UserPoolId, UserPoolClientId, CognitoDomain,
    HostedUiLoginUrl, ConfigTableName, ScansTableName, OpportunitiesTableName,
    PaperPositionsTableName, PaperTradesTableName, ScannerFunctionName,
    ScheduleRuleName.
"""

from __future__ import annotations

from pathlib import Path

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
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

HERE = Path(__file__).resolve().parent
SCANNER_LAMBDA_DIR = HERE.parent / "lambda" / "scanner"
API_LAMBDA_DIR = HERE.parent / "lambda" / "api"
FRONTEND_DIR = HERE.parents[1] / "gui"


class PolymarketScannerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        scanner_id = self.node.try_get_context("scanner_id") or "default"
        schedule_expr = (
            self.node.try_get_context("schedule") or "rate(5 minutes)"
        )

        # --- DynamoDB tables ------------------------------------------------
        config_table = ddb.Table(
            self,
            "ConfigTable",
            partition_key=ddb.Attribute(
                name="scanner_id", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Scans table: time-series summary of each scan run (one row per scan).
        scans_table = ddb.Table(
            self,
            "ScansTable",
            partition_key=ddb.Attribute(
                name="scanner_id", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(
                name="scanned_at", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # Opportunities table: ranked opportunities from the latest scan.
        # sk = "latest#meta" or "latest#<rank:04d>".
        opportunities_table = ddb.Table(
            self,
            "OpportunitiesTable",
            partition_key=ddb.Attribute(
                name="scanner_id", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Stage 2: paper trading tables ----------------------------------
        # Paper positions: one row per simulated position. sk encodes status
        # so OPEN positions sort ahead of CLOSED ones (OPEN# < CLOSED#).
        # Status is also duplicated into a top-level attribute so the API
        # can filter without parsing the sort key.
        paper_positions_table = ddb.Table(
            self,
            "PaperPositionsTable",
            partition_key=ddb.Attribute(
                name="scanner_id", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(
                name="position_sk", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Paper trades/fills: append-only ledger of simulated fills.
        # sk is the ISO timestamp of the fill, so a reverse query returns
        # the newest fills first. TTL is left unset by default so operators
        # can inspect a full history; add one later if ledger grows large.
        paper_trades_table = ddb.Table(
            self,
            "PaperTradesTable",
            partition_key=ddb.Attribute(
                name="scanner_id", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(
                name="trade_sk", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Log groups ------------------------------------------------------
        scanner_log_group = logs.LogGroup(
            self,
            "ScannerLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        api_log_group = logs.LogGroup(
            self,
            "ApiLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Scanner Lambda --------------------------------------------------
        scanner_role = iam.Role(
            self,
            "ScannerLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Scanner Lambda role - DDB write, no secrets, no trading.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        for table in (
            config_table,
            scans_table,
            opportunities_table,
            paper_positions_table,
            paper_trades_table,
        ):
            table.grant_read_write_data(scanner_role)

        scanner_fn = lambda_.Function(
            self,
            "ScannerFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(SCANNER_LAMBDA_DIR)),
            role=scanner_role,
            timeout=Duration.seconds(120),
            memory_size=512,
            log_group=scanner_log_group,
            environment={
                "SCANNER_ID": scanner_id,
                "CONFIG_TABLE": config_table.table_name,
                "SCANS_TABLE": scans_table.table_name,
                "OPPORTUNITIES_TABLE": opportunities_table.table_name,
                "PAPER_POSITIONS_TABLE": paper_positions_table.table_name,
                "PAPER_TRADES_TABLE": paper_trades_table.table_name,
                "MARKET_LIMIT": "500",
                "TOP_N": "50",
                "LOG_LEVEL": "INFO",
            },
        )

        schedule_rule = events.Rule(
            self,
            "ScannerScheduleRule",
            schedule=events.Schedule.expression(schedule_expr),
            description="Runs the Polymarket BTC scanner on a cadence.",
        )
        schedule_rule.add_target(targets.LambdaFunction(scanner_fn))

        # --- S3 + CloudFront static dashboard -------------------------------
        site_bucket = s3.Bucket(
            self,
            "DashboardBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "DashboardDistribution",
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

        # --- Cognito User Pool ----------------------------------------------
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
        domain_prefix = self.node.try_get_context("cognito_domain_prefix") or (
            f"polyscan-{self.account}-{self.region}"
        )
        user_pool.add_domain(
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

        # --- API Lambda + HTTP API ------------------------------------------
        api_role = iam.Role(
            self,
            "ApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Scanner API Lambda role - DDB read/write, invoke scanner.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        for table in (
            config_table,
            scans_table,
            opportunities_table,
            paper_positions_table,
            paper_trades_table,
        ):
            table.grant_read_write_data(api_role)
        scanner_fn.grant_invoke(api_role)
        # Note: deliberately NO secretsmanager access for the API role.

        api_fn = lambda_.Function(
            self,
            "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(API_LAMBDA_DIR)),
            role=api_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            log_group=api_log_group,
            environment={
                "SCANNER_ID": scanner_id,
                "CONFIG_TABLE": config_table.table_name,
                "SCANS_TABLE": scans_table.table_name,
                "OPPORTUNITIES_TABLE": opportunities_table.table_name,
                "PAPER_POSITIONS_TABLE": paper_positions_table.table_name,
                "PAPER_TRADES_TABLE": paper_trades_table.table_name,
                "SCANNER_FUNCTION_NAME": scanner_fn.function_name,
            },
        )

        # CORS: the CloudFront distribution domain is only known at deploy
        # time via a CFN token, and API Gateway HTTP API's allowOrigins does
        # not accept unresolved tokens — passing one produces a literal
        # "${Token[...]}" in the template and every browser preflight fails
        # with "NetworkError when attempting to fetch resource."
        #
        # Since we never send cookies (the dashboard uses bearer tokens in
        # the Authorization header and allow_credentials=False), "*" is the
        # pragmatic and safe choice: the CloudFront origin is unknown at
        # synth time, the API is JWT-protected on every route except the
        # CORS preflight, and no cross-origin credentials are exposed.
        http_api = apigwv2.HttpApi(
            self,
            "ScannerHttpApi",
            description="Polymarket BTC scanner dashboard API (JWT-protected).",
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
                allow_origins=["*"],
                allow_credentials=False,
                max_age=Duration.hours(1),
            ),
        )

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
        # Use explicit methods (GET/POST) rather than ANY so the JWT
        # authorizer never sees an OPTIONS preflight. HTTP API normally
        # handles OPTIONS at the gateway layer when cors_preflight is set,
        # but enumerating methods keeps the contract clear and prevents
        # future regressions where an authorizer could block preflight.
        api_methods = [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST]
        for path in (
            "/status",
            "/config",
            "/opportunities",
            "/scans",
            "/scan",
            "/paper/status",
            "/paper/positions",
            "/paper/trades",
            "/paper/reset",
        ):
            http_api.add_routes(
                path=path,
                methods=api_methods,
                integration=integration,
                authorizer=jwt_authorizer,
            )

        # --- Dashboard deployment with injected runtime config ---------------
        # Normalize apiUrl at runtime (strip trailing slashes) so that
        # fetch(API + "/status") never produces "//status". All URLs must
        # be absolute HTTPS — http_api.api_endpoint returns the fully
        # qualified "https://<id>.execute-api.<region>.amazonaws.com".
        config_js = (
            "(function(){\n"
            "  var cfg = {\n"
            f"    apiUrl: '{http_api.api_endpoint}',\n"
            f"    region: '{self.region}',\n"
            f"    userPoolId: '{user_pool.user_pool_id}',\n"
            f"    userPoolClientId: '{user_pool_client.user_pool_client_id}',\n"
            f"    cognitoDomain: '{hosted_ui_base}',\n"
            f"    redirectUri: '{dashboard_url}',\n"
            f"    logoutUri: '{dashboard_url}',\n"
            f"    scannerId: '{scanner_id}'\n"
            "  };\n"
            "  if (cfg.apiUrl) cfg.apiUrl = cfg.apiUrl.replace(/\\/+$/, '');\n"
            "  if (cfg.cognitoDomain) cfg.cognitoDomain = cfg.cognitoDomain.replace(/\\/+$/, '');\n"
            "  window.SCANNER_CONFIG = cfg;\n"
            "  window.SCANNER_API_URL = cfg.apiUrl;\n"
            "})();\n"
        )
        s3deploy.BucketDeployment(
            self,
            "DashboardDeployment",
            sources=[
                s3deploy.Source.asset(str(FRONTEND_DIR)),
                s3deploy.Source.data("config.js", config_js),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # --- Outputs --------------------------------------------------------
        CfnOutput(self, "DashboardUrl", value=dashboard_url)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=hosted_ui_base)
        CfnOutput(self, "HostedUiLoginUrl", value=hosted_ui_login_url)
        CfnOutput(self, "ConfigTableName", value=config_table.table_name)
        CfnOutput(self, "ScansTableName", value=scans_table.table_name)
        CfnOutput(self, "OpportunitiesTableName", value=opportunities_table.table_name)
        CfnOutput(
            self,
            "PaperPositionsTableName",
            value=paper_positions_table.table_name,
        )
        CfnOutput(
            self, "PaperTradesTableName", value=paper_trades_table.table_name
        )
        CfnOutput(self, "ScannerFunctionName", value=scanner_fn.function_name)
        CfnOutput(self, "ScheduleRuleName", value=schedule_rule.rule_name)
        CfnOutput(self, "ScannerLogGroupName", value=scanner_log_group.log_group_name)
        CfnOutput(self, "ApiLogGroupName", value=api_log_group.log_group_name)
