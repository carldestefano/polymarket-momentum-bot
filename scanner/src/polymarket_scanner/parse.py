"""Parsing helpers for Polymarket market shapes and threshold extraction."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_end_date(market: Dict[str, Any]) -> Optional[datetime]:
    """Pull the resolution/end time off a Gamma market record."""
    for key in ("endDate", "end_date", "endDateIso", "end_date_iso", "endTime"):
        raw = market.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None


def seconds_to_resolution(market: Dict[str, Any], now: Optional[datetime] = None) -> Optional[int]:
    end = parse_end_date(market)
    if end is None:
        return None
    ref = now or datetime.now(tz=timezone.utc)
    return int((end - ref).total_seconds())


_THRESHOLD_PATTERNS = (
    re.compile(r"\$\s*([0-9][0-9,]*\.?[0-9]*)\s*(k|m)?", re.IGNORECASE),
    re.compile(r"\b([0-9][0-9,]*\.?[0-9]*)\s*(k|m)\b", re.IGNORECASE),
)


def extract_price_threshold(text: Optional[str]) -> Optional[float]:
    """Extract a BTC price threshold from a market question.

    Examples of strings we try to handle:

        "Will Bitcoin hit $120,000 by Friday?"    -> 120000.0
        "BTC above 95k on April 30?"              -> 95000.0
        "Bitcoin price > 1.2m in 2030"            -> 1200000.0

    Returns None when no plausible price is found. This is a Stage 1
    best-effort extractor -- downstream code must treat the result as
    optional.
    """
    if not text:
        return None
    for pat in _THRESHOLD_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        num_raw, suffix = m.group(1), (m.group(2) or "").lower()
        try:
            val = float(num_raw.replace(",", ""))
        except ValueError:
            continue
        if suffix == "k":
            val *= 1_000
        elif suffix == "m":
            val *= 1_000_000
        # BTC prices are never this small in 2026 for a "threshold"; skip.
        if val < 1000:
            continue
        return val
    return None


def best_bid_ask(market: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Pull best bid / best ask off a Gamma market record."""
    bid = safe_float(market.get("bestBid") or market.get("best_bid"))
    ask = safe_float(market.get("bestAsk") or market.get("best_ask"))
    return bid, ask


def last_price(market: Dict[str, Any]) -> Optional[float]:
    for key in ("lastTradePrice", "last_price", "lastPrice", "price"):
        v = safe_float(market.get(key))
        if v is not None:
            return v
    # Gamma sometimes embeds token-level prices in an "outcomePrices" array
    prices = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(prices, list) and prices:
        v = safe_float(prices[0])
        if v is not None:
            return v
    if isinstance(prices, str):
        # Gamma occasionally serialises outcomePrices as a JSON string.
        import json as _json

        try:
            arr = _json.loads(prices)
        except Exception:  # pragma: no cover - defensive
            arr = None
        if isinstance(arr, list) and arr:
            v = safe_float(arr[0])
            if v is not None:
                return v
    return None


def volume_usd(market: Dict[str, Any]) -> Optional[float]:
    for key in ("volumeNum", "volume", "volume_num", "volumeUSD", "volume_usd"):
        v = safe_float(market.get(key))
        if v is not None:
            return v
    return None


def liquidity_usd(market: Dict[str, Any]) -> Optional[float]:
    for key in ("liquidityNum", "liquidity", "liquidity_num", "liquidityUSD"):
        v = safe_float(market.get(key))
        if v is not None:
            return v
    return None


def market_url(market: Dict[str, Any]) -> Optional[str]:
    slug = market.get("slug")
    if isinstance(slug, str) and slug:
        return f"https://polymarket.com/event/{slug}"
    mid = market.get("id") or market.get("marketId")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return None
