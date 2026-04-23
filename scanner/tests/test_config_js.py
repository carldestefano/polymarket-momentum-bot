"""Sanity checks for the generated dashboard config.js.

These run against the synthesized stack template rather than the raw
Python source, so we catch bugs where the injected apiUrl isn't what
the browser will actually receive.
"""

from __future__ import annotations

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
def synth_artifacts(tmp_path_factory):
    import app as cdk_app  # noqa: F401 - staging side-effect

    outdir = tmp_path_factory.mktemp("cdkout")
    app = cdk.App(outdir=str(outdir))
    PolymarketScannerStack(
        app,
        "TestScannerStack",
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    assembly = app.synth()
    return assembly


def _find_config_js(assembly) -> str | None:
    """Find the inline config.js payload written via Source.data()."""
    outdir = Path(assembly.directory)
    for path in outdir.rglob("*"):
        if path.is_file() and path.name == "config.js":
            text = path.read_text(encoding="utf-8")
            if "SCANNER_CONFIG" in text:
                return text
    return None


def test_config_js_is_generated(synth_artifacts):
    text = _find_config_js(synth_artifacts)
    assert text is not None, "config.js not found in synth assembly"


def test_config_js_strips_trailing_slashes(synth_artifacts):
    text = _find_config_js(synth_artifacts)
    assert text is not None
    # The normalization logic must be present so the browser never builds
    # "//status" style URLs.
    assert "replace(/\\/+$/, '')" in text or "replace(/\\/+$/,'')" in text


def test_config_js_has_https_api_url(synth_artifacts):
    text = _find_config_js(synth_artifacts)
    assert text is not None
    # The synthesized apiUrl should be an absolute HTTPS URL. It may
    # contain a CFN Fn::Join/Ref in the raw template, but the rendered
    # asset written to disk should have a concrete https:// prefix
    # resolved from http_api.api_endpoint.
    assert "apiUrl:" in text
    # Must not be empty and must not be http:// (mixed content).
    assert "apiUrl: ''" not in text
    assert "apiUrl: 'http://" not in text


def test_config_js_populates_cognito_fields(synth_artifacts):
    text = _find_config_js(synth_artifacts)
    assert text is not None
    for key in (
        "userPoolId:",
        "userPoolClientId:",
        "cognitoDomain:",
        "redirectUri:",
        "logoutUri:",
    ):
        assert key in text, f"config.js missing {key}"
