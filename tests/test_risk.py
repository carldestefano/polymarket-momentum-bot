import time

from polymarket_momentum_bot.config import BotConfig
from polymarket_momentum_bot.risk import PositionBook, RiskManager


def _cfg(**overrides) -> BotConfig:
    cfg = BotConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_kill_switch_blocks_all():
    risk = RiskManager(_cfg(kill_switch=True))
    ok, reason = risk.check_order("tok", "BUY", 1, 0.5)
    assert not ok
    assert "kill switch" in reason


def test_max_trade_size():
    risk = RiskManager(_cfg(max_trade_size_usdc=5))
    ok, reason = risk.check_order("tok", "BUY", 100, 0.5)
    assert not ok
    assert "max" in reason


def test_daily_spend_limit():
    risk = RiskManager(_cfg(max_daily_spend_usdc=10, max_trade_size_usdc=10))
    risk.book.record_fill("a", "BUY", 10, 0.6)  # 6 USDC spent
    ok, reason = risk.check_order("b", "BUY", 10, 0.5)  # + 5 = 11 > 10
    assert not ok
    assert "daily spend" in reason


def test_max_open_positions():
    cfg = _cfg(max_open_positions=2)
    risk = RiskManager(cfg)
    risk.book.record_fill("a", "BUY", 1, 0.5)
    risk.book.record_fill("b", "BUY", 1, 0.5)
    ok, reason = risk.check_order("c", "BUY", 1, 0.5)
    assert not ok
    assert "open positions" in reason


def test_cooldown():
    risk = RiskManager(_cfg(trade_cooldown_sec=300))
    risk.book.last_trade_at["tok"] = time.time()
    ok, reason = risk.check_order("tok", "BUY", 1, 0.5)
    assert not ok
    assert "cooldown" in reason


def test_market_gate_volume_liquidity_spread():
    risk = RiskManager(
        _cfg(min_volume_usdc=1000, min_liquidity_usdc=100, max_spread=0.05)
    )
    assert risk.market_is_tradeable(10, 500, 0.01)[0] is False
    assert risk.market_is_tradeable(5000, 10, 0.01)[0] is False
    assert risk.market_is_tradeable(5000, 500, 0.5)[0] is False
    assert risk.market_is_tradeable(5000, 500, 0.01)[0] is True


def test_position_book_averages():
    book = PositionBook()
    book.record_fill("t", "BUY", 10, 0.40)
    book.record_fill("t", "BUY", 10, 0.60)
    assert abs(book.positions["t"].avg_price - 0.50) < 1e-9
    assert book.positions["t"].size == 20
