"""Paper trading engine for Stage 2.

Pure-Python simulation over Stage 1 opportunity records. This module:

- Decides which opportunities should open a simulated BUY YES position.
- Sizes the position against risk-control limits (per-market and total).
- Records simulated fills at the current ask (never at mid) with an extra
  conservative slippage buffer so paper P&L is not falsely optimistic.
- Marks open positions using the current best bid (conservative exit
  price) and closes them when the edge flips, liquidity dries up, the
  market approaches resolution, or a config-driven hard stop triggers.

There is deliberately no NO-side simulation: Polymarket's NO token and
NO-side order book are not reliably available in the raw Gamma feed the
scanner consumes, so the "fair_value below mid" signal would imply an
asymmetry we cannot faithfully price. BUY YES only keeps the paper P&L
honest.

The engine is **stateless**. Callers hand in the previous open positions
plus the new ranked opportunities; the engine returns:

    {
        "positions": list[PaperPosition dict],
        "fills":     list[PaperFill dict that happened this tick],
        "summary":   aggregate metrics,
        "config":    the effective config used,
    }

Storage (DynamoDB in prod, dict in tests) is the caller's responsibility.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

log = logging.getLogger(__name__)


# --- Defaults ---------------------------------------------------------------
#
# Defaults are deliberately CONSERVATIVE. paper_trading_enabled defaults to
# False so that deploying Stage 2 does not change scan behavior until the
# operator explicitly flips the flag in ConfigTable. See README for the
# exact AWS CLI command.

DEFAULT_CONFIG: Dict[str, Any] = {
    "paper_trading_enabled": False,
    "max_paper_trade_usdc": 100.0,
    "max_paper_position_usdc_per_market": 250.0,
    "max_total_paper_exposure_usdc": 2000.0,
    "min_edge_to_trade": 0.05,           # fair_value - mid, in YES cents
    "min_liquidity_usdc": 500.0,
    "max_spread": 0.10,                  # ask - bid, in YES cents
    "max_resolution_days": 30,
    "cooldown_seconds": 15 * 60,         # 15 minutes between re-opens per market
    "allow_short_horizon_only": False,
    "close_on_edge_flip": True,
    "slippage_buffer": 0.01,             # extra cost added to fill ask
}


# Keys the API allows the operator to override. Must stay in sync with the
# scanner-level OVERRIDABLE_KEYS so the API POST /config round-trips.
PAPER_CONFIG_KEYS = frozenset(
    [
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
    ]
)


def effective_config(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Merge caller overrides on top of DEFAULT_CONFIG. Ignores unknown keys."""
    cfg = dict(DEFAULT_CONFIG)
    if not overrides:
        return cfg
    for k, v in overrides.items():
        if k not in PAPER_CONFIG_KEYS:
            continue
        if v is None:
            continue
        if isinstance(DEFAULT_CONFIG[k], bool):
            cfg[k] = bool(v)
        else:
            try:
                cfg[k] = float(v) if isinstance(DEFAULT_CONFIG[k], float) else type(DEFAULT_CONFIG[k])(v)
            except (TypeError, ValueError):
                continue
    return cfg


# --- Helpers ----------------------------------------------------------------

def _iso_now(now: Optional[datetime] = None) -> str:
    ref = now or datetime.now(tz=timezone.utc)
    return ref.isoformat().replace("+00:00", "Z")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- Entry/exit logic -------------------------------------------------------

