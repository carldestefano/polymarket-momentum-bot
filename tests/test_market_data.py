from unittest.mock import MagicMock

from polymarket_momentum_bot.market_data import MarketDataClient, extract_token_ids


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_extract_token_ids_from_json_string():
    assert extract_token_ids({"clobTokenIds": '["123","456"]'}) == ["123", "456"]


def test_extract_token_ids_from_list():
    assert extract_token_ids({"clobTokenIds": [1, 2]}) == ["1", "2"]


def test_extract_token_ids_missing():
    assert extract_token_ids({}) == []


def test_price_history_parses_points():
    session = MagicMock()
    session.get.return_value = _Resp(
        {"history": [{"t": 1, "p": "0.45"}, {"t": 2, "p": 0.47}]}
    )
    client = MarketDataClient(session=session)
    out = client.price_history("tok", interval="1h", fidelity=60)
    assert out == [{"t": 1, "p": 0.45}, {"t": 2, "p": 0.47}]
    args, kwargs = session.get.call_args
    assert "prices-history" in args[0]
    assert kwargs["params"]["market"] == "tok"
    assert kwargs["params"]["interval"] == "1h"
    assert kwargs["params"]["fidelity"] == 60


def test_midpoint_returns_float():
    session = MagicMock()
    session.get.return_value = _Resp({"mid": "0.5"})
    client = MarketDataClient(session=session)
    assert client.midpoint("tok") == 0.5


def test_list_active_markets_sorted_and_filtered():
    session = MagicMock()
    session.get.return_value = _Resp(
        [
            {
                "active": True,
                "closed": False,
                "category": "politics",
                "tags": ["politics"],
                "markets": [
                    {"active": True, "closed": False, "volume24hr": 100, "question": "low"},
                    {"active": False, "closed": False, "volume24hr": 9999, "question": "inactive"},
                ],
            },
            {
                "active": True,
                "closed": False,
                "category": "sports",
                "tags": ["sports"],
                "markets": [
                    {"active": True, "closed": False, "volume24hr": 500, "question": "high"}
                ],
            },
        ]
    )
    client = MarketDataClient(session=session)
    # Allowlist only "politics" — the sports event drops, so only "low" remains.
    markets = client.list_active_markets(max_markets=10, allowed_categories=["politics"])
    assert [m["question"] for m in markets] == ["low"]
