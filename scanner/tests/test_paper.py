"""Unit tests for the Stage 2 paper trading engine.

These exercise the pure-Python tick function without any AWS/DynamoDB
dependency. Focus areas:

- gating: edge, liquidity, spread, resolution-horizon, ask sanity
- cooldown / dedup: no re-open while a fill is inside the cooldown window
- sizing: per-trade, per-market, and total-exposure caps
- marking: open positions mark to the current bid, not midpoint
- closing: edge flip, resolution reach, no-bid, market-closed, market-gone
- aggregates: open exposure, unrealized, realized, trade count, win rate
- config flag: paper_trading_enabled=False opens nothing but still marks

The tests pass the clock in explicitly via ``now=`` so cooldown math is
deterministic and does not depend on wall-clock time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_scanner.paper import (
    DEFAULT_CONFIG,
    effective_config,
    run_paper_tick,
)


T0 = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
T0_ISO = T0.isoformat().replace("+00:00", "Z")


def _opp(
    *,
    id="mkt-1",
    question="Will BTC cross $120k by May?",
    bid=0.40,
    ask=0.45,
    fair=0.70,
    liq=10_000.0,
    days_to_resolution=5,
    closed=False,
):
    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    edge = None
    if fair is not None and mid is not None:
        edge = fair - mid
    return {
        "id": id,
        "slug": id,
        "question": question,
        "url": f"https://polymarket.com/event/{id}",
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid,
        "fair_value": fair,
        "edge": edge,
        "liquidity_usd": liq,
        "seconds_to_resolution": int(days_to_resolution * 86400) if days_to_resolution is not None else None,
        "closed": closed,
    }


def _cfg(**kw):
    base = dict(DEFAULT_CONFIG)
    base["paper_trading_enabled"] = True
    base.update(kw)
    return base


# ---------------- effective_config ----------------


def test_effective_config_defaults_safe():
    c = effective_config(None)
    assert c["paper_trading_enabled"] is False  # safe default
    assert c["max_paper_trade_usdc"] == 100.0


def test_effective_config_accepts_bool_and_numeric_overrides():
    c = effective_config(
        {
            "paper_trading_enabled": 1,  # truthy -> True
            "max_paper_trade_usdc": "250",
            "min_edge_to_trade": 0.2,
            "ignored_key": "ignored",
        }
    )
    assert c["paper_trading_enabled"] is True
    assert c["max_paper_trade_usdc"] == 250.0
    assert c["min_edge_to_trade"] == 0.2
    assert "ignored_key" not in c


def test_effective_config_ignores_none_values():
    c = effective_config({"max_paper_trade_usdc": None})
    assert c["max_paper_trade_usdc"] == 100.0


# ---------------- disabled flag ----------------


def test_disabled_does_not_open_positions_but_does_mark():
    existing = [
        {
            "market_id": "mkt-existing",
            "status": "OPEN",
            "entry_price": 0.30,
            "shares": 100.0,
            "notional_usdc": 30.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-existing", bid=0.35, ask=0.38, fair=0.60)]
    # Config missing the flag -> defaults (disabled).
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=existing,
        now=T0,
    )
    assert result["summary"]["enabled"] is False
    assert result["summary"]["skipped_disabled"] is True
    assert result["summary"]["opened_this_tick"] == 0
    # Mark price updated to current bid.
    pos = [p for p in result["positions"] if p["market_id"] == "mkt-existing"][0]
    assert pos["mark_price"] == 0.35
    assert pos["unrealized_pnl"] == pytest.approx(100 * (0.35 - 0.30))


# ---------------- gates ----------------


@pytest.mark.parametrize(
    "kwargs,reason_tag",
    [
        ({"fair": 0.425, "bid": 0.40, "ask": 0.45}, "edge"),  # edge = 0 < 0.05
        ({"liq": 10.0}, "liquidity"),
        ({"ask": 0.95, "bid": 0.10}, "spread"),  # spread 0.85
        ({"days_to_resolution": 365}, "far_out"),
        ({"ask": 1.0}, "no_ask"),
        ({"bid": 0.0}, "no_bid"),
        ({"fair": None}, "no_fv"),
    ],
)
def test_gates_reject_unfit_opps(kwargs, reason_tag):
    opp = _opp(**kwargs)
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[opp],
        positions=[],
        config=_cfg(),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 0, reason_tag
    assert result["positions"] == [] or all(p["status"] == "CLOSED" for p in result["positions"])


def test_gate_passes_when_all_conditions_met():
    opp = _opp()  # default: bid 0.40 ask 0.45 fair 0.70 liq 10k 5d out
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[opp],
        positions=[],
        config=_cfg(),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 1
    pos = result["positions"][0]
    assert pos["status"] == "OPEN"
    assert pos["side"] == "BUY"
    assert pos["outcome"] == "YES"
    # Fill at ask + slippage, never at mid.
    assert pos["entry_price"] == pytest.approx(0.45 + DEFAULT_CONFIG["slippage_buffer"])


def test_short_horizon_only_flag():
    opp = _opp(days_to_resolution=30)
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[opp],
        positions=[],
        config=_cfg(allow_short_horizon_only=True, max_resolution_days=60),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 0


# ---------------- sizing ----------------


def test_sizing_respects_per_trade_cap():
    opp = _opp()
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[opp],
        positions=[],
        config=_cfg(max_paper_trade_usdc=50.0),
        now=T0,
    )
    pos = result["positions"][0]
    assert pos["notional_usdc"] <= 50.0
    # shares * fill_price == notional (within rounding)
    assert abs(pos["shares"] * pos["entry_price"] - pos["notional_usdc"]) < 0.01


def test_sizing_respects_total_exposure_cap():
    # Two opps, per-trade cap 100, total cap 120 -> second should shrink to 20.
    opps = [_opp(id="a"), _opp(id="b")]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=[],
        config=_cfg(
            max_paper_trade_usdc=100.0,
            max_paper_position_usdc_per_market=100.0,
            max_total_paper_exposure_usdc=120.0,
        ),
        now=T0,
    )
    opens = [p for p in result["positions"] if p["status"] == "OPEN"]
    assert len(opens) == 2
    total = sum(p["notional_usdc"] for p in opens)
    assert total <= 120.0 + 0.01


def test_does_not_scale_in_to_existing_open():
    opp = _opp(id="mkt-1")
    existing = [
        {
            "market_id": "mkt-1",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 50.0,
            "notional_usdc": 20.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[opp],
        positions=existing,
        config=_cfg(),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 0


# ---------------- cooldown ----------------


def test_cooldown_prevents_rapid_reopen():
    recent = [
        {
            "market_id": "mkt-1",
            "side": "SELL",
            "ts": (T0 - timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
        }
    ]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[_opp(id="mkt-1")],
        positions=[],
        recent_fills=recent,
        config=_cfg(cooldown_seconds=600),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 0


def test_cooldown_passes_after_window_elapses():
    recent = [
        {
            "market_id": "mkt-1",
            "side": "SELL",
            "ts": (T0 - timedelta(seconds=601)).isoformat().replace("+00:00", "Z"),
        }
    ]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[_opp(id="mkt-1")],
        positions=[],
        recent_fills=recent,
        config=_cfg(cooldown_seconds=600),
        now=T0,
    )
    assert result["summary"]["opened_this_tick"] == 1


# ---------------- marking & closing ----------------


def test_mark_uses_current_bid_not_mid():
    existing = [
        {
            "market_id": "mkt-1",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 100.0,
            "notional_usdc": 40.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.50, ask=0.60, fair=0.80)]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=existing,
        config=_cfg(),
        now=T0,
    )
    pos = [p for p in result["positions"] if p["market_id"] == "mkt-1"][0]
    assert pos["mark_price"] == 0.50  # bid, not mid (0.55)
    assert pos["unrealized_pnl"] == pytest.approx(100 * (0.50 - 0.40))


def test_close_on_edge_flip():
    existing = [
        {
            "market_id": "mkt-1",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 100.0,
            "notional_usdc": 40.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    # fair < mid so edge flips negative.
    opps = [_opp(id="mkt-1", bid=0.55, ask=0.60, fair=0.50)]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=existing,
        config=_cfg(close_on_edge_flip=True),
        now=T0,
    )
    closed = [p for p in result["positions"] if p["status"] == "CLOSED"]
    assert len(closed) == 1
    p = closed[0]
    assert p["close_reason"] == "edge_flipped"
    # Realized at current bid.
    assert p["exit_price"] == 0.55
    assert p["realized_pnl"] == pytest.approx(100 * (0.55 - 0.40))
    # A matching SELL fill was emitted.
    sells = [f for f in result["fills"] if f["side"] == "SELL"]
    assert len(sells) == 1


def test_close_on_resolution_reached():
    existing = [
        {
            "market_id": "mkt-1",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 100.0,
            "notional_usdc": 40.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    opps = [_opp(id="mkt-1", bid=0.50, ask=0.52, fair=0.80, days_to_resolution=0)]
    opps[0]["seconds_to_resolution"] = -10
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=existing,
        config=_cfg(),
        now=T0,
    )
    closed = [p for p in result["positions"] if p["status"] == "CLOSED"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "resolution_reached"


def test_close_when_market_gone():
    existing = [
        {
            "market_id": "mkt-gone",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 100.0,
            "notional_usdc": 40.0,
            "mark_price": 0.42,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        }
    ]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=[],  # market disappeared from scan
        positions=existing,
        config=_cfg(),
        now=T0,
    )
    closed = [p for p in result["positions"] if p["status"] == "CLOSED"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "market_gone"
    # Exit falls back to last mark when no fresh quote is available.
    assert closed[0]["exit_price"] == 0.42


# ---------------- aggregates ----------------


def test_summary_aggregates_open_and_closed():
    existing = [
        {
            "market_id": "winner",
            "status": "OPEN",
            "entry_price": 0.40,
            "shares": 100.0,
            "notional_usdc": 40.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        },
        {
            "market_id": "loser",
            "status": "OPEN",
            "entry_price": 0.60,
            "shares": 50.0,
            "notional_usdc": 30.0,
            "opened_at": T0_ISO,
            "updated_at": T0_ISO,
        },
    ]
    # winner marks higher; loser edge flips and closes at a lower bid.
    opps = [
        _opp(id="winner", bid=0.55, ask=0.60, fair=0.80),
        _opp(id="loser", bid=0.50, ask=0.55, fair=0.40),
    ]
    result = run_paper_tick(
        scanned_at=T0_ISO,
        opportunities=opps,
        positions=existing,
        config=_cfg(close_on_edge_flip=True),
        now=T0,
    )
    s = result["summary"]
    assert s["open_count"] == 1
    assert s["closed_count"] == 1
    assert s["unrealized_pnl_usdc"] == pytest.approx(100 * (0.55 - 0.40))
    assert s["realized_pnl_usdc"] == pytest.approx(50 * (0.50 - 0.60))
    assert s["total_pnl_usdc"] == pytest.approx(
        s["unrealized_pnl_usdc"] + s["realized_pnl_usdc"]
    )
    assert s["trade_count"] == 1
    # winner still open (no realized), loser realized negative -> 0 wins, 1 loss.
    assert s["win_rate"] == 0.0


def test_deterministic_same_inputs_same_outputs():
    opp = _opp()
    r1 = run_paper_tick(
        scanned_at=T0_ISO, opportunities=[opp], positions=[], config=_cfg(), now=T0
    )
    r2 = run_paper_tick(
        scanned_at=T0_ISO, opportunities=[opp], positions=[], config=_cfg(), now=T0
    )
    # Drop timestamps that could tie to wall clock (there shouldn't be any
    # since now is fixed, but be explicit).
    assert r1["positions"][0]["entry_price"] == r2["positions"][0]["entry_price"]
    assert r1["summary"]["open_exposure_usdc"] == r2["summary"]["open_exposure_usdc"]
