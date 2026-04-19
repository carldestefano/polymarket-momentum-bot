"""DynamoDB-backed config overlay.

A single item, keyed by ``bot_id``, stores overrides for a subset of
``BotConfig`` fields. This lets the GUI flip ``dry_run`` / ``kill_switch``
and tune risk limits without redeploying the task.

Item shape (example)::

    {
      "bot_id": "default",
      "dry_run": True,
      "kill_switch": False,
      "max_trade_size_usdc": 5,
      "category_allowlist": ["politics", "sports"],
      "updated_at": "2026-04-19T12:34:56Z"
    }

Usage::

    overrides = fetch_config_overrides()
    overrides.apply_to(cfg)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config import BotConfig

log = logging.getLogger(__name__)


# Fields that are safe to override at runtime from DynamoDB.
OVERRIDABLE = {
    "dry_run": bool,
    "kill_switch": bool,
    "ma_window": int,
    "price_interval": str,
    "price_fidelity": int,
    "max_trade_size_usdc": float,
    "max_daily_spend_usdc": float,
    "max_open_positions": int,
    "min_volume_usdc": float,
    "min_liquidity_usdc": float,
    "max_spread": float,
    "trade_cooldown_sec": int,
    "category_allowlist": list,
    "market_allowlist": list,
    "poll_interval_sec": int,
    "max_markets": int,
    "log_level": str,
}


def _load_boto3() -> Optional[Any]:
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    return boto3


@dataclass
class ConfigOverrides:
    """Parsed overrides from DynamoDB."""

    values: Dict[str, Any] = field(default_factory=dict)

    def apply_to(self, cfg: BotConfig) -> BotConfig:
        for key, value in self.values.items():
            if key not in OVERRIDABLE:
                continue
            target_type = OVERRIDABLE[key]
            try:
                coerced = _coerce(value, target_type)
            except (TypeError, ValueError) as exc:
                log.warning(
                    "Ignoring override %s=%r: %s", key, value, exc
                )
                continue
            setattr(cfg, key, coerced)
        return cfg


def _coerce(value: Any, target: type) -> Any:
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is list:
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        raise ValueError(f"cannot coerce {type(value).__name__} to list")
    return str(value)


def fetch_config_overrides(
    table_name: Optional[str] = None,
    bot_id: Optional[str] = None,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> ConfigOverrides:
    """Load overrides from DynamoDB. Returns empty set on any failure."""
    table_name = table_name or os.getenv("CONFIG_TABLE")
    bot_id = bot_id or os.getenv("BOT_ID", "default")
    if not table_name:
        return ConfigOverrides()

    if client is None:
        boto3 = _load_boto3()
        if boto3 is None:
            log.debug("boto3 missing — skipping DynamoDB config overlay")
            return ConfigOverrides()
        kwargs: Dict[str, Any] = {}
        if region or os.getenv("AWS_REGION"):
            kwargs["region_name"] = region or os.getenv("AWS_REGION")
        client = boto3.resource("dynamodb", **kwargs).Table(table_name)

    try:
        resp = client.get_item(Key={"bot_id": bot_id})
    except Exception as exc:  # pragma: no cover - network path
        log.warning("Config table get_item failed: %s", exc)
        return ConfigOverrides()

    item = resp.get("Item") if isinstance(resp, dict) else None
    if not item:
        log.info("No config override row for bot_id=%s", bot_id)
        return ConfigOverrides()

    values = {k: v for k, v in item.items() if k != "bot_id"}
    # DynamoDB returns Decimals for numeric types — preserve raw values; _coerce
    # will convert them when applied.
    return ConfigOverrides(values=values)


def put_config_overrides(
    values: Dict[str, Any],
    table_name: Optional[str] = None,
    bot_id: Optional[str] = None,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> None:
    """Write a config overlay row. Intended for the GUI / admin path."""
    table_name = table_name or os.getenv("CONFIG_TABLE")
    bot_id = bot_id or os.getenv("BOT_ID", "default")
    if not table_name:
        raise RuntimeError("CONFIG_TABLE env var is not set")

    if client is None:
        boto3 = _load_boto3()
        if boto3 is None:
            raise RuntimeError("boto3 is not installed")
        kwargs: Dict[str, Any] = {}
        if region or os.getenv("AWS_REGION"):
            kwargs["region_name"] = region or os.getenv("AWS_REGION")
        client = boto3.resource("dynamodb", **kwargs).Table(table_name)

    item = {"bot_id": bot_id, "updated_at": _utc_iso()}
    for key, value in values.items():
        if key in OVERRIDABLE:
            item[key] = value
    client.put_item(Item=item)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
