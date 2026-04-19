"""HTTP API handler for the Polymarket bot dashboard.

Routes (all JSON):

    GET  /status        -> { heartbeat, positions, tables }
    GET  /config        -> current config overrides
    POST /config        -> set config overrides (partial)
    GET  /signals       -> recent signals (?limit=50)
    GET  /orders        -> recent orders (?limit=50)
    POST /kill-switch   -> { "enabled": true|false }

Reads config/state/signals/orders from DynamoDB. Has NO secret access.
"""

from __future__ import annotations

import decimal
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

BOT_ID = os.environ.get("BOT_ID", "default")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE")
STATE_TABLE = os.environ.get("STATE_TABLE")
SIGNALS_TABLE = os.environ.get("SIGNALS_TABLE")
ORDERS_TABLE = os.environ.get("ORDERS_TABLE")

_ddb = boto3.resource("dynamodb")

OVERRIDABLE_KEYS = {
    "dry_run",
    "kill_switch",
    "ma_window",
    "price_interval",
    "price_fidelity",
    "max_trade_size_usdc",
    "max_daily_spend_usdc",
    "max_open_positions",
    "min_volume_usdc",
    "min_liquidity_usdc",
    "max_spread",
    "trade_cooldown_sec",
    "category_allowlist",
    "market_allowlist",
    "poll_interval_sec",
    "max_markets",
    "log_level",
}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            if o == o.to_integral_value():
                return int(o)
            return float(o)
        return super().default(o)


def _response(status: int, body: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _table(name: Optional[str]):
    if not name:
        return None
    return _ddb.Table(name)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_value(key: str, value: Any) -> Any:
    if key in {"dry_run", "kill_switch"}:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if key in {
        "ma_window",
        "price_fidelity",
        "max_open_positions",
        "trade_cooldown_sec",
        "poll_interval_sec",
        "max_markets",
    }:
        return int(value)
    if key in {
        "max_trade_size_usdc",
        "max_daily_spend_usdc",
        "min_volume_usdc",
        "min_liquidity_usdc",
        "max_spread",
    }:
        return decimal.Decimal(str(value))
    if key in {"category_allowlist", "market_allowlist"}:
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return []
    return str(value)


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# --- Route handlers ---------------------------------------------------------


def handle_status() -> Dict[str, Any]:
    state = _table(STATE_TABLE)
    heartbeat = None
    positions: List[Dict[str, Any]] = []
    recent_errors: List[Dict[str, Any]] = []
    if state is not None:
        try:
            resp = state.get_item(Key={"bot_id": BOT_ID, "sk": "heartbeat"})
            heartbeat = resp.get("Item")
        except Exception as exc:  # pragma: no cover
            heartbeat = {"error": str(exc)}
        try:
            q = state.query(
                KeyConditionExpression=Key("bot_id").eq(BOT_ID)
                & Key("sk").begins_with("position#"),
                Limit=50,
            )
            positions = q.get("Items", [])
        except Exception:  # pragma: no cover
            positions = []
        try:
            q = state.query(
                KeyConditionExpression=Key("bot_id").eq(BOT_ID)
                & Key("sk").begins_with("error#"),
                ScanIndexForward=False,
                Limit=10,
            )
            recent_errors = q.get("Items", [])
        except Exception:  # pragma: no cover
            recent_errors = []
    return _response(
        200,
        {
            "bot_id": BOT_ID,
            "heartbeat": heartbeat,
            "positions": positions,
            "recent_errors": recent_errors,
            "tables": {
                "config": CONFIG_TABLE,
                "state": STATE_TABLE,
                "signals": SIGNALS_TABLE,
                "orders": ORDERS_TABLE,
            },
        },
    )


def handle_get_config() -> Dict[str, Any]:
    cfg = _table(CONFIG_TABLE)
    if cfg is None:
        return _response(200, {})
    resp = cfg.get_item(Key={"bot_id": BOT_ID})
    item = resp.get("Item") or {"bot_id": BOT_ID}
    return _response(200, item)


def handle_put_config(event: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _table(CONFIG_TABLE)
    if cfg is None:
        return _response(500, {"error": "CONFIG_TABLE not configured"})
    body = _parse_body(event)
    updates: Dict[str, Any] = {}
    for key, value in body.items():
        if key in OVERRIDABLE_KEYS:
            try:
                updates[key] = _coerce_value(key, value)
            except Exception as exc:
                return _response(400, {"error": f"invalid value for {key}: {exc}"})
    if not updates:
        return _response(400, {"error": "no overridable fields supplied"})

    resp = cfg.get_item(Key={"bot_id": BOT_ID})
    item = resp.get("Item") or {"bot_id": BOT_ID}
    item.update(updates)
    item["updated_at"] = _utc_iso()
    cfg.put_item(Item=item)
    return _response(200, item)


def handle_kill_switch(event: Dict[str, Any]) -> Dict[str, Any]:
    body = _parse_body(event)
    enabled = bool(body.get("enabled"))
    cfg = _table(CONFIG_TABLE)
    if cfg is None:
        return _response(500, {"error": "CONFIG_TABLE not configured"})
    resp = cfg.get_item(Key={"bot_id": BOT_ID})
    item = resp.get("Item") or {"bot_id": BOT_ID}
    item["kill_switch"] = enabled
    item["updated_at"] = _utc_iso()
    cfg.put_item(Item=item)
    return _response(200, {"kill_switch": enabled, "updated_at": item["updated_at"]})


def handle_signals(event: Dict[str, Any]) -> Dict[str, Any]:
    table = _table(SIGNALS_TABLE)
    if table is None:
        return _response(200, {"items": []})
    limit = int((event.get("queryStringParameters") or {}).get("limit") or 50)
    limit = max(1, min(limit, 200))
    q = table.query(
        KeyConditionExpression=Key("bot_id").eq(BOT_ID),
        ScanIndexForward=False,
        Limit=limit,
    )
    return _response(200, {"items": q.get("Items", [])})


def handle_orders(event: Dict[str, Any]) -> Dict[str, Any]:
    table = _table(ORDERS_TABLE)
    if table is None:
        return _response(200, {"items": []})
    limit = int((event.get("queryStringParameters") or {}).get("limit") or 50)
    limit = max(1, min(limit, 200))
    q = table.query(
        KeyConditionExpression=Key("bot_id").eq(BOT_ID),
        ScanIndexForward=False,
        Limit=limit,
    )
    return _response(200, {"items": q.get("Items", [])})


# --- Dispatcher -------------------------------------------------------------


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", event.get("httpMethod", "GET"))
    ).upper()
    path = event.get("rawPath") or event.get("path") or "/"
    path = path.rstrip("/") or "/"

    try:
        if path == "/status" and method == "GET":
            return handle_status()
        if path == "/config":
            if method == "GET":
                return handle_get_config()
            if method in ("POST", "PUT"):
                return handle_put_config(event)
        if path == "/signals" and method == "GET":
            return handle_signals(event)
        if path == "/orders" and method == "GET":
            return handle_orders(event)
        if path == "/kill-switch" and method in ("POST", "PUT"):
            return handle_kill_switch(event)
        if method == "OPTIONS":
            return _response(204, {})
        return _response(404, {"error": "not found", "path": path, "method": method})
    except Exception as exc:  # pragma: no cover - defensive
        return _response(500, {"error": str(exc)})
