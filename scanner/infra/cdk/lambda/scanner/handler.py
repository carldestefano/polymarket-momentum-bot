"""Scanner Lambda: runs every ~5 minutes, writes results to DynamoDB.

This Lambda has NO wallet credentials and does NO trading. It only reads
public Polymarket Gamma API data, classifies BTC markets, computes
scanner metrics, and persists the results to DynamoDB so the dashboard
API can serve them.

The scanner package lives alongside this file in a `polymarket_scanner/`
directory; CDK copies both into the deployment zip via `Code.from_asset`.
"""

from __future__ import annotations

import decimal
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# Ensure the bundled scanner package is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from polymarket_scanner.paper import run_paper_tick  # noqa: E402
from polymarket_scanner.scan import run_scan  # noqa: E402

SCANS_TABLE = os.environ.get("SCANS_TABLE")
OPPORTUNITIES_TABLE = os.environ.get("OPPORTUNITIES_TABLE")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE")
PAPER_POSITIONS_TABLE = os.environ.get("PAPER_POSITIONS_TABLE")
PAPER_TRADES_TABLE = os.environ.get("PAPER_TRADES_TABLE")
SCANNER_ID = os.environ.get("SCANNER_ID", "default")
MARKET_LIMIT = int(os.environ.get("MARKET_LIMIT", "500"))
TOP_N = int(os.environ.get("TOP_N", "50"))

_ddb = boto3.resource("dynamodb")


