"""HTTP API handler for the Polymarket BTC scanner dashboard.

Routes (all JSON, JWT-protected by API Gateway):

    GET  /status            -> { scanner_id, latest scan summary, tables }
    GET  /config            -> current scanner + paper trading config
    POST /config            -> set scanner/paper config overrides (partial)
    GET  /opportunities     -> latest ranked BTC opportunities (?limit=50)
    GET  /scans             -> recent scan summaries (?limit=20)
    POST /scan              -> manually invoke the scanner Lambda
    GET  /paper/status      -> paper trading summary + effective config
    GET  /paper/positions   -> paper positions (?status=open|closed|all)
    GET  /paper/trades      -> recent simulated fills (?limit=100)
    POST /paper/reset       -> wipe all paper positions + fills (destructive)

This handler has NO wallet credentials, NO trading permissions, and NO
Secrets Manager access. It only reads/writes DynamoDB tables owned by
the scanner stack and (optionally) invokes the scanner Lambda.
"""

from __future__ import annotations

import decimal
import json
import logging
import os
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger(__name__)
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SCANNER_ID = os.environ.get("SCANNER_ID", "default")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE")
SCANS_TABLE = os.environ.get("SCANS_TABLE")
OPPORTUNITIES_TABLE = os.environ.get("OPPORTUNITIES_TABLE")
PAPER_POSITIONS_TABLE = os.environ.get("PAPER_POSITIONS_TABLE")
PAPER_TRADES_TABLE = os.environ.get("PAPER_TRADES_TABLE")
SCANNER_FUNCTION_NAME = os.environ.get("SCANNER_FUNCTION_NAME")

_ddb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")

# Keys accepted via POST /config. Scanner keys + paper trading keys.
SCANNER_OVERRIDABLE_KEYS = {
    "market_limit",
    "top_n",
    "short_horizon_only",
    "log_level",
}
PAPER_OVERRIDABLE_KEYS = {
    "paper_trading_enabled",
    "max_paper_trade_usdc",
    "max_paper_position_usdc_per_market",
    "max_total_paper_exposure_usdc",
    "min_edge_to_trade",
    "min_liquidity_usdc",
    "max_spread",
    "max_resolution_days",
    "cooldown_seconds",
    "allow_short_horizon_only",
    "close_on_edge_flip",
    "slippage_buffer",
}
OVERRIDABLE_KEYS = SCANNER_OVERRIDABLE_KEYS | PAPER_OVERRIDABLE_KEYS


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


def _route(event: Dict[str, Any]) -> tuple[str, str]:
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = (
        event.get("rawPath")
        or event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or "/"
    )
    return method, path


def _query(event: Dict[str, Any]) -> Dict[str, str]:
    return event.get("queryStringParameters") or {}


def _body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _get_status() -> Dict[str, Any]:
    table = _table(OPPORTUNITIES_TABLE)
    latest_meta = None
    if table is not None:
        try:
            resp = table.get_item(
                Key={"scanner_id": SCANNER_ID, "sk": "latest#meta"}
            )
            latest_meta = resp.get("Item")
        except Exception as e:  # pragma: no cover - defensive
            log.warning("status get_item failed: %s", e)
    return {
        "scanner_id": SCANNER_ID,
        "latest": latest_meta,
        "tables": {
            "config": CONFIG_TABLE,
            "scans": SCANS_TABLE,
            "opportunities": OPPORTUNITIES_TABLE,
        },
        "scanner_function": SCANNER_FUNCTION_NAME,
    }


def _get_config() -> Dict[str, Any]:
    table = _table(CONFIG_TABLE)
    if table is None:
        return {}
    resp = table.get_item(Key={"scanner_id": SCANNER_ID})
    return resp.get("Item") or {"scanner_id": SCANNER_ID}


def _to_ddb_value(v: Any) -> Any:
    """DynamoDB rejects floats; convert to Decimal. Recurse into containers."""
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        return decimal.Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb_value(x) for x in v]
    return v


