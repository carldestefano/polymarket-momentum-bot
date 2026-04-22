"""Ranking helpers for scanner opportunities."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _as_float(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def score_opportunity(opp: Dict[str, Any]) -> float:
    """Compute a simple interest score for a BTC opportunity.

    Higher score = more worth looking at. Factors:
      - absolute edge (|fair_value - mid|), weighted heavily when present
      - liquidity (log-scaled so a $1M market doesn't dominate by 1000x)
      - volume (same log-scale treatment)
      - short time to resolution bumps the score (more actionable)
      - wide spreads penalise the score (hard to act on / noisy)

    This is a Stage 1 heuristic. It is NOT a trading signal. It exists
    so the dashboard can show the most interesting markets first.
    """
    import math

    edge = opp.get("edge")
    edge_component = abs(_as_float(edge)) * 100.0 if edge is not None else 0.0

    liq = _as_float(opp.get("liquidity_usd"))
    vol = _as_float(opp.get("volume_usd"))
    liq_component = math.log10(liq + 1.0)
    vol_component = math.log10(vol + 1.0) * 0.5

    spread_component = 0.0
    sp = opp.get("spread")
    if sp is not None:
        spread_component = -_as_float(sp) * 20.0

    time_component = 0.0
    sec = opp.get("seconds_to_resolution")
    if isinstance(sec, (int, float)) and sec > 0:
        # Markets resolving within ~1 week get a mild boost, decaying to 0
        # as the horizon stretches out beyond a month.
        days = sec / 86400.0
        time_component = max(0.0, 5.0 - math.log10(days + 1.0) * 2.0)

    return (
        edge_component
        + liq_component
        + vol_component
        + spread_component
        + time_component
    )


def rank_opportunities(
    opportunities: List[Dict[str, Any]],
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    scored = []
    for opp in opportunities:
        o = dict(opp)
        o["score"] = score_opportunity(o)
        scored.append(o)
    scored.sort(key=lambda o: o["score"], reverse=True)
    if limit is not None:
        return scored[:limit]
    return scored