def _to_decimal(obj: Any) -> Any:
    """DynamoDB refuses floats; convert recursively to Decimal."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return decimal.Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


def _from_decimal(obj: Any) -> Any:
    """Inverse of _to_decimal for items we read back and feed into paper logic."""
    if isinstance(obj, decimal.Decimal):
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: _from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_decimal(v) for v in obj]
    return obj


def _load_config() -> Dict[str, Any]:
    if not CONFIG_TABLE:
        return {}
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"scanner_id": SCANNER_ID})
        item = resp.get("Item") or {}
        return item
    except Exception as e:  # pragma: no cover - defensive
        log.warning("config load failed: %s", e)
        return {}


def _write_scan(result: Dict[str, Any]) -> None:
    if not SCANS_TABLE:
        return
    item = _to_decimal(
        {
            "scanner_id": SCANNER_ID,
            "scanned_at": result["scanned_at"],
            "btc_price_usd": result.get("btc_price_usd"),
            "total_markets": result.get("total_markets"),
            "btc_markets": result.get("btc_markets"),
            "top_n": len(result.get("opportunities") or []),
        }
    )
    _ddb.Table(SCANS_TABLE).put_item(Item=item)


def _write_opportunities(result: Dict[str, Any]) -> None:
    if not OPPORTUNITIES_TABLE:
        return
    table = _ddb.Table(OPPORTUNITIES_TABLE)
    scanned_at = result["scanned_at"]
    # One item per (scanner_id, sk) where sk embeds rank so query results
    # are already ordered by score when reading the latest scan.
    with table.batch_writer(overwrite_by_pkeys=["scanner_id", "sk"]) as bw:
        # Latest pointer row so the API can cheaply answer "what is the
        # most recent scan".
        bw.put_item(
            Item=_to_decimal(
                {
                    "scanner_id": SCANNER_ID,
                    "sk": "latest#meta",
                    "scanned_at": scanned_at,
                    "btc_price_usd": result.get("btc_price_usd"),
                    "total_markets": result.get("total_markets"),
                    "btc_markets": result.get("btc_markets"),
                }
            )
        )
        for rank, opp in enumerate(result.get("opportunities") or []):
            bw.put_item(
                Item=_to_decimal(
                    {
                        "scanner_id": SCANNER_ID,
                        "sk": f"latest#{rank:04d}",
                        "scanned_at": scanned_at,
                        "rank": rank,
                        **opp,
                    }
                )
            )


def _load_paper_positions() -> list:
    if not PAPER_POSITIONS_TABLE:
        return []
    try:
        from boto3.dynamodb.conditions import Key

        resp = _ddb.Table(PAPER_POSITIONS_TABLE).query(
            KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
            Limit=500,
        )
        items = resp.get("Items") or []
        return [_from_decimal(i) for i in items]
    except Exception as e:  # pragma: no cover - defensive
        log.warning("paper positions load failed: %s", e)
        return []


def _load_recent_fills(limit: int = 50) -> list:
    if not PAPER_TRADES_TABLE:
        return []
    try:
        from boto3.dynamodb.conditions import Key

        resp = _ddb.Table(PAPER_TRADES_TABLE).query(
            KeyConditionExpression=Key("scanner_id").eq(SCANNER_ID),
            ScanIndexForward=False,  # newest first
            Limit=max(1, min(limit, 200)),
        )
        items = resp.get("Items") or []
        return [_from_decimal(i) for i in items]
    except Exception as e:  # pragma: no cover - defensive
        log.warning("paper fills load failed: %s", e)
        return []


def _write_paper_positions(positions: list) -> None:
    if not PAPER_POSITIONS_TABLE:
        return
    table = _ddb.Table(PAPER_POSITIONS_TABLE)
    # Sort key: OPEN# sorts before CLOSED# so queries return live first.
    with table.batch_writer(
        overwrite_by_pkeys=["scanner_id", "position_sk"]
    ) as bw:
        for p in positions:
            status = p.get("status") or "OPEN"
            opened = p.get("opened_at") or ""
            market_id = str(p.get("market_id") or "")
            sk = f"{status}#{opened}#{market_id}"
            item = {
                "scanner_id": SCANNER_ID,
                "position_sk": sk,
                **p,
            }
            bw.put_item(Item=_to_decimal(item))


def _write_paper_fills(fills: list) -> None:
    if not PAPER_TRADES_TABLE or not fills:
        return
    table = _ddb.Table(PAPER_TRADES_TABLE)
    with table.batch_writer(
        overwrite_by_pkeys=["scanner_id", "trade_sk"]
    ) as bw:
        for i, f in enumerate(fills):
            ts = f.get("ts") or ""
            market_id = str(f.get("market_id") or "")
            # Include a per-batch counter so two fills in the same
            # millisecond don't collide on the sort key.
            sk = f"{ts}#{i:04d}#{market_id}"
            item = {
                "scanner_id": SCANNER_ID,
                "trade_sk": sk,
                **f,
            }
            bw.put_item(Item=_to_decimal(item))


def _write_paper_summary(summary: Dict[str, Any]) -> None:
    """Pin the latest paper-trading summary onto opportunities meta row."""
    if not OPPORTUNITIES_TABLE:
        return
    try:
        _ddb.Table(OPPORTUNITIES_TABLE).put_item(
            Item=_to_decimal(
                {
                    "scanner_id": SCANNER_ID,
                    "sk": "paper#latest",
                    **summary,
                }
            )
        )
    except Exception as e:  # pragma: no cover - defensive
        log.warning("paper summary write failed: %s", e)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    started = time.time()
    cfg = _load_config()
    market_limit = int(cfg.get("market_limit") or MARKET_LIMIT)
    top_n = int(cfg.get("top_n") or TOP_N)
    short_only = bool(cfg.get("short_horizon_only") or False)

    result = run_scan(
        market_limit=market_limit,
        top_n=top_n,
        short_horizon_only=short_only,
        now=datetime.now(tz=timezone.utc),
    )
    _write_scan(result)
    _write_opportunities(result)

    # --- Stage 2: paper trading tick ---------------------------------------
    # Always run the paper tick so mark-to-market happens on every scan;
    # the engine itself respects the `paper_trading_enabled` config flag
    # and skips new opens when disabled.
    paper_summary: Dict[str, Any] = {}
    try:
        prior_positions = _load_paper_positions()
        recent_fills = _load_recent_fills(limit=50)
        tick = run_paper_tick(
            scanned_at=result["scanned_at"],
            opportunities=result.get("opportunities") or [],
            positions=prior_positions,
            recent_fills=recent_fills,
            config=cfg,
            now=datetime.now(tz=timezone.utc),
        )
        _write_paper_positions(tick["positions"])
        _write_paper_fills(tick["fills"])
        paper_summary = tick["summary"]
        _write_paper_summary(paper_summary)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("paper tick failed: %s", e)
        paper_summary = {"error": str(e)}

    elapsed = time.time() - started
    log.info(
        "scan ok: total=%s btc=%s top=%s paper_open=%s elapsed=%.2fs",
        result.get("total_markets"),
        result.get("btc_markets"),
        len(result.get("opportunities") or []),
        paper_summary.get("open_count"),
        elapsed,
    )
    return {
        "ok": True,
        "scanned_at": result["scanned_at"],
        "total_markets": result.get("total_markets"),
        "btc_markets": result.get("btc_markets"),
        "top_n": len(result.get("opportunities") or []),
        "paper": paper_summary,
        "elapsed_sec": round(elapsed, 3),
    }


if __name__ == "__main__":  # pragma: no cover - local smoke test
    print(json.dumps(lambda_handler({}, None), default=str, indent=2))
