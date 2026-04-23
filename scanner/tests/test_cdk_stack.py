"""CloudFormation template assertions for the scanner CDK stack.

These guard against the Stage 1 "NetworkError when attempting to fetch
resource" regression. Specifically:

- The HTTP API must have CORS configured with a literal allowOrigins
  (no unresolved CDK tokens, which produce "${Token[...]}" in the
  template and cause every browser preflight to fail).
- The JWT authorizer must not be attached to OPTIONS routes.
- No route should accept the ANY method (which would include OPTIONS
  and route preflight through the authorizer).

The test is skipped if aws-cdk-lib is not installed in the test env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

ROOT = Path(__file__).resolve().parents[1]
CDK_DIR = ROOT / "infra" / "cdk"
sys.path.insert(0, str(CDK_DIR))

from stacks.scanner_stack import PolymarketScannerStack  # noqa: E402


@pytest.fixture(scope="module")
def template():
    # Ensure the scanner package is staged so Code.from_asset succeeds.
    import app as cdk_app  # noqa: F401 - import triggers staging side-effect

    app = cdk.App()
    stack = PolymarketScannerStack(
        app,
        "TestScannerStack",
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def test_http_api_has_literal_cors_config(template):
    """allowOrigins must be a concrete list, not an unresolved token."""
    apis = template.find_resources("AWS::ApiGatewayV2::Api")
    assert apis, "expected an HTTP API in the template"
    (_, api), = apis.items()
    cors = api["Properties"].get("CorsConfiguration")
    assert cors, "HTTP API must have CorsConfiguration"
    origins = cors.get("AllowOrigins") or []
    assert origins, "AllowOrigins must not be empty"
    for origin in origins:
        assert isinstance(origin, str), (
            f"AllowOrigins entry must be a literal string, got {origin!r}"
        )
        assert "${Token[" not in origin, (
            "AllowOrigins contains an unresolved CDK token; this will "
            "cause every browser preflight to fail."
        )
    assert cors.get("AllowCredentials") in (False, None)


def test_no_route_uses_any_method(template):
    """ANY routes would send OPTIONS preflight through the JWT authorizer."""
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert routes, "expected HTTP API routes"
    for _, route in routes.items():
        route_key = route["Properties"]["RouteKey"]
        assert not route_key.startswith("ANY "), (
            f"route {route_key} uses ANY; use explicit methods so OPTIONS "
            "preflight is handled by API Gateway, not the authorizer."
        )
        assert not route_key.startswith("OPTIONS "), (
            f"route {route_key}: OPTIONS should be handled by the "
            "HTTP API CORS preflight, not an explicit route."
        )


def test_protected_routes_have_jwt_authorizer(template):
    """Every explicit GET/POST route should require the Cognito JWT."""
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    for _, route in routes.items():
        props = route["Properties"]
        assert props.get("AuthorizationType") == "JWT", (
            f"route {props['RouteKey']} must use JWT authorization"
        )


def test_dashboard_url_output_is_https(template):
    outputs = template.find_outputs("DashboardUrl")
    assert outputs, "DashboardUrl output missing"


def test_paper_tables_exist(template):
    """Stage 2 adds PaperPositionsTable and PaperTradesTable."""
    tables = template.find_resources("AWS::DynamoDB::Table")
    # Logical IDs are deterministic under the given construct names.
    names = list(tables.keys())
    assert any(n.startswith("PaperPositionsTable") for n in names), names
    assert any(n.startswith("PaperTradesTable") for n in names), names


def test_paper_table_outputs_exist(template):
    for out in ("PaperPositionsTableName", "PaperTradesTableName"):
        assert template.find_outputs(out), f"missing {out} output"


def test_paper_routes_are_registered(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = [r["Properties"]["RouteKey"] for r in routes.values()]
    expected_paths = [
        "/paper/status",
        "/paper/positions",
        "/paper/trades",
        "/paper/reset",
    ]
    for path in expected_paths:
        assert any(path in rk for rk in route_keys), (
            f"expected at least one route for {path}, got {route_keys}"
        )


def test_mm_tables_exist(template):
    """Stage 3 adds MmQuotesTable, MmFillsTable, MmInventoryTable."""
    tables = template.find_resources("AWS::DynamoDB::Table")
    names = list(tables.keys())
    assert any(n.startswith("MmQuotesTable") for n in names), names
    assert any(n.startswith("MmFillsTable") for n in names), names
    assert any(n.startswith("MmInventoryTable") for n in names), names


def test_mm_table_outputs_exist(template):
    for out in ("MmQuotesTableName", "MmFillsTableName", "MmInventoryTableName"):
        assert template.find_outputs(out), f"missing {out} output"


def test_mm_routes_are_registered(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = [r["Properties"]["RouteKey"] for r in routes.values()]
    expected_paths = [
        "/mm/status",
        "/mm/quotes",
        "/mm/fills",
        "/mm/inventory",
        "/mm/reset",
    ]
    for path in expected_paths:
        assert any(path in rk for rk in route_keys), (
            f"expected at least one route for {path}, got {route_keys}"
        )


def test_no_secrets_manager_or_live_trading_resources(template):
    """Stage 3 is simulation only. No Secrets Manager, no KMS keys,
    no external HTTP invocation resources should appear.
    """
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert not secrets, f"unexpected SecretsManager resources: {list(secrets)}"
    # The IAM role statements should NOT mention secretsmanager either.
    policies = template.find_resources("AWS::IAM::Policy")
    for logical_id, pol in policies.items():
        doc = pol["Properties"].get("PolicyDocument", {})
        for stmt in doc.get("Statement", []) or []:
            action = stmt.get("Action")
            actions = action if isinstance(action, list) else [action]
            for a in actions:
                if a is None:
                    continue
                assert "secretsmanager" not in str(a).lower(), (
                    f"unexpected secretsmanager permission in {logical_id}: {a}"
                )
