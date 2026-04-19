#!/usr/bin/env python3
"""CDK v2 entrypoint for the Polymarket momentum bot AWS deployment."""

from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.bot_stack import BotStack

app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1",
)

BotStack(
    app,
    "PolymarketMomentumBot",
    env=env,
    description="Polymarket CLOB momentum bot - ECS Fargate + DynamoDB + GUI.",
)

app.synth()
