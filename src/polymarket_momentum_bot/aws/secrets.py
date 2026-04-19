"""AWS Secrets Manager adapter.

Loads wallet credentials (PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE and
any extra CLOB SDK keys) from a single JSON secret and places them into
``os.environ`` so downstream ``BotConfig.from_env`` sees them.

Usage::

    from polymarket_momentum_bot.aws.secrets import hydrate_env_from_secret
    hydrate_env_from_secret()  # no-op if boto3 missing or AWS_SECRET_ID unset
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def _load_boto3() -> Optional[Any]:
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    return boto3


def fetch_secret(
    secret_id: str,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return the parsed JSON body of ``secret_id``.

    Raises ``RuntimeError`` if boto3 is missing or the secret is not JSON.
    ``client`` is accepted so tests can inject a fake.
    """
    if client is None:
        boto3 = _load_boto3()
        if boto3 is None:
            raise RuntimeError(
                "boto3 is not installed; `pip install boto3` to enable "
                "Secrets Manager integration."
            )
        kwargs: Dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        client = boto3.client("secretsmanager", **kwargs)

    resp = client.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString")
    if raw is None:
        # Binary secrets are not supported here.
        raise RuntimeError(f"Secret {secret_id} has no SecretString payload")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Secret {secret_id} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Secret {secret_id} JSON must be an object")
    return data


_ENV_KEYS = (
    "PRIVATE_KEY",
    "FUNDER_ADDRESS",
    "SIGNATURE_TYPE",
    "CHAIN_ID",
    "CLOB_HOST",
    "CLOB_API_KEY",
    "CLOB_API_SECRET",
    "CLOB_API_PASSPHRASE",
)


def hydrate_env_from_secret(
    secret_id: Optional[str] = None,
    region: Optional[str] = None,
    client: Optional[Any] = None,
    override: bool = False,
) -> bool:
    """Populate ``os.environ`` from a Secrets Manager JSON secret.

    Returns ``True`` if a secret was loaded, ``False`` otherwise (no-op).
    Keys already present in ``os.environ`` are kept unless ``override=True``.
    Unknown keys in the secret are also exported so bots can use custom vars.
    """
    secret_id = secret_id or os.getenv("AWS_SECRET_ID")
    if not secret_id:
        return False
    region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

    try:
        data = fetch_secret(secret_id, region=region, client=client)
    except Exception as exc:
        log.error("Failed to load secret %s: %s", secret_id, exc)
        raise

    count = 0
    for key, value in data.items():
        if value is None:
            continue
        str_value = str(value)
        if override or not os.environ.get(key):
            os.environ[key] = str_value
            count += 1
    log.info(
        "Loaded %d env vars from secret %s (known keys: %s)",
        count,
        secret_id,
        ", ".join(k for k in _ENV_KEYS if k in data),
    )
    return True
