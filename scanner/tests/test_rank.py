from polymarket_scanner.rank import rank_opportunities, score_opportunity


def _opp(**overrides):
    base = {
        "id": "x",
        "question": "Will BTC hit 100k by Friday?",
        "best_bid": 0.4,
        "best_ask": 0.5,
        "mid": 0.45,
        "spread": 0.1,
        "fair_value": 0.6,
        "edge": 0.15,
        "volume_usd": 10_000,
        "liquidity_usd": 50_000,
        "seconds_to_resolution": 3 * 86400,
    }
    base.update(overrides)
    return base


def test_score_rewards_edge_and_liquidity():
    high = score_opportunity(_opp(edge=0.30, liquidity_usd=500_000))
    low = score_opportunity(_opp(edge=0.01, liquidity_usd=1_000))
    assert high > low


def test_score_penalises_wide_spread():
    tight = score_opportunity(_opp(spread=0.01))
    wide = score_opportunity(_opp(spread=0.30))
    assert tight > wide


def test_rank_sorts_desc_and_limits():
    opps = [
        _opp(id="a", edge=0.05),
        _opp(id="b", edge=0.25),
        _opp(id="c", edge=0.10),
    ]
    ranked = rank_opportunities(opps, limit=2)
    assert [o["id"] for o in ranked] == ["b", "c"]
    assert all("score" in o for o in ranked)


def test_rank_handles_missing_fields():
    opps = [
        {"id": "a"},
        {"id": "b", "liquidity_usd": 1000},
    ]
    ranked = rank_opportunities(opps)
    assert len(ranked) == 2
    assert all("score" in o for o in ranked)