def _put_config(partial: Dict[str, Any]) -> Dict[str, Any]:
    table = _table(CONFIG_TABLE)
    if table is None:
        return {"error": "config table not configured"}
    current = _get_config() or {"scanner_id": SCANNER_ID}
    for k, v in partial.items():
        if k in OVERRIDABLE_KEYS:
            current[k] = _to_ddb_value(v)
    current["scanner_id"] = SCANNER_ID
    table.put_item(Item=current)
    return current


def _get_opportunities(limit: int) -> Dict[str, Any]:
    table = _table(OPPORTUNITIES_TABLE)
    if table is None:
        return {"items": [], "count": 0}
    resp = table.query(
        KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID)
        & Key("sk").begins_with("latest#"),
        Limit=max(1, min(limit, 200)) + 1,
    )
    items = resp.get("Items") or []
    meta = next((i for i in items if i.get("sk") == "latest#meta"), None)
    opps = [i for i in items if i.get("sk", "").startswith("latest#") and i.get("sk") != "latest#meta"]
    opps.sort(key=lambda i: i.get("sk", ""))
    return {
        "scanner_id": SCANNER_ID,
        "scanned_at": (meta or {}).get("scanned_at"),
        "btc_price_usd": (meta or {}).get("btc_price_usd"),
        "count": len(opps),
        "items": opps[:limit],
    }


def _get_scans(limit: int) -> Dict[str, Any]:
    table = _table(SCANS_TABLE)
    if table is None:
        return {"items": [], "count": 0}
    resp = table.query(
        KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
        ScanIndexForward=False,
        Limit=max(1, min(limit, 100)),
    )
    items = resp.get("Items") or []
    return {"count": len(items), "items": items}


_PAPER_DEFAULTS = {
    "paper_trading_enabled": False,
    "max_paper_trade_usdc": 100.0,
    "max_paper_position_usdc_per_market": 250.0,
    "max_total_paper_exposure_usdc": 2000.0,
    "min_edge_to_trade": 0.05,
    "min_liquidity_usdc": 500.0,
    "max_spread": 0.10,
    "max_resolution_days": 30,
    "cooldown_seconds": 900,
    "allow_short_horizon_only": False,
    "close_on_edge_flip": True,
    "slippage_buffer": 0.01,
}


def _effective_paper_config() -> Dict[str, Any]:
    cfg = _get_config() or {}
    merged = dict(_PAPER_DEFAULTS)
    for k in PAPER_OVERRIDABLE_KEYS:
        if k in cfg and cfg[k] is not None:
            merged[k] = cfg[k]
    return merged


def _get_paper_status() -> Dict[str, Any]:
    table = _table(OPPORTUNITIES_TABLE)
    latest = None
    if table is not None:
        try:
            resp = table.get_item(
                Key={"scanner_id": SCANNER_ID, "sk": "paper#latest"}
            )
            latest = resp.get("Item")
        except Exception as e:  # pragma: no cover - defensive
            log.warning("paper status get_item failed: %s", e)
    return {
        "scanner_id": SCANNER_ID,
        "summary": latest,
        "config": _effective_paper_config(),
    }


def _get_paper_positions(status_filter: str) -> Dict[str, Any]:
    table = _table(PAPER_POSITIONS_TABLE)
    if table is None:
        return {"items": [], "count": 0}
    resp = table.query(
        KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
        Limit=500,
    )
    items = resp.get("Items") or []
    f = (status_filter or "all").lower()
    if f == "open":
        items = [i for i in items if i.get("status") == "OPEN"]
    elif f == "closed":
        items = [i for i in items if i.get("status") == "CLOSED"]
    # Open positions first, most recently updated first within each group.
    items.sort(
        key=lambda i: (
            0 if i.get("status") == "OPEN" else 1,
            str(i.get("updated_at") or i.get("opened_at") or ""),
        ),
        reverse=False,
    )
    return {"count": len(items), "items": items}


