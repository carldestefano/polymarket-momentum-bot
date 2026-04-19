"""Verify the CDK stack creates Cognito + JWT authorizer for the dashboard.

Uses CDK's built-in assertions against a synthed template. Skipped if
``aws_cdk`` is not installed (e.g. when running tests without the CDK
Python deps).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CDK_DIR = Path(__file__).resolve().parents[1] / "infra" / "cdk"


@pytest.fixture(scope="module")
def template():
    try:
        import aws_cdk as cdk
        from aws_cdk import assertions
    except ImportError:
        pytest.skip("aws_cdk not installed")

    # Make the CDK stack package importable.
    if str(CDK_DIR) not in sys.path:
        sys.path.insert(0, str(CDK_DIR))

    from stacks.bot_stack import BotStack  # type: ignore

    app = cdk.App()
    stack = BotStack(app, "PolymarketMomentumBotTest")
    return assertions.Template.from_stack(stack)


def test_user_pool_created(template):
    template.resource_count_is("AWS::Cognito::UserPool", 1)


def test_user_pool_client_has_no_secret(template):
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"GenerateSecret": False},
    )


def test_user_pool_domain_created(template):
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 1)


def test_jwt_authorizer_tied_to_cognito(template):
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {"AuthorizerType": "JWT", "IdentitySource": ["$request.header.Authorization"]},
    )


def test_protected_routes_require_authorizer(template):
    # Every route for the dashboard API must have AuthorizationType JWT.
    routes = template.find_resources(
        "AWS::ApiGatewayV2::Route",
        {"Properties": {"AuthorizationType": "JWT"}},
    )
    # One route per method binding for /status /config /signals /orders /kill-switch.
    assert len(routes) >= 5, f"expected >=5 JWT-protected routes, got {len(routes)}"


def test_outputs_include_cognito_values(template):
    outputs = template.find_outputs("UserPoolId")
    assert outputs, "missing UserPoolId output"
    outputs = template.find_outputs("UserPoolClientId")
    assert outputs, "missing UserPoolClientId output"
    outputs = template.find_outputs("HostedUiLoginUrl")
    assert outputs, "missing HostedUiLoginUrl output"
    outputs = template.find_outputs("DashboardUrl")
    assert outputs, "missing DashboardUrl output"
    outputs = template.find_outputs("ApiUrl")
    assert outputs, "missing ApiUrl output"
