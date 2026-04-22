"""Polymarket public API client (read-only, no auth).

Uses urllib only so the Lambda has zero external dependencies and can be
deployed without a build step. The Gamma API is the public market catalog
and is sufficient for Stage 1 scanning -- CLOB order book queries are
not needed because bestBid/bestAsk are returned inline.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketError(RuntimeError):
    pass


def _get_json(url: str, *, timeout: float = 10.0, retries: int = 2) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "accept": "application/json",
                    "user-agent": "polymarket-scanner/0.1 (+github.com/carldestefano/polymarket-momentum-bot)",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(0.5 * (attempt + 1))
    raise PolymarketError(f"GET {url} failed: {last_err}")


def fetch_active_markets(
    *,
    limit: int = 500,
    tag: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch active, open markets from the public Gamma catalog.

    Paginates through Gamma's offset pagination until either `limit` is
    reached or the API returns an empty page.
    """
    per_page = 100
    collected: List[Dict[str, Any]] = []
    offset = 0
    while len(collected) < limit:
        params: Dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": per_page,
            "offset": offset,
            "order": "volumeNum",
            "ascending": "false",
        }
        if tag:
            params["tag_slug"] = tag
        url = f"{GAMMA_BASE}/markets?" + urllib.parse.urlencode(params)
        page = _get_json(url)
        if not isinstance(page, list) or not page:
            break
        collected.extend(page)
        if len(page) < per_page:
            break
        offset += per_page
    return collected[:limit]


def fetch_btc_spot_price() -> Optional[float]:
    """Best-effort current BTC price from a public source.

    Uses Coinbase's public spot endpoint, which requires no auth and has
    generous rate limits. Returns None on any failure so the scanner
    degrades gracefully (fair-value computations will then be skipped).
    """
    try:
        data = _get_json(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5.0,
            retries=1,
        )
        amt = data.get("data", {}).get("amount")
        if amt is None:
            return None
        return float(amt)
    except (PolymarketError, ValueError, TypeError, AttributeError) as e:
        log.warning("btc spot fetch failed: %s", e)
        return None
