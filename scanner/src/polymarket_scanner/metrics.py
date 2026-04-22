"""Scanner metrics: spread, mid, fair-value placeholder, edge, freshness."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .parse import (
    best_bid_ask,
    last_price,
    liquidity_usd,
    market_url,
    extract_price_threshold,
    seconds_to_resolution,
    volume_usd,
)


def mid_price(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def spread(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    if ask < bid:
        return None
    return ask - bid


def fair_value_placeholder(
    threshold_usd: Optional[float],
    current_btc_price: Optional[float],
    seconds_remaining: Optional[int],
    annualised_vol: float = 0.6,
) -> Optional[float]:
    """A very rough fair-value estimate for a "BTC > T by time T_end" market.

    Uses a log-normal approximation: P(BTC_end > threshold) with drift 0
    and annualised volatility `annualised_vol`. This is a **placeholder**
    for Stage 1 -- it is not a trading signal, just a rough number so the
    dashboard can show a directional edge vs. the market mid. Returns
    None when any input is missing/invalid.
    """
    if threshold_usd is None or current_btc_price is None or seconds_remaining is None:
        return None
    if threshold_usd <= 0 or current_btc_price <= 0 or seconds_remaining <= 0:
        return None
    # Use math.erf to avoid pulling in scipy/numpy.
    import math

    t_years = seconds_remaining / (365.25 * 24 * 3600)
    if t_years <= 0:
        return None
    sigma = annualised_vol * math.sqrt(t_years)
    if sigma <= 0:
        return None
    d = (math.log(current_btc_price / threshold_usd)) / sigma
    # P(X > threshold) for log-normal with drift 0 == Phi(d)
    phi = 0.5 * (1.0 + math.erf(d / math.sqrt(2.0)))
    # Clamp to [0,1] to avoid tiny float excursions.
    return max(0.0, min(1.0, phi))


def edge(fair_value: Optional[float], mid: Optional[float]) -> Optional[float]:
    if fair_value is None or mid is None:
        return None
    return fair_value - mid


def freshness_seconds(
    market: Dict[str, Any], now: Optional[datetime] = None
) -> Optional[int]:
    """How stale the market record is, in seconds, based on updatedAt."""
    for key in ("updatedAt", "updated_at", "lastUpdated", "last_updated"):
        raw = market.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            ts = float(raw)
            # Heuristic: treat >10^12 as milliseconds.
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(raw, str):
            text = raw.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            continue
        ref = now or datetime.now(tz=timezone.utc)
        return int((ref - dt).total_seconds())
    return None


def build_opportunity(
    market: Dict[str, Any],
    current_btc_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a dashboard-ready opportunity record from a Gamma market."""
    bid, ask = best_bid_ask(market)
    last = last_price(market)
    m = mid_price(bid, ask)
    sp = spread(bid, ask)
    sec_left = seconds_to_resolution(market, now=now)
    thresh = extract_price_threshold(market.get("question"))
    fair = fair_value_placeholder(thresh, current_btc_price, sec_left)
    return {
        "id": str(market.get("id") or market.get("slug") or ""),
        "question": market.get("question") or "",
        "slug": market.get("slug") or "",
        "url": market_url(market),
        "end_date": market.get("endDate") or market.get("end_date"),
        "seconds_to_resolution": sec_left,
        "best_bid": bid,
        "best_ask": ask,
        "mid": m,
        "spread": sp,
        "last_price": last,
        "volume_usd": volume_usd(market),
        "liquidity_usd": liquidity_usd(market),
        "threshold_usd": thresh,
        "fair_value": fair,
        "edge": edge(fair, m),
        "freshness_sec": freshness_seconds(market, now=now),
        "active": bool(market.get("active", True)),
        "closed": bool(market.get("closed", False)),
    }
