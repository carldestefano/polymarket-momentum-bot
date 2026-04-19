from polymarket_momentum_bot.config import BotConfig
from polymarket_momentum_bot.trader import Trader


def test_dry_run_order_does_not_touch_network():
    cfg = BotConfig(dry_run=True)
    trader = Trader(cfg)
    result = trader.place_limit_order("tok1", "BUY", 10, 0.5)
    assert result.ok is True
    assert result.dry_run is True
    assert result.response == {"simulated": True, "ts": result.response["ts"]}


def test_invalid_side_rejected():
    trader = Trader(BotConfig(dry_run=True))
    result = trader.place_limit_order("tok1", "HODL", 1, 0.5)
    assert result.ok is False
    assert "invalid side" in (result.error or "")


def test_connect_noop_in_dry_run():
    trader = Trader(BotConfig(dry_run=True))
    trader.connect()  # should not raise, should not try to import SDK
