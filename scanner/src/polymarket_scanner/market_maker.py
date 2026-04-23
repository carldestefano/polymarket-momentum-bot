"""Market-making simulator for Stage 3.

Pure-Python simulation over Stage 1 opportunity records. This module is
100% simulation: it NEVER places orders, contains NO wallet or private
keys, and requires NO Polymarket auth. It produces fair-value-based
quote recommendations, tracks a synthetic quote lifecycle, simulates
conservative maker-only fills based on current book movement, and marks
inventory to market.

Design choices:

- Maker-only (no crossing the book) by default. Target YES bid is placed
  below the current best ask and target YES ask is placed above the
  current best bid. A simulated fill only occurs when the current book
  "touches" the quote: a BUY (bid) fills if the current ask <= quote_bid,
  a SELL (ask) fills if the current bid >= quote_ask. This is
  deliberately conservative: we do not invent fills that the visible
  book does not justify.

- YES-only inventory. We do not short YES. ASK fills are only permitted
  if they reduce existing long inventory (partial fills clamped to
  available shares). This prevents the simulator from showing fake P&L
  from a short leg we cannot actually model.

- Inventory skew: long YES -> lower both quotes so the ask becomes more
  aggressive (easier to hit, reducing inventory) and the bid becomes
  less aggressive (harder to add to inventory). Flat -> center around
  fair value.

- Deterministic: given the same inputs, returns the same quotes, fills,
  and inventory. The caller passes `now=` explicitly in tests.

Storage (DynamoDB in prod, dict in tests) is the caller's responsibility.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

log = logging.getLogger(__name__)


# --- Defaults ---------------------------------------------------------------
#
# Defaults are CONSERVATIVE. market_making_enabled defaults to False so
# that deploying Stage 3 does not change scan behavior until the operator
# explicitly flips the flag in ConfigTable.

DEFAULT_CONFIG: Dict[str, Any] = {
    "market_making_enabled": False,
    "mm_max_markets": 5,
    "mm_min_liquidity_usdc": 1000.0,
    "mm_max_spread": 0.15,
    "mm_min_edge_or_width": 0.02,       # min (fv_edge OR quote width)
    "mm_quote_size_usdc": 50.0,
    "mm_max_position_usdc_per_market": 200.0,
    "mm_max_total_inventory_usdc": 1000.0,
    "mm_base_quote_width": 0.04,        # total width around fair value
    "mm_inventory_skew_factor": 0.5,    # fraction of width shifted by full inventory
    "mm_cancel_if_stale_seconds": 300,  # 5 min stale -> cancel
    "mm_avoid_near_resolution_seconds": 3600,  # skip markets < 1h to resolution
    "mm_fill_probability": 1.0,         # reserved; deterministic book-cross model
}


MM_CONFIG_KEYS = frozenset(DEFAULT_CONFIG.keys())


def effective_config(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Merge caller overrides on top of DEFAULT_CONFIG. Ignores unknown keys."""
    cfg = dict(DEFAULT_CONFIG)
    if not overrides:
        return cfg
    for k, v in overrides.items():
        if k not in MM_CONFIG_KEYS:
            continue
        if v is None:
            continue
        default = DEFAULT_CONFIG[k]
        if isinstance(default, bool):
            cfg[k] = bool(v)
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                cfg[k] = int(v)
            except (TypeError, ValueError):
                continue
        elif isinstance(default, float):
            try:
                cfg[k] = float(v)
            except (TypeError, ValueError):
                continue
        else:
            cfg[k] = v
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


def _clamp_price(p: float) -> float:
    """Clamp a YES probability to a sane tradeable range."""
    if p < 0.01:
        return 0.01
    if p > 0.99:
        return 0.99
    return round(p, 4)


# --- Risk gates / quote generation ------------------------------------------

