"""End-to-end scan orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .classify import filter_btc, is_short_horizon
from .metrics import build_opportunity
from .polymarket import fetch_active_markets, fetch_btc_spot_price
from .rank import rank_opportunities

log = logging.getLogger(__name__)


def run_scan(
    *,
    market_limit: int = 500,
    top_n: int = 50,
    short_horizon_only: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pull markets, filter to BTC, score them, and return a scan result.

    Returns a dict with keys:
        scanned_at:    ISO8601 UTC timestamp
        btc_price_usd: float | None
        total_markets: int
        btc_markets:   int
        opportunities: list[dict]  (ranked, top N)
    """
    ref = now or datetime.now(tz=timezone.utc)
    raw_markets = fetch_active_markets(limit=market_limit)
    btc_markets = filter_btc(raw_markets)
    if short_horizon_only:
        btc_markets = [m for m in btc_markets if is_short_horizon(m)]

    btc_price = fetch_btc_spot_price()

    opps: List[Dict[str, Any]] = [
        build_opportunity(m, current_btc_price=btc_price, now=ref)
        for m in btc_markets
    ]
    ranked = rank_opportunities(opps, limit=top_n)

    return {
        "scanned_at": ref.isoformat().replace("+00:00", "Z"),
        "btc_price_usd": btc_price,
        "total_markets": len(raw_markets),
        "btc_markets": len(btc_markets),
        "opportunities": ranked,
    }
