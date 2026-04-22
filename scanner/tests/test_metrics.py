from datetime import datetime, timezone

from polymarket_scanner.metrics import (
    build_opportunity,
    edge,
    fair_value_placeholder,
    mid_price,
    spread,
)


def test_mid_and_spread():
    assert abs(mid_price(0.4, 0.5) - 0.45) < 1e-9
    assert abs(spread(0.4, 0.5) - 0.1) < 1e-9
    assert mid_price(None, 0.5) is None
    assert spread(0.5, 0.4) is None  # ask < bid is invalid


def test_fair_value_placeholder_basic():
    # 1 year horizon, threshold well above spot -> FV well below 0.5.
    fv = fair_value_placeholder(
        threshold_usd=200_000,
        current_btc_price=100_000,
        seconds_remaining=365 * 86400,
        annualised_vol=0.6,
    )
    assert fv is not None
    assert 0.0 <= fv <= 0.5


def test_fair_value_placeholder_above_spot():
    # threshold far below spot -> FV close to 1.
    fv = fair_value_placeholder(
        threshold_usd=50_000,
        current_btc_price=100_000,
        seconds_remaining=7 * 86400,
    )
    assert fv is not None
    assert fv > 0.9


def test_fair_value_handles_missing():
    assert fair_value_placeholder(None, 100, 3600) is None
    assert fair_value_placeholder(100, None, 3600) is None
    assert fair_value_placeholder(100, 100, 0) is None


def test_edge_basic():
    assert edge(0.6, 0.5) == pytest_approx(0.1)
    assert edge(None, 0.5) is None
    assert edge(0.6, None) is None


def pytest_approx(v):  # tiny helper to avoid depending on pytest.approx here
    class _A:
        def __eq__(self, other):
            return abs(other - v) < 1e-9

    return _A()


def test_build_opportunity_integrates_fields():
    market = {
        "id": "abc",
        "slug": "btc-120k",
        "question": "Will Bitcoin hit $120,000 by 2026-12-31?",
        "endDate": "2026-12-31T23:59:00Z",
        "bestBid": "0.20",
        "bestAsk": "0.22",
        "lastTradePrice": "0.21",
        "volumeNum": 50000,
        "liquidityNum": 120000,
        "active": True,
        "closed": False,
    }
    now = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc)
    opp = build_opportunity(market, current_btc_price=90_000, now=now)
    assert opp["id"] == "abc"
    assert opp["url"] == "https://polymarket.com/event/btc-120k"
    assert opp["best_bid"] == 0.20
    assert opp["best_ask"] == 0.22
    assert abs(opp["mid"] - 0.21) < 1e-9
    assert round(opp["spread"], 4) == 0.02
    assert opp["threshold_usd"] == 120000.0
    assert opp["seconds_to_resolution"] > 0
    assert opp["fair_value"] is not None
    assert opp["edge"] is not None
