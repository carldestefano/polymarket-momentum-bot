"""Public Polymarket market-data helpers.

These endpoints do **not** require a wallet, signature, or API key. They are
used for market discovery, price history, order-book snapshots, etc.

References
----------
- Gamma API (events + market metadata): https://gamma-api.polymarket.com
- Data API (positions, trades, etc.):   https://data-api.polymarket.com
- CLOB market-data endpoints:           https://clob.polymarket.com
  * GET /prices-history?market=<tokenId>&interval=<1h|6h|1d|1w|1m|max>
    &fidelity=<minutes>&startTs=<unix>&endTs=<unix>
  * GET /price?token_id=<tokenId>&side=<BUY|SELL>
  * GET /book?token_id=<tokenId>
  * GET /midpoint?token_id=<tokenId>
  * GET /spread?token_id=<tokenId>
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
DEFAULT_CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 15


class MarketDataClient:
    """Thin wrapper around Polymarket's public HTTP endpoints."""

    def __init__(
        self,
        clob_host: str = DEFAULT_CLOB_BASE,
        gamma_host: str = GAMMA_BASE,
        session: Optional[requests.Session] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.clob_host = clob_host.rstrip("/")
        self.gamma_host = gamma_host.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ Gamma

    def list_active_events(
        self,
        limit: int = 100,
        offset: int = 0,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return active, non-closed events (each contains one or more markets)."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        url = f"{self.gamma_host}/events"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return list(data["data"])
        return list(data)

    def list_active_markets(
        self, max_markets: int = 50, allowed_categories: Optional[Iterable[str]] = None
    ) -> List[Dict[str, Any]]:
        """Flatten events into markets, sorted by 24h volume (desc)."""
        allow = {c.lower() for c in allowed_categories or []}
        events = self.list_active_events(limit=min(100, max_markets * 4))
        markets: List[Dict[str, Any]] = []
        for ev in events:
            if allow:
                tags = {str(t).lower() for t in ev.get("tags") or []}
                category = str(ev.get("category") or "").lower()
                if not (allow & tags) and category not in allow:
                    continue
            for m in ev.get("markets") or []:
                if not m.get("active") or m.get("closed"):
                    continue
                markets.append(m)
        markets.sort(key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
        return markets[:max_markets]

    # -------------------------------------------------------------- CLOB data

    def price_history(
        self,
        token_id: str,
        interval: str = "1h",
        fidelity: int = 60,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[Dict[str, float]]:
        """Return price history as a list of ``{"t": int, "p": float}`` points."""
        params: Dict[str, Any] = {"market": token_id, "fidelity": fidelity}
        if interval:
            params["interval"] = interval
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        url = f"{self.clob_host}/prices-history"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        history = body.get("history") if isinstance(body, dict) else body
        out: List[Dict[str, float]] = []
        for point in history or []:
            try:
                out.append({"t": int(point["t"]), "p": float(point["p"])})
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def midpoint(self, token_id: str) -> Optional[float]:
        url = f"{self.clob_host}/midpoint"
        try:
            resp = self.session.get(
                url, params={"token_id": token_id}, timeout=self.timeout
            )
            resp.raise_for_status()
            body = resp.json()
            mid = body.get("mid") if isinstance(body, dict) else None
            return float(mid) if mid is not None else None
        except (requests.RequestException, ValueError) as exc:
            log.warning("midpoint failed for %s: %s", token_id, exc)
            return None

    def spread(self, token_id: str) -> Optional[float]:
        url = f"{self.clob_host}/spread"
        try:
            resp = self.session.get(
                url, params={"token_id": token_id}, timeout=self.timeout
            )
            resp.raise_for_status()
            body = resp.json()
            val = body.get("spread") if isinstance(body, dict) else None
            return float(val) if val is not None else None
        except (requests.RequestException, ValueError) as exc:
            log.warning("spread failed for %s: %s", token_id, exc)
            return None

    def book(self, token_id: str) -> Dict[str, Any]:
        url = f"{self.clob_host}/book"
        resp = self.session.get(
            url, params={"token_id": token_id}, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()


def extract_token_ids(market: Dict[str, Any]) -> List[str]:
    """Return the list of CLOB token ids for a Gamma ``market`` object.

    Gamma returns ``clobTokenIds`` as either a JSON-encoded string or a list
    depending on the endpoint / version, so be defensive.
    """
    raw = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []
