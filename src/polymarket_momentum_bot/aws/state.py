"""DynamoDB writers for bot heartbeat, signals, orders, and errors.

Each writer is a no-op when the relevant table env var is unset or when
boto3 is not installed, so the bot continues to run locally without AWS.

Table key schemas (set up by the CDK stack):

- ``STATE_TABLE``  : PK=bot_id (S), SK=sk (S). ``sk='heartbeat'`` for the
                     latest heartbeat; ``sk='position#<token_id>'`` per pos.
- ``SIGNALS_TABLE``: PK=bot_id (S), SK=ts (S ISO8601).
- ``ORDERS_TABLE`` : PK=bot_id (S), SK=ts#id (S).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def _load_boto3() -> Optional[Any]:
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    return boto3


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal (DynamoDB doesn't accept floats)."""
    if isinstance(value, float):
        # Route through str to avoid floating-point surprises.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


class StateWriter:
    """Best-effort DynamoDB writer. Silent no-op when not configured.

    Each table is looked up lazily via boto3 resource. All writes are wrapped
    in try/except so logging/monitoring failures never crash the main loop.
    """

    def __init__(
        self,
        bot_id: Optional[str] = None,
        region: Optional[str] = None,
        state_table: Optional[str] = None,
        signals_table: Optional[str] = None,
        orders_table: Optional[str] = None,
        boto3_module: Optional[Any] = None,
    ) -> None:
        self.bot_id = bot_id or os.getenv("BOT_ID", "default")
        self.region = (
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
        )
        self._names = {
            "state": state_table or os.getenv("STATE_TABLE"),
            "signals": signals_table or os.getenv("SIGNALS_TABLE"),
            "orders": orders_table or os.getenv("ORDERS_TABLE"),
        }
        self._boto3 = boto3_module if boto3_module is not None else _load_boto3()
        self._tables: Dict[str, Any] = {}
        self._resource = None

    # -- enablement ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._boto3 is not None and any(self._names.values())

    def _table(self, key: str) -> Optional[Any]:
        name = self._names.get(key)
        if not name or self._boto3 is None:
            return None
        if key in self._tables:
            return self._tables[key]
        if self._resource is None:
            kwargs: Dict[str, Any] = {}
            if self.region:
                kwargs["region_name"] = self.region
            self._resource = self._boto3.resource("dynamodb", **kwargs)
        table = self._resource.Table(name)
        self._tables[key] = table
        return table

    # -- writes -------------------------------------------------------------

    def heartbeat(self, status: str = "running", extra: Optional[Dict[str, Any]] = None) -> None:
        table = self._table("state")
        if table is None:
            return
        item = {
            "bot_id": self.bot_id,
            "sk": "heartbeat",
            "status": status,
            "updated_at": _utc_iso(),
        }
        if extra:
            item.update(_to_dynamo(extra))
        self._safe_put(table, item, "heartbeat")

    def record_position(self, token_id: str, size: float, avg_price: float) -> None:
        table = self._table("state")
        if table is None:
            return
        if size <= 0:
            self._safe_delete(
                table,
                key={"bot_id": self.bot_id, "sk": f"position#{token_id}"},
                context="position",
            )
            return
        item = {
            "bot_id": self.bot_id,
            "sk": f"position#{token_id}",
            "token_id": token_id,
            "size": _to_dynamo(size),
            "avg_price": _to_dynamo(avg_price),
            "updated_at": _utc_iso(),
        }
        self._safe_put(table, item, "position")

    def record_signal(
        self,
        token_id: str,
        question: str,
        signal: str,
        last_price: Optional[float],
        moving_average: Optional[float],
        reason: str,
    ) -> None:
        table = self._table("signals")
        if table is None:
            return
        item = {
            "bot_id": self.bot_id,
            "ts": _utc_iso(),
            "token_id": token_id,
            "question": (question or "")[:200],
            "signal": signal,
            "last_price": _to_dynamo(last_price) if last_price is not None else None,
            "moving_average": _to_dynamo(moving_average)
            if moving_average is not None
            else None,
            "reason": reason[:500],
        }
        self._safe_put(table, {k: v for k, v in item.items() if v is not None}, "signal")

    def record_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        dry_run: bool,
        ok: bool,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        table = self._table("orders")
        if table is None:
            return
        ts = _utc_iso()
        order_id = str(uuid.uuid4())
        item = {
            "bot_id": self.bot_id,
            "sk": f"{ts}#{order_id}",
            "ts": ts,
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "size": _to_dynamo(size),
            "price": _to_dynamo(price),
            "dry_run": bool(dry_run),
            "ok": bool(ok),
        }
        if response is not None:
            try:
                item["response"] = json.dumps(response)[:4000]
            except (TypeError, ValueError):
                item["response"] = str(response)[:4000]
        if error:
            item["error"] = error[:1000]
        self._safe_put(table, item, "order")

    def record_error(self, where: str, message: str) -> None:
        table = self._table("state")
        if table is None:
            return
        item = {
            "bot_id": self.bot_id,
            "sk": f"error#{_utc_iso()}",
            "where": where[:100],
            "message": message[:1500],
        }
        self._safe_put(table, item, "error")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_put(table: Any, item: Dict[str, Any], context: str) -> None:
        try:
            table.put_item(Item=item)
        except Exception as exc:  # pragma: no cover - network path
            log.warning("DynamoDB put (%s) failed: %s", context, exc)

    @staticmethod
    def _safe_delete(table: Any, key: Dict[str, Any], context: str) -> None:
        try:
            table.delete_item(Key=key)
        except Exception as exc:  # pragma: no cover - network path
            log.warning("DynamoDB delete (%s) failed: %s", context, exc)


class MetricsPublisher:
    """Tiny CloudWatch metrics helper. All methods are best-effort."""

    def __init__(
        self,
        namespace: Optional[str] = None,
        bot_id: Optional[str] = None,
        region: Optional[str] = None,
        boto3_module: Optional[Any] = None,
    ) -> None:
        self.namespace = namespace or os.getenv("METRICS_NAMESPACE")
        self.bot_id = bot_id or os.getenv("BOT_ID", "default")
        self.region = region or os.getenv("AWS_REGION")
        self._boto3 = boto3_module if boto3_module is not None else _load_boto3()
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.namespace is not None and self._boto3 is not None

    def _cw(self) -> Optional[Any]:
        if not self.enabled:
            return None
        if self._client is None:
            kwargs: Dict[str, Any] = {}
            if self.region:
                kwargs["region_name"] = self.region
            self._client = self._boto3.client("cloudwatch", **kwargs)
        return self._client

    def put(self, name: str, value: float, unit: str = "Count") -> None:
        client = self._cw()
        if client is None:
            return
        try:
            client.put_metric_data(
                Namespace=self.namespace,
                MetricData=[
                    {
                        "MetricName": name,
                        "Value": float(value),
                        "Unit": unit,
                        "Dimensions": [{"Name": "BotId", "Value": self.bot_id}],
                    }
                ],
            )
        except Exception as exc:  # pragma: no cover - network path
            log.debug("CloudWatch put_metric_data failed: %s", exc)