def _reject_reason(opp: Mapping[str, Any], cfg: Mapping[str, Any]) -> Optional[str]:
    """Return a string reason why `opp` cannot open a new paper position.

    None means the opportunity passes all gates.
    """
    bid = _num(opp.get("best_bid"))
    ask = _num(opp.get("best_ask"))
    mid = _num(opp.get("mid"))
    fair = _num(opp.get("fair_value"))
    ed = _num(opp.get("edge"))
    liq = _num(opp.get("liquidity_usd"))
    sec_left = opp.get("seconds_to_resolution")

    if opp.get("closed"):
        return "market_closed"
    if ask is None or ask <= 0 or ask >= 1.0:
        return "no_executable_ask"
    if bid is None or bid <= 0:
        return "no_bid"
    if fair is None or mid is None or ed is None:
        return "no_fair_value"
    if ed < cfg["min_edge_to_trade"]:
        return "edge_below_threshold"
    sp = ask - bid
    if sp > cfg["max_spread"]:
        return "spread_too_wide"
    if liq is None or liq < cfg["min_liquidity_usdc"]:
        return "liquidity_too_low"
    try:
        sec_left_f = float(sec_left) if sec_left is not None else None
    except (TypeError, ValueError):
        sec_left_f = None
    if sec_left_f is None or sec_left_f <= 0:
        return "no_resolution_time"
    if sec_left_f / 86400.0 > cfg["max_resolution_days"]:
        return "resolves_too_far_out"
    if cfg.get("allow_short_horizon_only") and sec_left_f > 7 * 86400:
        return "not_short_horizon"
    return None


def _is_on_cooldown(
    market_id: str,
    recent_fills: Iterable[Mapping[str, Any]],
    now: datetime,
    cooldown_seconds: float,
) -> bool:
    """True if this market already had an open/close within the cooldown window."""
    if cooldown_seconds <= 0:
        return False
    for f in recent_fills:
        if f.get("market_id") != market_id:
            continue
        ts = _parse_iso(f.get("ts"))
        if ts is None:
            continue
        if (now - ts).total_seconds() < cooldown_seconds:
            return True
    return False


def _size_trade(
    *,
    cfg: Mapping[str, Any],
    ask: float,
    existing_notional_for_market: float,
    existing_total_exposure: float,
) -> float:
    """Compute the notional (USDC) to open for a new position. 0 means skip."""
    per_trade = float(cfg["max_paper_trade_usdc"])
    per_market_cap = max(
        0.0, float(cfg["max_paper_position_usdc_per_market"]) - existing_notional_for_market
    )
    total_cap = max(
        0.0, float(cfg["max_total_paper_exposure_usdc"]) - existing_total_exposure
    )
    notional = min(per_trade, per_market_cap, total_cap)
    # Round down to cents and require at least $1 to avoid dust fills.
    notional = max(0.0, round(notional, 2))
    if notional < 1.0:
        return 0.0
    if ask <= 0 or ask >= 1.0:
        return 0.0
    return notional


