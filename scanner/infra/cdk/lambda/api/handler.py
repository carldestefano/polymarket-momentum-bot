"""HTTP API handler for the Polymarket BTC scanner dashboard.

Routes (all JSON, JWT-protected by API Gateway):

    GET  /status         -> { scanner_id, latest scan summary, tables }
    GET  /config         -> current scanner config overrides
    POST /config         -> set scanner config overrides (partial)
    GET  /opportunities  -> latest ranked BTC opportunities (?limit=50)
    GET  /scans          -> recent scan summaries (?limit=20)
    POST /scan           -> manually invoke the scanner Lambda

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
SCANNER_FUNCTION_NAME = os.environ.get("SCANNER_FUNCTION_NAME")

_ddb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")

OVERRIDABLE_KEYS = {
    "market_limit",
    "top_n",
    "short_horizon_only",
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


def _put_config(partial: Dict[str, Any]) -> Dict[str, Any]:
    table = _table(CONFIG_TABLE)
    if table is None:
        return {"error": "config table not configured"}
    current = _get_config() or {"scanner_id": SCANNER_ID}
    for k, v in partial.items():
        if k in OVERRIDABLE_KEYS:
            current[k] = v
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
        return _response(404, {"error": "not found", "method": method, "path": path})
    except Exception as e:  # pragma: no cover - defensive
        log.exception("handler error")
        return _response(500, {"error": str(e)})