def _get_paper_trades(limit: int) -> Dict[str, Any]:
    table = _table(PAPER_TRADES_TABLE)
    if table is None:
        return {"items": [], "count": 0}
    resp = table.query(
        KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
        ScanIndexForward=False,
        Limit=max(1, min(limit, 500)),
    )
    items = resp.get("Items") or []
    return {"count": len(items), "items": items}


def _reset_paper() -> Dict[str, Any]:
    """Destructive: delete every paper position and trade row."""
    deleted_pos = 0
    deleted_trd = 0
    pos_table = _table(PAPER_POSITIONS_TABLE)
    if pos_table is not None:
        try:
            resp = pos_table.query(
                KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
                Limit=500,
            )
            with pos_table.batch_writer() as bw:
                for item in resp.get("Items") or []:
                    bw.delete_item(
                        Key={
                            "scanner_id": item["scanner_id"],
                            "position_sk": item["position_sk"],
                        }
                    )
                    deleted_pos += 1
        except Exception as e:  # pragma: no cover - defensive
            log.error("paper reset positions failed: %s", e)
    trd_table = _table(PAPER_TRADES_TABLE)
    if trd_table is not None:
        try:
            resp = trd_table.query(
                KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
                Limit=500,
            )
            with trd_table.batch_writer() as bw:
                for item in resp.get("Items") or []:
                    bw.delete_item(
                        Key={
                            "scanner_id": item["scanner_id"],
                            "trade_sk": item["trade_sk"],
                        }
                    )
                    deleted_trd += 1
        except Exception as e:  # pragma: no cover - defensive
            log.error("paper reset trades failed: %s", e)
    opp_table = _table(OPPORTUNITIES_TABLE)
    if opp_table is not None:
        try:
            opp_table.delete_item(
                Key={"scanner_id": SCANNER_ID, "sk": "paper#latest"}
            )
        except Exception as e:  # pragma: no cover - defensive
            log.warning("paper reset summary clear failed: %s", e)
    return {
        "ok": True,
        "deleted_positions": deleted_pos,
        "deleted_trades": deleted_trd,
    }


def _trigger_scan() -> Dict[str, Any]:
    if not SCANNER_FUNCTION_NAME:
        return {"ok": False, "error": "scanner function not configured"}
    try:
        _lambda.invoke(
            FunctionName=SCANNER_FUNCTION_NAME,
            InvocationType="Event",  # async
            Payload=b"{}",
        )
    except Exception as e:  # pragma: no cover - defensive
        log.error("scan invoke failed: %s", e)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "function": SCANNER_FUNCTION_NAME}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method, path = _route(event)
    try:
        if method == "OPTIONS":
            return _response(204, {})
        if path.endswith("/status") and method == "GET":
            return _response(200, _get_status())
        if path.endswith("/config") and method == "GET":
            return _response(200, _get_config())
        if path.endswith("/config") and method == "POST":
            return _response(200, _put_config(_body(event)))
        if path.endswith("/opportunities") and method == "GET":
            try:
                limit = int(_query(event).get("limit") or "50")
            except ValueError:
                limit = 50
            return _response(200, _get_opportunities(limit))
        if path.endswith("/scans") and method == "GET":
            try:
                limit = int(_query(event).get("limit") or "20")
            except ValueError:
                limit = 20
            return _response(200, _get_scans(limit))
        if path.endswith("/scan") and method == "POST":
            return _response(202, _trigger_scan())
        if path.endswith("/paper/status") and method == "GET":
            return _response(200, _get_paper_status())
        if path.endswith("/paper/positions") and method == "GET":
            status_filter = _query(event).get("status") or "all"
            return _response(200, _get_paper_positions(status_filter))
        if path.endswith("/paper/trades") and method == "GET":
            try:
                limit = int(_query(event).get("limit") or "100")
            except ValueError:
                limit = 100
            return _response(200, _get_paper_trades(limit))
        if path.endswith("/paper/reset") and method == "POST":
            return _response(200, _reset_paper())
        return _response(404, {"error": "not found", "method": method, "path": path})
    except Exception as e:  # pragma: no cover - defensive
        log.exception("handler error")
        return _response(500, {"error": str(e)})