def _reject_reason(opp: Mapping[str, Any], cfg: Mapping[str, Any]) -> Optional[str]:
    """Return a string reason why we should not quote this market."""
    bid = _num(opp.get("best_bid"))
    ask = _num(opp.get("best_ask"))
    fair = _num(opp.get("fair_value"))
    liq = _num(opp.get("liquidity_usd"))
    sec_left = opp.get("seconds_to_resolution")

    if opp.get("closed"):
        return "market_closed"
    if fair is None:
        return "no_fair_value"
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return "no_book"
    if ask >= 1.0 or bid >= 1.0:
        return "invalid_book"
    if ask <= bid:
        return "crossed_book"
    spread = ask - bid
    if spread > cfg["mm_max_spread"]:
        return "spread_too_wide"
    if liq is None or liq < cfg["mm_min_liquidity_usdc"]:
        return "liquidity_too_low"
    try:
        sec_left_f = float(sec_left) if sec_left is not None else None
    except (TypeError, ValueError):
        sec_left_f = None
    if sec_left_f is None or sec_left_f <= 0:
        return "no_resolution_time"
    if sec_left_f < cfg["mm_avoid_near_resolution_seconds"]:
        return "near_resolution"
    return None


def generate_quotes(
    opp: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    current_inventory_shares: float = 0.0,
) -> Optional[Dict[str, float]]:
    """Compute target bid/ask for a market. Returns None if we should not quote.

    - quote_bid = fair_value - width/2 - skew
    - quote_ask = fair_value + width/2 - skew
    Skew > 0 when long (inventory > 0): both quotes move down so the ask
    gets closer to mid (easier hit, reduces inventory) and the bid gets
    farther (harder hit, reduces adds).

    The quote is adjusted to not cross the current book (maker-only):
      quote_bid <= best_ask - 0.005
      quote_ask >= best_bid + 0.005
    """
    if _reject_reason(opp, cfg) is not None:
        return None

    fair = float(opp["fair_value"])
    bid = float(opp["best_bid"])
    ask = float(opp["best_ask"])
    width = float(cfg["mm_base_quote_width"])
    max_pos = float(cfg["mm_max_position_usdc_per_market"])
    skew_factor = float(cfg["mm_inventory_skew_factor"])

    # Skew proportional to inventory shares / max_pos at mid (~1 USDC/share baseline).
    # We normalize shares by (max_pos / fair) so "full position" = full skew.
    if fair > 0 and max_pos > 0:
        max_shares = max_pos / max(fair, 0.01)
        inv_ratio = max(-1.0, min(1.0, current_inventory_shares / max_shares))
    else:
        inv_ratio = 0.0
    skew = inv_ratio * skew_factor * width

    q_bid = fair - width / 2.0 - skew
    q_ask = fair + width / 2.0 - skew

    # Maker-only: never post through the book.
    q_bid = min(q_bid, ask - 0.005)
    q_ask = max(q_ask, bid + 0.005)

    q_bid = _clamp_price(q_bid)
    q_ask = _clamp_price(q_ask)

    if q_ask <= q_bid:
        return None

    # Edge-or-width gate.
    fv_edge = abs(fair - (bid + ask) / 2.0)
    quote_width = q_ask - q_bid
    if max(fv_edge, quote_width) < float(cfg["mm_min_edge_or_width"]):
        return None

    return {
        "quote_bid": q_bid,
        "quote_ask": q_ask,
        "fair_value": round(fair, 6),
        "best_bid": round(bid, 6),
        "best_ask": round(ask, 6),
        "width": round(quote_width, 6),
        "skew": round(skew, 6),
        "inv_ratio": round(inv_ratio, 6),
    }


# --- Inventory --------------------------------------------------------------

