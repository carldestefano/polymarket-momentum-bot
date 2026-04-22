"""BTC market classification.

Pure string-matching heuristics so there are no external deps and the
logic is easy to test offline. A market is classified as "BTC" when
its question, slug, or tags contain bitcoin-related keywords but
nothing that strongly suggests another asset (ETH, SOL, etc.).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

BTC_KEYWORDS = (
    "bitcoin",
    " btc",
    "btc ",
    "btc-",
    "btc/",
    "xbt",
)

# Tokens that should disqualify a market from being classified as BTC
# even if the word "bitcoin" appears elsewhere (e.g. "will ethereum
# flip bitcoin by 2026"). We prefer precision over recall here -- Stage 1
# is a scanner so missing a handful of markets is acceptable, but
# ranking an ETH market as BTC is not.
NON_BTC_ASSET_KEYWORDS = (
    "ethereum",
    " eth ",
    " eth-",
    "/eth",
    "solana",
    " sol ",
    "dogecoin",
    " doge ",
    "ripple",
    " xrp ",
    "cardano",
    " ada ",
    "litecoin",
    " ltc ",
)

SHORT_HORIZON_KEYWORDS = (
    "hour",
    "hourly",
    "today",
    "tonight",
    "this week",
    "by friday",
    "by monday",
    "by tuesday",
    "by wednesday",
    "by thursday",
    "by saturday",
    "by sunday",
    "end of day",
    "eod",
)


def _haystack(market: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("question", "slug", "description", "title", "category"):
        val = market.get(key)
        if isinstance(val, str):
            parts.append(val)
    tags = market.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                parts.append(t)
            elif isinstance(t, dict):
                for v in t.values():
                    if isinstance(v, str):
                        parts.append(v)
    return (" " + " ".join(parts) + " ").lower()


def is_btc_market(market: Dict[str, Any]) -> bool:
    """Return True when the market looks like a BTC/bitcoin market."""
    hay = _haystack(market)
    if not any(k in hay for k in BTC_KEYWORDS):
        return False
    if any(k in hay for k in NON_BTC_ASSET_KEYWORDS):
        # Any other crypto asset mentioned -> skip to avoid false positives.
        return False
    return True


def is_short_horizon(market: Dict[str, Any]) -> bool:
    """Rough heuristic for short-time BTC markets (hours / this week)."""
    hay = _haystack(market)
    return any(k in hay for k in SHORT_HORIZON_KEYWORDS)


def filter_btc(markets: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in markets if is_btc_market(m)]
