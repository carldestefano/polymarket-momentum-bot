from datetime import datetime, timezone

from polymarket_scanner.parse import (
    best_bid_ask,
    extract_price_threshold,
    last_price,
    liquidity_usd,
    market_url,
    parse_end_date,
    safe_float,
    seconds_to_resolution,
    volume_usd,
)


def test_safe_float_handles_bad_input():
    assert safe_float(None) is None
    assert safe_float("not a number") is None
    assert safe_float("1.5") == 1.5
    assert safe_float(3) == 3.0


def test_extract_threshold_dollar_comma():
    assert extract_price_threshold("Will Bitcoin hit $120,000 by Friday?") == 120000.0


def test_extract_threshold_k_suffix():
    assert extract_price_threshold("BTC above 95k on April 30?") == 95000.0


def test_extract_threshold_m_suffix():
    assert extract_price_threshold("Bitcoin price > 1.2m in 2030") == 1200000.0


def test_extract_threshold_rejects_tiny_numbers():
    # "$4" is almost certainly not a BTC threshold -> None.
    assert extract_price_threshold("Will this cost $4 in 2030?") is None


def test_extract_threshold_handles_missing():
    assert extract_price_threshold(None) is None
    assert extract_price_threshold("") is None
    assert extract_price_threshold("No price mentioned here") is None


def test_parse_end_date_iso_z():
    m = {"endDate": "2026-05-01T12:00:00Z"}
    dt = parse_end_date(m)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 5


def test_seconds_to_resolution():
    m = {"endDate": "2026-04-22T01:00:00Z"}
    now = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc)
    assert seconds_to_resolution(m, now=now) == 3600


def test_best_bid_ask_and_last():
    m = {"bestBid": "0.42", "bestAsk": 0.45, "lastTradePrice": 0.43}
    assert best_bid_ask(m) == (0.42, 0.45)
    assert last_price(m) == 0.43


def test_last_price_from_outcome_prices_json_string():
    m = {"outcomePrices": '["0.67", "0.33"]'}
    assert last_price(m) == 0.67


def test_volume_liquidity():
    m = {"volumeNum": 12345, "liquidityNum": 67890}
    assert volume_usd(m) == 12345.0
    assert liquidity_usd(m) == 67890.0


def test_market_url_prefers_slug():
    assert market_url({"slug": "btc-120k", "id": "abc"}) == "https://polymarket.com/event/btc-120k"
    assert market_url({"id": "abc"}) == "https://polymarket.com/market/abc"
    assert market_url({}) is None