def _empty_inventory(market_id: str, opp: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {
        "market_id": market_id,
        "token_id": str((opp or {}).get("token_id") or ""),
        "question": (opp or {}).get("question"),
        "slug": (opp or {}).get("slug"),
        "url": (opp or {}).get("url"),
        "shares": 0.0,
        "avg_cost": 0.0,
        "notional_usdc": 0.0,
        "mark_price": None,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    }


def _apply_buy(inv: Dict[str, Any], price: float, shares: float) -> None:
    """Update running average cost on a simulated BUY fill."""
    prev_shares = float(inv.get("shares") or 0.0)
    prev_cost = float(inv.get("avg_cost") or 0.0)
    new_shares = round(prev_shares + shares, 6)
    if new_shares <= 0:
        inv["shares"] = 0.0
        inv["avg_cost"] = 0.0
        inv["notional_usdc"] = 0.0
        return
    total_cost = prev_shares * prev_cost + shares * price
    inv["shares"] = new_shares
    inv["avg_cost"] = round(total_cost / new_shares, 6)
    inv["notional_usdc"] = round(new_shares * inv["avg_cost"], 4)


def _apply_sell(inv: Dict[str, Any], price: float, shares: float) -> float:
    """Reduce long YES inventory. Returns realized P&L for this fill."""
    prev_shares = float(inv.get("shares") or 0.0)
    prev_cost = float(inv.get("avg_cost") or 0.0)
    sellable = min(prev_shares, shares)
    if sellable <= 0:
        return 0.0
    realized = round(sellable * (price - prev_cost), 4)
    new_shares = round(prev_shares - sellable, 6)
    inv["shares"] = new_shares
    inv["realized_pnl"] = round(float(inv.get("realized_pnl") or 0.0) + realized, 4)
    if new_shares <= 0:
        inv["avg_cost"] = 0.0
        inv["notional_usdc"] = 0.0
    else:
        inv["notional_usdc"] = round(new_shares * prev_cost, 4)
    return realized


def _mark_inventory(inv: Dict[str, Any], opp: Optional[Mapping[str, Any]]) -> None:
    """Mark-to-market the inventory using the current best bid (conservative)."""
    if opp is None:
        # Keep previous mark; zero unrealized cannot be computed.
        return
    bid = _num(opp.get("best_bid"))
    mid = _num(opp.get("mid"))
    mark = bid if bid is not None and bid > 0 else mid
    shares = float(inv.get("shares") or 0.0)
    avg = float(inv.get("avg_cost") or 0.0)
    inv["mark_price"] = mark
    if shares <= 0 or mark is None:
        inv["unrealized_pnl"] = 0.0
    else:
        inv["unrealized_pnl"] = round(shares * (mark - avg), 4)
    # Also refresh descriptive fields if the caller kept older metadata.
    for key in ("question", "slug", "url", "token_id"):
        val = opp.get(key)
        if val is not None:
            inv[key] = val


# --- Quote lifecycle / fills ------------------------------------------------

def _quote_size_shares(cfg: Mapping[str, Any], price: float) -> float:
    notional = float(cfg["mm_quote_size_usdc"])
    if price <= 0 or price >= 1.0:
        return 0.0
    return round(notional / price, 6)


def _simulate_fill(
    quote: Mapping[str, Any],
    current: Mapping[str, Any],
    inv: Mapping[str, Any],
    cfg: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Deterministic, conservative fill simulation.

    - BUY (bid quote): fills if current best_ask <= quote_bid. Fill price
      is quote_bid (we posted at that price).
    - SELL (ask quote): fills if current best_bid >= quote_ask. Fill
      price is quote_ask. Only fills up to existing long shares.
    Returns a dict with fill shares/price or None.
    """
    side = quote.get("side")
    q_price = _num(quote.get("price"))
    if q_price is None:
        return None
    bid = _num(current.get("best_bid"))
    ask = _num(current.get("best_ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None

    if side == "BUY":
        if ask > q_price:
            return None
        shares = float(quote.get("shares") or 0.0)
        # Respect per-market and total inventory caps at fill time too.
        max_pos = float(cfg["mm_max_position_usdc_per_market"])
        existing_notional = float(inv.get("notional_usdc") or 0.0)
        if existing_notional + shares * q_price > max_pos + 0.01:
            available = max(0.0, max_pos - existing_notional)
            shares = round(available / q_price, 6) if q_price > 0 else 0.0
        if shares <= 0:
            return None
        return {"side": "BUY", "price": q_price, "shares": shares}

    if side == "SELL":
        if bid < q_price:
            return None
        inv_shares = float(inv.get("shares") or 0.0)
        shares = min(float(quote.get("shares") or 0.0), inv_shares)
        if shares <= 0:
            return None
        return {"side": "SELL", "price": q_price, "shares": round(shares, 6)}

    return None


def _is_stale(quote: Mapping[str, Any], now: datetime, cfg: Mapping[str, Any]) -> bool:
    ts = _parse_iso(quote.get("placed_at"))
    if ts is None:
        return True
    return (now - ts).total_seconds() > float(cfg["mm_cancel_if_stale_seconds"])


# --- Main tick --------------------------------------------------------------

def run_mm_tick(
    *,
    scanned_at: str,
    opportunities: List[Dict[str, Any]],
    quotes: List[Dict[str, Any]],
    inventory: List[Dict[str, Any]],
    config: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Advance the market-making portfolio one tick.

    Given the prior list of ACTIVE quotes and the current inventory per
    market, this function:

      1. Marks inventory to market against the latest book.
      2. Walks each active quote:
         - if stale -> mark EXPIRED
         - elif its market vanished -> mark CANCELLED
         - elif book crosses it -> mark FILLED and update inventory
         - else leave ACTIVE
      3. For each eligible market without an active quote on that side,
         if MM is enabled and risk caps permit, places new BUY/SELL quotes.
      4. Builds a summary.

    Returns:
        {
            "quotes":     list[quote dicts, all statuses, terminal quotes
                                retained for caller to persist to ledger],
            "fills":      list[new fill dicts generated this tick],
            "inventory":  list[inventory dicts, one per market with state],
            "summary":    aggregate metrics,
            "config":     the effective config used,
        }
    """
    ref = now or datetime.now(tz=timezone.utc)
    cfg = effective_config(config)

    opps_by_id: Dict[str, Dict[str, Any]] = {}
    for o in opportunities:
        mid = str(o.get("id") or o.get("slug") or "")
        if mid:
            opps_by_id[mid] = o

    # Inventory map keyed by market_id.
    inv_by_id: Dict[str, Dict[str, Any]] = {}
    for it in inventory:
        mid = str(it.get("market_id") or "")
        if mid:
            inv_by_id[mid] = dict(it)

    # 1) Mark existing inventory.
    for mid, inv in inv_by_id.items():
        _mark_inventory(inv, opps_by_id.get(mid))

    new_fills: List[Dict[str, Any]] = []
    terminal_quotes: List[Dict[str, Any]] = []
    active_quotes: List[Dict[str, Any]] = []
    quote_counter = 0

    # 2) Walk active quotes.
    for q in quotes:
        q = dict(q)
        if q.get("status") != "ACTIVE":
            # We don't re-process non-active quotes; caller persists ledger.
            terminal_quotes.append(q)
            continue
        mid = str(q.get("market_id") or "")
        current = opps_by_id.get(mid)
        if current is None:
            q["status"] = "CANCELLED"
            q["closed_at"] = _iso_now(ref)
            q["close_reason"] = "market_gone"
            terminal_quotes.append(q)
            continue
        if _is_stale(q, ref, cfg):
            q["status"] = "EXPIRED"
            q["closed_at"] = _iso_now(ref)
            q["close_reason"] = "stale"
            terminal_quotes.append(q)
            continue
        inv = inv_by_id.setdefault(mid, _empty_inventory(mid, current))
        fill = _simulate_fill(q, current, inv, cfg)
        if fill is None:
            active_quotes.append(q)
            continue
        # Apply fill.
        side = fill["side"]
        price = float(fill["price"])
        shares = float(fill["shares"])
        if side == "BUY":
            _apply_buy(inv, price, shares)
            fill_reason = "book_cross_bid"
        else:
            _apply_sell(inv, price, shares)
            fill_reason = "book_cross_ask"
        _mark_inventory(inv, current)
        q["status"] = "FILLED"
        q["filled_at"] = _iso_now(ref)
        q["fill_price"] = round(price, 6)
        q["fill_shares"] = round(shares, 6)
        q["fill_notional_usdc"] = round(price * shares, 4)
        q["close_reason"] = fill_reason
        terminal_quotes.append(q)
        new_fills.append(
            {
                "market_id": mid,
                "token_id": q.get("token_id"),
                "side": side,
                "price": round(price, 6),
                "shares": round(shares, 6),
                "notional_usdc": round(price * shares, 4),
                "ts": _iso_now(ref),
                "fill_reason": fill_reason,
                "quote_id": q.get("quote_id"),
                "question": q.get("question") or (current or {}).get("question"),
                "url": q.get("url") or (current or {}).get("url"),
            }
        )

    # 3) Generate new quotes if enabled.
    if cfg["market_making_enabled"]:
        # Enforce per-market and global caps before generating new quotes.
        active_by_market: Dict[str, Dict[str, Any]] = {}
        for q in active_quotes:
            mid = str(q.get("market_id") or "")
            active_by_market.setdefault(mid, {})[q.get("side")] = q

        active_market_count = len({str(q.get("market_id")) for q in active_quotes})
        total_inv_notional = sum(
            float(i.get("notional_usdc") or 0.0) for i in inv_by_id.values()
        )

        # Use ranked ordering (opportunities list is already ranked).
        for opp in opportunities:
            if active_market_count >= int(cfg["mm_max_markets"]):
                break
            mid = str(opp.get("id") or opp.get("slug") or "")
            if not mid:
                continue
            inv = inv_by_id.get(mid) or _empty_inventory(mid, opp)
            q_params = generate_quotes(
                opp, cfg, current_inventory_shares=float(inv.get("shares") or 0.0)
            )
            if q_params is None:
                continue
            existing_sides = active_by_market.get(mid, {})

            # BUY quote on bid side.
            inv_notional = float(inv.get("notional_usdc") or 0.0)
            max_pos = float(cfg["mm_max_position_usdc_per_market"])
            max_total = float(cfg["mm_max_total_inventory_usdc"])
            if (
                "BUY" not in existing_sides
                and inv_notional < max_pos - 0.01
                and total_inv_notional < max_total - 0.01
            ):
                shares = _quote_size_shares(cfg, q_params["quote_bid"])
                # Shrink to stay within caps.
                room_market = max(0.0, max_pos - inv_notional)
                room_total = max(0.0, max_total - total_inv_notional)
                cap_notional = min(
                    float(cfg["mm_quote_size_usdc"]), room_market, room_total
                )
                shares = round(cap_notional / q_params["quote_bid"], 6)
                if shares > 0:
                    quote_counter += 1
                    qid = f"{_iso_now(ref)}#{mid}#BUY#{quote_counter:03d}"
                    active_quotes.append(
                        {
                            "quote_id": qid,
                            "market_id": mid,
                            "token_id": str(opp.get("token_id") or ""),
                            "question": opp.get("question"),
                            "slug": opp.get("slug"),
                            "url": opp.get("url"),
                            "side": "BUY",
                            "price": q_params["quote_bid"],
                            "shares": shares,
                            "notional_usdc": round(
                                shares * q_params["quote_bid"], 4
                            ),
                            "fair_value": q_params["fair_value"],
                            "best_bid_at_placement": q_params["best_bid"],
                            "best_ask_at_placement": q_params["best_ask"],
                            "status": "ACTIVE",
                            "placed_at": _iso_now(ref),
                            "closed_at": None,
                            "close_reason": None,
                        }
                    )
                    existing_sides["BUY"] = active_quotes[-1]
                    active_by_market[mid] = existing_sides

            # SELL quote on ask side — only if we have inventory to reduce.
            inv_shares = float(inv.get("shares") or 0.0)
            if "SELL" not in existing_sides and inv_shares > 0:
                shares = min(
                    _quote_size_shares(cfg, q_params["quote_ask"]), inv_shares
                )
                if shares > 0:
                    quote_counter += 1
                    qid = f"{_iso_now(ref)}#{mid}#SELL#{quote_counter:03d}"
                    active_quotes.append(
                        {
                            "quote_id": qid,
                            "market_id": mid,
                            "token_id": str(opp.get("token_id") or ""),
                            "question": opp.get("question"),
                            "slug": opp.get("slug"),
                            "url": opp.get("url"),
                            "side": "SELL",
                            "price": q_params["quote_ask"],
                            "shares": round(shares, 6),
                            "notional_usdc": round(
                                shares * q_params["quote_ask"], 4
                            ),
                            "fair_value": q_params["fair_value"],
                            "best_bid_at_placement": q_params["best_bid"],
                            "best_ask_at_placement": q_params["best_ask"],
                            "status": "ACTIVE",
                            "placed_at": _iso_now(ref),
                            "closed_at": None,
                            "close_reason": None,
                        }
                    )
                    existing_sides["SELL"] = active_quotes[-1]
                    active_by_market[mid] = existing_sides

            if mid not in {str(q.get("market_id")) for q in active_quotes}:
                continue
            active_market_count = len(
                {str(q.get("market_id")) for q in active_quotes}
            )

    # 4) Build result.
    final_inventory = list(inv_by_id.values())
    summary = _build_summary(
        scanned_at=scanned_at,
        ref=ref,
        cfg=cfg,
        active_quotes=active_quotes,
        terminal_quotes_this_tick=[
            q for q in terminal_quotes if q.get("closed_at") == _iso_now(ref)
            or q.get("filled_at") == _iso_now(ref)
        ],
        new_fills=new_fills,
        inventory=final_inventory,
    )
    # Return everything the caller needs: active quotes (for persistence
    # as the new ACTIVE set) PLUS terminal quotes from this tick (for the
    # ledger).
    all_quotes = active_quotes + [q for q in terminal_quotes if q.get("status") != "ACTIVE"]

    return {
        "quotes": all_quotes,
        "fills": new_fills,
        "inventory": final_inventory,
        "summary": summary,
        "config": dict(cfg),
    }


def _build_summary(
    *,
    scanned_at: str,
    ref: datetime,
    cfg: Mapping[str, Any],
    active_quotes: List[Dict[str, Any]],
    terminal_quotes_this_tick: List[Dict[str, Any]],
    new_fills: List[Dict[str, Any]],
    inventory: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    inv_list = list(inventory)
    inv_notional = round(
        sum(float(i.get("notional_usdc") or 0.0) for i in inv_list), 4
    )
    unrealized = round(
        sum(float(i.get("unrealized_pnl") or 0.0) for i in inv_list), 4
    )
    realized = round(
        sum(float(i.get("realized_pnl") or 0.0) for i in inv_list), 4
    )
    filled_count = sum(1 for q in terminal_quotes_this_tick if q.get("status") == "FILLED")
    cancelled_count = sum(
        1 for q in terminal_quotes_this_tick if q.get("status") == "CANCELLED"
    )
    expired_count = sum(
        1 for q in terminal_quotes_this_tick if q.get("status") == "EXPIRED"
    )
    return {
        "scanned_at": scanned_at,
        "evaluated_at": _iso_now(ref),
        "enabled": bool(cfg["market_making_enabled"]),
        "active_quote_count": len(active_quotes),
        "filled_this_tick": filled_count,
        "cancelled_this_tick": cancelled_count,
        "expired_this_tick": expired_count,
        "fills_this_tick": len(new_fills),
        "inventory_markets": sum(
            1 for i in inv_list if float(i.get("shares") or 0.0) > 0
        ),
        "inventory_exposure_usdc": inv_notional,
        "unrealized_pnl_usdc": unrealized,
        "realized_pnl_usdc": realized,
        "total_pnl_usdc": round(unrealized + realized, 4),
    }
