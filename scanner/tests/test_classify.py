from polymarket_scanner.classify import (
    filter_btc,
    is_btc_market,
    is_short_horizon,
)


def test_btc_question_is_classified():
    m = {"question": "Will Bitcoin hit $120,000 by Friday?", "slug": "btc-120k"}
    assert is_btc_market(m)


def test_eth_flippening_is_not_btc():
    m = {"question": "Will Ethereum flip Bitcoin by 2026?", "slug": "eth-flip-btc"}
    assert not is_btc_market(m)


def test_solana_is_not_btc():
    m = {"question": "Will Solana hit $500 by year end?", "slug": "sol-500"}
    assert not is_btc_market(m)


def test_non_crypto_is_not_btc():
    m = {"question": "Will the Fed cut rates in May?", "slug": "fed-may"}
    assert not is_btc_market(m)


def test_btc_tag_match():
    m = {"question": "Price by end of week", "tags": ["bitcoin"]}
    assert is_btc_market(m)


def test_short_horizon_detection():
    m = {"question": "BTC above $100k by Friday?"}
    assert is_short_horizon(m)


def test_short_horizon_false_when_long():
    m = {"question": "Will BTC reach $1M by 2030?"}
    assert not is_short_horizon(m)


def test_filter_btc_mixed_list():
    markets = [
        {"question": "Will Bitcoin hit $120,000?"},
        {"question": "Will Ethereum flip Bitcoin?"},
        {"question": "Will the Fed cut?"},
        {"question": "BTC above 100k this week", "tags": []},
    ]
    out = filter_btc(markets)
    assert len(out) == 2
    assert all("bitcoin" in (m.get("question") or "").lower() or "btc" in (m.get("question") or "").lower() for m in out)
