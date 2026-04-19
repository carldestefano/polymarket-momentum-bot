"""Optional AWS runtime adapters.

These modules integrate the bot with AWS Secrets Manager (wallet creds),
DynamoDB (config + state/signals/orders), and CloudWatch (metrics).

All imports here are lazy: the bot still works offline, without ``boto3``
installed, and without any AWS env vars set. The CLI checks for environment
variables at startup and only activates these adapters when asked.

Environment variables that activate AWS adapters:

- ``AWS_SECRET_ID``  — Secrets Manager secret (JSON) with wallet credentials.
- ``CONFIG_TABLE``   — DynamoDB table for runtime config overrides.
- ``STATE_TABLE``    — DynamoDB table for heartbeat + position state.
- ``SIGNALS_TABLE``  — DynamoDB table for signal events.
- ``ORDERS_TABLE``   — DynamoDB table for simulated/live orders.
- ``METRICS_NAMESPACE`` — CloudWatch namespace for custom metrics (optional).
- ``BOT_ID``         — Logical bot identifier (default ``default``).
"""
