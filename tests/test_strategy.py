from polymarket_momentum_bot.strategy import Signal, evaluate, moving_average


def test_moving_average_basic():
    assert moving_average([1, 2, 3, 4, 5], 5) == 3
    assert moving_average([1, 2, 3], 5) is None


def _hist(prices):
    return [{"t": i, "p": p} for i, p in enumerate(prices)]


def test_buy_on_cross_above():
    # Flat below the MA, then a clear cross above.
    prices = [0.45] * 20 + [0.55]
    result = evaluate(_hist(prices), window=20, currently_long=False)
    assert result.signal is Signal.BUY
    assert result.last_price == 0.55
    assert result.moving_average is not None


def test_sell_on_cross_below():
    prices = [0.60] * 20 + [0.40]
    result = evaluate(_hist(prices), window=20, currently_long=True)
    assert result.signal is Signal.SELL


def test_hold_when_flat():
    prices = [0.50] * 25
    result = evaluate(_hist(prices), window=20)
    assert result.signal is Signal.HOLD


def test_no_duplicate_buy_when_long():
    prices = [0.45] * 20 + [0.55]
    result = evaluate(_hist(prices), window=20, currently_long=True)
    assert result.signal is Signal.HOLD


def test_insufficient_history():
    result = evaluate(_hist([0.5, 0.5]), window=20)
    assert result.signal is Signal.HOLD
    assert "need at least" in result.reason
