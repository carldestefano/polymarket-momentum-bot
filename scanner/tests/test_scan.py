"""Offline test of run_scan by monkey-patching the HTTP fetchers."""

from datetime import datetime, timezone

from polymarket_scanner import polymarket, scan


def test_run_scan_offline(monkeypatch):
    fake_markets = [
        {
            "id": "1",
            "slug": "btc-100k-friday",
            "question": "Will Bitcoin hit $100,000 by Friday?",
            "endDate": "2026-04-25T00:00:00Z",
            "bestBid": "0.31",
            "bestAsk": "0.33",
            "lastTradePrice": "0.32",
            "volumeNum": 50_000,
            "liquidityNum": 200_000,
            "active": True,
            "closed": False,
        },
        {
            "id": "2",
            "slug": "eth-5k",
            "question": "Will Ethereum hit $5,000 by 2026-12-31?",
            "endDate": "2026-12-31T23:59:00Z",
            "bestBid": "0.10",
            "bestAsk": "0.12",
            "volumeNum": 10_000,
            "liquidityNum": 25_000,
        },
        {
            "id": "3",
            "slug": "btc-120k-dec",
            "question": "Will Bitcoin hit $120,000 by 2026-12-31?",
            "endDate": "2026-12-31T23:59:00Z",
            "bestBid": "0.20",
            "bestAsk": "0.24",
            "lastTradePrice": "0.22",
            "volumeNum": 30_000,
            "liquidityNum": 75_000,
        },
    ]

    monkeypatch.setattr(scan, "fetch_active_markets", lambda limit=500: fake_markets)
    monkeypatch.setattr(scan, "fetch_btc_spot_price", lambda: 95_000.0)

    now = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc)
    result = scan.run_scan(market_limit=500, top_n=10, now=now)

    assert result["total_markets"] == 3
    assert result["btc_markets"] == 2
    assert result["btc_price_usd"] == 95_000.0
    assert result["scanned_at"].endswith("Z")
    ids = {o["id"] for o in result["opportunities"]}
    assert ids == {"1", "3"}
    for o in result["opportunities"]:
        assert "score" in o
        assert "fair_value" in o
        assert "edge" in o
