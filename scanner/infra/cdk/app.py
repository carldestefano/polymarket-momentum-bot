#!/usr/bin/env python3
"""CDK v2 entrypoint for the Polymarket BTC scanner (Stage 1).

Before synth we stage the shared `polymarket_scanner` Python package into
the scanner Lambda asset directory so `Code.from_asset` picks it up
without any Docker bundling or external install step.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import aws_cdk as cdk

from stacks.scanner_stack import PolymarketScannerStack

HERE = Path(__file__).resolve().parent
SCANNER_PKG_SRC = HERE.parents[1] / "src" / "polymarket_scanner"
SCANNER_LAMBDA_DIR = HERE / "lambda" / "scanner"
STAGED_PKG = SCANNER_LAMBDA_DIR / "polymarket_scanner"


def _stage_scanner_package() -> None:
    if not SCANNER_PKG_SRC.is_dir():
        raise SystemExit(
            f"scanner package not found at {SCANNER_PKG_SRC}; "
            "make sure you are running `cdk synth` from scanner/infra/cdk/"
        )
    if STAGED_PKG.exists():
        shutil.rmtree(STAGED_PKG)
    shutil.copytree(SCANNER_PKG_SRC, STAGED_PKG)


_stage_scanner_package()

app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1",
)

PolymarketScannerStack(
    app,
    "PolymarketScannerStack",
    env=env,
    description="Polymarket BTC scanner Stage 1 + Stage 2 paper trading + Stage 3 market making - Lambda + DynamoDB + static dashboard.",
)

app.synth()