def _mark_position(pos: Dict[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    """Update mark price and unrealized P&L on an open position using current bid."""
    bid = _num(current.get("best_bid"))
    ask = _num(current.get("best_ask"))
    mid = _num(current.get("mid"))
    mark = bid if bid is not None and bid > 0 else mid
    pos["mark_price"] = mark
    pos["current_bid"] = bid
    pos["current_ask"] = ask
    pos["current_mid"] = mid
    pos["current_edge"] = _num(current.get("edge"))
    pos["current_fair_value"] = _num(current.get("fair_value"))
    entry = float(pos.get("entry_price") or 0.0)
    shares = float(pos.get("shares") or 0.0)
    if mark is None or entry <= 0 or shares <= 0:
        pos["unrealized_pnl"] = 0.0
    else:
        pos["unrealized_pnl"] = round(shares * (mark - entry), 4)
    return pos


def _should_close(
    pos: Mapping[str, Any],
    current: Optional[Mapping[str, Any]],
    cfg: Mapping[str, Any],
) -> Optional[str]:
    """Return a close_reason string, or None to hold the position."""
    if current is None:
        return "market_gone"
    if current.get("closed"):
        return "market_closed"
    sec_left = current.get("seconds_to_resolution")
    try:
        sec_left_f = float(sec_left) if sec_left is not None else None
    except (TypeError, ValueError):
        sec_left_f = None
    if sec_left_f is not None and sec_left_f <= 0:
        return "resolution_reached"
    if sec_left_f is not None and sec_left_f < 3600:
        # Within 1h of resolution -> flatten.
        return "near_resolution"
    edge = _num(current.get("edge"))
    if cfg.get("close_on_edge_flip") and edge is not None and edge < 0:
        return "edge_flipped"
    bid = _num(current.get("best_bid"))
    if bid is None or bid <= 0:
        return "no_bid"
    return None


# --- Main tick --------------------------------------------------------------

def run_paper_tick(
    *,
    scanned_at: str,
    opportunities: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    recent_fills: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Advance the paper portfolio one tick.

    Deterministic: given the same inputs, returns the same fills and
    positions. The caller is responsible for persisting `positions` and
    appending the returned `fills` to a trade log.
    """
    ref = now or datetime.now(tz=timezone.utc)
    cfg = effective_config(config)
    recent_fills = list(recent_fills or [])
    opps_by_id: Dict[str, Dict[str, Any]] = {}
    for o in opportunities:
        mid = str(o.get("id") or o.get("slug") or "")
        if mid:
            opps_by_id[mid] = o

    closed: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    new_fills: List[Dict[str, Any]] = []

    # 1) Mark existing positions, decide who closes.
    for pos in positions:
        if pos.get("status") != "OPEN":
            kept.append(pos)
            continue
        current = opps_by_id.get(str(pos.get("market_id") or ""))
        if current is not None:
            _mark_position(pos, current)
        reason = _should_close(pos, current, cfg)
        if reason is None:
            kept.append(pos)
            continue
        # Exit at current bid when we have one; fall back to last mark.
        exit_price = _num((current or {}).get("best_bid"))
        if exit_price is None or exit_price <= 0:
            exit_price = _num(pos.get("mark_price")) or 0.0
        shares = float(pos.get("shares") or 0.0)
        entry = float(pos.get("entry_price") or 0.0)
        realized = round(shares * (exit_price - entry), 4)
        pos["status"] = "CLOSED"
        pos["exit_price"] = round(float(exit_price), 6) if exit_price else 0.0
        pos["realized_pnl"] = realized
        pos["unrealized_pnl"] = 0.0
        pos["closed_at"] = _iso_now(ref)
        pos["close_reason"] = reason
        closed.append(pos)
        new_fills.append(
            {
                "market_id": pos.get("market_id"),
                "token_id": pos.get("token_id"),
                "side": "SELL",
                "price": round(float(exit_price), 6) if exit_price else 0.0,
                "shares": round(shares, 6),
                "notional_usdc": round(shares * float(exit_price or 0.0), 4),
                "ts": _iso_now(ref),
                "reason": "close:" + reason,
                "question": pos.get("question"),
                "url": pos.get("url"),
            }
        )

    # 2) Compute current open exposure for sizing.
    def _notional_for(mid: str) -> float:
        total = 0.0
        for p in kept:
            if p.get("status") != "OPEN":
                continue
            if str(p.get("market_id")) == mid:
                total += float(p.get("notional_usdc") or 0.0)
        return total

    def _total_exposure() -> float:
        return sum(
            float(p.get("notional_usdc") or 0.0)
            for p in kept
            if p.get("status") == "OPEN"
        )

    # 3) Consider new opportunities. Cooldown uses this-tick fills + caller's
    #    recent_fills, so rapid re-opens are prevented even across scans.
    all_fills_for_cooldown = list(recent_fills) + list(new_fills)

    if not cfg["paper_trading_enabled"]:
        # Still mark positions and return. No new opens allowed.
        return _build_result(scanned_at, kept, new_fills, cfg, ref, opened=0, skipped_disabled=True)

    opened_count = 0
    for opp in opportunities:
        mid = str(opp.get("id") or opp.get("slug") or "")
        if not mid:
            continue
        # Skip if we already have an OPEN position in this market (scale-in
        # adds complexity and fake precision, not worth it for Stage 2).
        if any(
            p.get("status") == "OPEN" and str(p.get("market_id")) == mid for p in kept
        ):
            continue
        reason = _reject_reason(opp, cfg)
        if reason is not None:
            continue
        if _is_on_cooldown(mid, all_fills_for_cooldown, ref, float(cfg["cooldown_seconds"])):
            continue

        ask = float(opp["best_ask"])
        fill_price = min(0.99, round(ask + float(cfg["slippage_buffer"]), 4))
        notional = _size_trade(
            cfg=cfg,
            ask=fill_price,
            existing_notional_for_market=_notional_for(mid),
            existing_total_exposure=_total_exposure(),
        )
        if notional <= 0:
            continue
        shares = round(notional / fill_price, 6)
        pos = {
            "market_id": mid,
            "token_id": str(opp.get("token_id") or ""),
            "outcome": "YES",
            "question": opp.get("question"),
            "slug": opp.get("slug"),
            "url": opp.get("url"),
            "side": "BUY",
            "entry_price": fill_price,
            "shares": shares,
            "notional_usdc": round(shares * fill_price, 4),
            "status": "OPEN",
            "opened_at": _iso_now(ref),
            "updated_at": _iso_now(ref),
            "current_bid": _num(opp.get("best_bid")),
            "current_ask": _num(opp.get("best_ask")),
            "current_mid": _num(opp.get("mid")),
            "current_edge": _num(opp.get("edge")),
            "current_fair_value": _num(opp.get("fair_value")),
            "mark_price": _num(opp.get("best_bid")),
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "close_reason": None,
            "exit_price": None,
            "closed_at": None,
        }
        _mark_position(pos, opp)
        kept.append(pos)
        opened_count += 1
        new_fills.append(
            {
                "market_id": mid,
                "token_id": pos["token_id"],
                "side": "BUY",
                "price": fill_price,
                "shares": shares,
                "notional_usdc": pos["notional_usdc"],
                "ts": _iso_now(ref),
                "reason": "open:edge>=threshold",
                "question": opp.get("question"),
                "url": opp.get("url"),
            }
        )

    # 4) touch updated_at on kept positions so dashboards show freshness.
    for p in kept:
        if p.get("status") == "OPEN":
            p["updated_at"] = _iso_now(ref)

    return _build_result(scanned_at, kept + closed, new_fills, cfg, ref, opened=opened_count)


def _build_result(
    scanned_at: str,
    all_positions: List[Dict[str, Any]],
    new_fills: List[Dict[str, Any]],
    cfg: Mapping[str, Any],
    ref: datetime,
    *,
    opened: int = 0,
    skipped_disabled: bool = False,
) -> Dict[str, Any]:
    open_positions = [p for p in all_positions if p.get("status") == "OPEN"]
    closed_positions = [p for p in all_positions if p.get("status") == "CLOSED"]
    open_exposure = round(
        sum(float(p.get("notional_usdc") or 0.0) for p in open_positions), 4
    )
    unrealized = round(
        sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions), 4
    )
    realized = round(
        sum(float(p.get("realized_pnl") or 0.0) for p in closed_positions), 4
    )
    wins = sum(1 for p in closed_positions if float(p.get("realized_pnl") or 0.0) > 0)
    losses = sum(
        1 for p in closed_positions if float(p.get("realized_pnl") or 0.0) < 0
    )
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else None

    summary = {
        "scanned_at": scanned_at,
        "evaluated_at": _iso_now(ref),
        "enabled": bool(cfg["paper_trading_enabled"]),
        "skipped_disabled": skipped_disabled,
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "open_exposure_usdc": open_exposure,
        "unrealized_pnl_usdc": unrealized,
        "realized_pnl_usdc": realized,
        "total_pnl_usdc": round(unrealized + realized, 4),
        "trade_count": len(closed_positions),
        "win_rate": win_rate,
        "opened_this_tick": opened,
        "fills_this_tick": len(new_fills),
    }
    return {
        "positions": all_positions,
        "fills": new_fills,
        "summary": summary,
        "config": dict(cfg),
    }
