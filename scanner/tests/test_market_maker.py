"""Unit tests for the Stage 3 market-making simulator.

These exercise the pure-Python tick function. Focus areas:

- quote generation: clamping, maker-only adjustment, inventory skew
- risk gates: spread, liquidity, resolution horizon, missing fair value
- lifecycle: stale -> EXPIRED, vanished -> CANCELLED, book cross -> FILLED
- fills: BUY adds inventory with running avg cost, SELL reduces and
  realizes P&L, SELL never creates negative inventory
- P&L: mark-to-bid; unrealized and realized sum into total
- risk caps: per-market and global notional caps honored at place time
  and at fill time
- disabled flag: still marks inventory but places no new quotes

Tests pass the clock via ``now=`` so staleness is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_scanner.market_maker import (
    DEFAULT_CONFIG,
    effective_config,
    generate_quotes,
    run_mm_tick,
)


T0 = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
T0_ISO = T0.isoformat().replace("+00:00", "Z")


def _opp(
    *,
    id="mkt-1",
    question="Will BTC cross $120k by May?",
    bid=0.40,
    ask=0.50,
    fair=0.48,
    liq=10_000.0,
    days_to_resolution=5,
    closed=False,
):
    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    return {
        "id": id,
        "slug": id,
        "question": question,
        "url": f"https://polymarket.com/event/{id}",
        "token_id": f"tok-{id}",
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid,
        "fair_value": fair,
        "liquidity_usd": liq,
        "seconds_to_resolution": int(days_to_resolution * 86400)
        if days_to_resolution is not None
        else None,
        "closed": closed,
    }


def _cfg(**kw):
    base = dict(DEFAULT_CONFIG)
    base["market_making_enabled"] = True
    base.update(kw)
    return base


# ---------------- effective_config ----------------


def test_effective_config_defaults_safe():
    c = effective_config(None)
    assert c["market_making_enabled"] is False  # safe default
    assert c["mm_quote_size_usdc"] == 50.0


def test_effective_config_accepts_typed_overrides():
    c = effective_config(
        {
            "market_making_enabled": 1,
            "mm_quote_size_usdc": "75",
            "mm_max_markets": "3",
            "unknown_key": "x",
        }
    )
    assert c["market_making_enabled"] is True
    assert c["mm_quote_size_usdc"] == 75.0
    assert c["mm_max_markets"] == 3
    assert "unknown_key" not in c


def test_effective_config_ignores_none():
    c = effective_config({"mm_quote_size_usdc": None})
    assert c["mm_quote_size_usdc"] == 50.0


# ---------------- generate_quotes ----------------


def test_generate_quotes_centers_around_fair_when_flat():
    opp = _opp(bid=0.40, ask=0.50, fair=0.48)
    q = generate_quotes(opp, _cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0))
    assert q is not None
    # width=0.06 -> bid=0.45, ask=0.51 — but ask must be clamped away from
    # crossing: ask-0.005 == 0.495, so bid becomes 0.495 (worse for us but
    # keeps maker-only invariant).
    assert q["quote_bid"] <= q["quote_ask"]
    assert q["quote_ask"] >= q["best_bid"] + 0.005 - 1e-9


def test_generate_quotes_skews_when_long():
    # Long inventory -> both quotes move DOWN.
    # Use a wide book so the maker-only clamp isn't what's being tested.
    opp = _opp(bid=0.20, ask=0.80, fair=0.50)
    flat = generate_quotes(
        opp,
        _cfg(mm_base_quote_width=0.06, mm_inventory_skew_factor=0.5,
             mm_min_edge_or_width=0.0, mm_max_spread=1.0),
        current_inventory_shares=0.0,
    )
    long = generate_quotes(
        opp,
        _cfg(mm_base_quote_width=0.06, mm_inventory_skew_factor=0.5,
             mm_min_edge_or_width=0.0, mm_max_position_usdc_per_market=100.0,
             mm_max_spread=1.0),
        current_inventory_shares=500.0,  # "full" long relative to max_pos
    )
    assert flat is not None and long is not None
    assert long["quote_bid"] < flat["quote_bid"]
    assert long["quote_ask"] < flat["quote_ask"]


def test_generate_quotes_rejects_wide_spread():
    opp = _opp(bid=0.10, ask=0.90, fair=0.50)
    q = generate_quotes(opp, _cfg(mm_max_spread=0.15))
    assert q is None


def test_generate_quotes_rejects_near_resolution():
    opp = _opp(days_to_resolution=None)
    opp["seconds_to_resolution"] = 60  # 1 minute
    q = generate_quotes(opp, _cfg(mm_avoid_near_resolution_seconds=3600))
    assert q is None


def test_generate_quotes_rejects_low_liquidity():
    opp = _opp(liq=10.0)
    q = generate_quotes(opp, _cfg(mm_min_liquidity_usdc=500.0))
    assert q is None


def test_generate_quotes_rejects_no_fair_value():
    opp = _opp(fair=None)
    q = generate_quotes(opp, _cfg())
    assert q is None


def test_generate_quotes_enforces_min_edge_or_width():
    # fair == mid (no edge), tiny width -> rejected.
    opp = _opp(bid=0.49, ask=0.51, fair=0.50)
    q = generate_quotes(
        opp,
        _cfg(mm_base_quote_width=0.001, mm_min_edge_or_width=0.05),
    )
    assert q is None


def test_generate_quotes_clamps_to_valid_prices():
    opp = _opp(bid=0.95, ask=0.98, fair=1.20)  # fair nonsensical > 1
    q = generate_quotes(opp, _cfg(mm_base_quote_width=0.10))
    # Either clamped quotes are ordered or the quote was rejected.
    if q is not None:
        assert 0.01 <= q["quote_bid"] <= 0.99
        assert 0.01 <= q["quote_ask"] <= 0.99


# ---------------- disabled flag ----------------


def test_disabled_does_not_quote_but_still_marks_inventory():
    inv = [
        {
            "market_id": "mkt-1",
            "shares": 100.0,
            "avg_cost": 0.40,
            "notional_usdc": 40.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.45, ask=0.50, fair=0.60)]
    # No config => market_making_enabled defaults to False.
    res = run_mm_tick(
        scanned_at=T0_ISO, opportunities=opps, quotes=[], inventory=inv, now=T0,
    )
    assert res["summary"]["enabled"] is False
    # No new quotes placed.
    active = [q for q in res["quotes"] if q.get("status") == "ACTIVE"]
    assert active == []
    # Inventory marked to bid.
    i = [x for x in res["inventory"] if x["market_id"] == "mkt-1"][0]
    assert i["mark_price"] == 0.45
    assert i["unrealized_pnl"] == pytest.approx(100 * (0.45 - 0.40))


# ---------------- lifecycle: stale / vanished ----------------


def test_stale_quote_expires():
    old = (T0 - timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    quotes = [
        {
            "quote_id": "qid-1",
            "market_id": "mkt-1",
            "side": "BUY",
            "price": 0.40,
            "shares": 100.0,
            "status": "ACTIVE",
            "placed_at": old,
        }
    ]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=[_opp(id="mkt-1")],
        quotes=quotes,
        inventory=[],
        config=_cfg(mm_cancel_if_stale_seconds=300),
        now=T0,
    )
    expired = [q for q in res["quotes"] if q["status"] == "EXPIRED"]
    assert len(expired) == 1
    assert expired[0]["close_reason"] == "stale"


def test_vanished_market_cancels_quote():
    quotes = [
        {
            "quote_id": "qid-1",
            "market_id": "mkt-gone",
            "side": "BUY",
            "price": 0.40,
            "shares": 100.0,
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=[],  # market is gone
        quotes=quotes,
        inventory=[],
        config=_cfg(),
        now=T0,
    )
    cancelled = [q for q in res["quotes"] if q["status"] == "CANCELLED"]
    assert len(cancelled) == 1
    assert cancelled[0]["close_reason"] == "market_gone"


# ---------------- fills: BUY / SELL ----------------


def test_buy_quote_fills_when_book_crosses_it():
    # BUY quote at 0.45 fills if best_ask <= 0.45.
    quotes = [
        {
            "quote_id": "qid-1",
            "market_id": "mkt-1",
            "side": "BUY",
            "price": 0.45,
            "shares": 100.0,
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.42, ask=0.44, fair=0.50)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=quotes,
        inventory=[],
        config=_cfg(mm_max_position_usdc_per_market=10_000.0),
        now=T0,
    )
    filled = [q for q in res["quotes"] if q["status"] == "FILLED"]
    assert len(filled) == 1
    assert filled[0]["fill_price"] == 0.45
    assert filled[0]["fill_shares"] == 100.0
    # Inventory built up.
    i = [x for x in res["inventory"] if x["market_id"] == "mkt-1"][0]
    assert i["shares"] == 100.0
    assert i["avg_cost"] == pytest.approx(0.45)


def test_buy_quote_does_not_fill_when_ask_above_bid_price():
    quotes = [
        {
            "quote_id": "qid-1",
            "market_id": "mkt-1",
            "side": "BUY",
            "price": 0.40,
            "shares": 100.0,
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.42, ask=0.48, fair=0.50)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=quotes,
        inventory=[],
        config=_cfg(),
        now=T0,
    )
    active = [q for q in res["quotes"] if q["status"] == "ACTIVE"]
    # The previous quote should remain ACTIVE; no fill.
    assert any(q["quote_id"] == "qid-1" for q in active)


def test_sell_quote_fills_and_realizes_pnl():
    quotes = [
        {
            "quote_id": "qid-sell-1",
            "market_id": "mkt-1",
            "side": "SELL",
            "price": 0.50,
            "shares": 50.0,
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    inv = [
        {
            "market_id": "mkt-1",
            "shares": 100.0,
            "avg_cost": 0.40,
            "notional_usdc": 40.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.52, ask=0.55, fair=0.54)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=quotes,
        inventory=inv,
        config=_cfg(),
        now=T0,
    )
    filled = [q for q in res["quotes"] if q["status"] == "FILLED"]
    assert len(filled) == 1
    i = [x for x in res["inventory"] if x["market_id"] == "mkt-1"][0]
    assert i["shares"] == 50.0  # 100 - 50
    assert i["realized_pnl"] == pytest.approx(50 * (0.50 - 0.40))


def test_sell_never_oversells_inventory():
    quotes = [
        {
            "quote_id": "qid-sell-1",
            "market_id": "mkt-1",
            "side": "SELL",
            "price": 0.50,
            "shares": 500.0,  # more than we own
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    inv = [
        {
            "market_id": "mkt-1",
            "shares": 100.0,
            "avg_cost": 0.40,
            "notional_usdc": 40.0,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.55, ask=0.58)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=quotes,
        inventory=inv,
        config=_cfg(),
        now=T0,
    )
    i = [x for x in res["inventory"] if x["market_id"] == "mkt-1"][0]
    # Must not go negative.
    assert i["shares"] == 0.0
    filled = [q for q in res["quotes"] if q["status"] == "FILLED"]
    assert filled[0]["fill_shares"] == 100.0  # clamped


def test_sell_without_inventory_does_not_fill():
    # Stage 3 is YES-long only. A SELL quote without inventory cannot fill.
    quotes = [
        {
            "quote_id": "qid-sell-1",
            "market_id": "mkt-1",
            "side": "SELL",
            "price": 0.50,
            "shares": 100.0,
            "status": "ACTIVE",
            "placed_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.55, ask=0.58)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=quotes,
        inventory=[],
        config=_cfg(),
        now=T0,
    )
    active = [q for q in res["quotes"] if q["status"] == "ACTIVE"]
    assert any(q["quote_id"] == "qid-sell-1" for q in active)


# ---------------- quote placement ----------------


def test_enabled_places_buy_quote_when_eligible():
    opps = [_opp(id="mkt-1", bid=0.40, ask=0.45, fair=0.50)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=[],
        config=_cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0),
        now=T0,
    )
    new_quotes = [q for q in res["quotes"] if q.get("status") == "ACTIVE"]
    assert any(q["side"] == "BUY" for q in new_quotes)


def test_enabled_places_sell_quote_only_when_has_inventory():
    opps = [_opp(id="mkt-1", bid=0.40, ask=0.45, fair=0.50)]
    res_no_inv = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=[],
        config=_cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0),
        now=T0,
    )
    sells_no_inv = [q for q in res_no_inv["quotes"] if q["side"] == "SELL"]
    assert sells_no_inv == []

    inv = [
        {
            "market_id": "mkt-1",
            "shares": 100.0,
            "avg_cost": 0.40,
            "notional_usdc": 40.0,
        }
    ]
    res_with_inv = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=inv,
        config=_cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0),
        now=T0,
    )
    sells = [q for q in res_with_inv["quotes"] if q["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["price"] > 0.40  # sell price above our avg cost? not required; just above bid
    assert sells[0]["shares"] <= 100.0


def test_mm_max_markets_cap_respected():
    opps = [
        _opp(id=f"mkt-{i}", bid=0.40, ask=0.45, fair=0.50) for i in range(10)
    ]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=[],
        config=_cfg(
            mm_max_markets=3, mm_base_quote_width=0.06, mm_min_edge_or_width=0.0
        ),
        now=T0,
    )
    active = [q for q in res["quotes"] if q.get("status") == "ACTIVE"]
    markets = {q["market_id"] for q in active}
    assert len(markets) <= 3


def test_global_inventory_cap_halts_new_buys():
    # Already at global inventory limit -> no new BUY quotes.
    inv = [
        {
            "market_id": "mkt-a",
            "shares": 1000.0,
            "avg_cost": 1.0,
            "notional_usdc": 1000.0,
        }
    ]
    opps = [_opp(id="mkt-b", bid=0.40, ask=0.45, fair=0.50)]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=inv,
        config=_cfg(
            mm_max_total_inventory_usdc=1000.0,
            mm_base_quote_width=0.06,
            mm_min_edge_or_width=0.0,
        ),
        now=T0,
    )
    new_buys = [
        q for q in res["quotes"]
        if q.get("status") == "ACTIVE" and q.get("side") == "BUY" and q.get("market_id") == "mkt-b"
    ]
    assert new_buys == []


# ---------------- aggregates ----------------


def test_summary_sums_pnl_correctly():
    inv = [
        {
            "market_id": "winner",
            "shares": 100.0,
            "avg_cost": 0.40,
            "notional_usdc": 40.0,
            "realized_pnl": 5.0,
            "unrealized_pnl": 0.0,
        },
        {
            "market_id": "loser",
            "shares": 50.0,
            "avg_cost": 0.60,
            "notional_usdc": 30.0,
            "realized_pnl": -2.0,
            "unrealized_pnl": 0.0,
        },
    ]
    opps = [
        _opp(id="winner", bid=0.55, ask=0.60, fair=0.70),
        _opp(id="loser", bid=0.50, ask=0.55, fair=0.60),
    ]
    res = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=inv,
        config=_cfg(),
        now=T0,
    )
    s = res["summary"]
    assert s["unrealized_pnl_usdc"] == pytest.approx(
        100 * (0.55 - 0.40) + 50 * (0.50 - 0.60)
    )
    assert s["realized_pnl_usdc"] == pytest.approx(5.0 + -2.0)
    assert s["total_pnl_usdc"] == pytest.approx(
        s["unrealized_pnl_usdc"] + s["realized_pnl_usdc"]
    )
    assert s["inventory_exposure_usdc"] == pytest.approx(40 + 30)


def test_deterministic_same_inputs_same_outputs():
    opps = [_opp(id="mkt-1")]
    r1 = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=[],
        config=_cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0),
        now=T0,
    )
    r2 = run_mm_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        quotes=[],
        inventory=[],
        config=_cfg(mm_base_quote_width=0.06, mm_min_edge_or_width=0.0),
        now=T0,
    )
    # Quote prices are deterministic given the same config and opportunity.
    a1 = sorted(
        (q["side"], q["price"]) for q in r1["quotes"] if q.get("status") == "ACTIVE"
    )
    a2 = sorted(
        (q["side"], q["price"]) for q in r2["quotes"] if q.get("status") == "ACTIVE"
    )
    assert a1 == a2
