"""Simple moving-average momentum strategy.

Signal rules
------------
- ``BUY``  when the most-recent price crosses **above** the N-period moving
  average (and we are not already long).
- ``SELL`` when the most-recent price crosses **below** the N-period moving
  average (used to exit / close an existing position).
- Otherwise ``HOLD``.

The strategy is intentionally stateless: it takes a list of recent price
points and returns a single ``Signal``. Position tracking happens in
``risk.PositionBook``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategyResult:
    signal: Signal
    last_price: Optional[float]
    moving_average: Optional[float]
    reason: str


def moving_average(prices: Sequence[float], window: int) -> Optional[float]:
    """Return the simple moving average of the last ``window`` prices, or None."""
    if window <= 0 or len(prices) < window:
        return None
    window_slice = prices[-window:]
    return sum(window_slice) / float(window)


def evaluate(
    history: Sequence[dict],
    window: int = 20,
    currently_long: bool = False,
) -> StrategyResult:
    """Return a BUY / SELL / HOLD signal given ``{t, p}`` price history.

    ``currently_long`` lets the caller suppress duplicate BUY signals when a
    position is already open.
    """
    prices: List[float] = [float(pt["p"]) for pt in history if "p" in pt]
    if len(prices) < window + 1:
        return StrategyResult(
            signal=Signal.HOLD,
            last_price=prices[-1] if prices else None,
            moving_average=None,
            reason=f"need at least {window + 1} price points, have {len(prices)}",
        )

    last_price = prices[-1]
    prev_price = prices[-2]
    ma_now = moving_average(prices, window)
    ma_prev = moving_average(prices[:-1], window)

    if ma_now is None or ma_prev is None:
        return StrategyResult(
            signal=Signal.HOLD,
            last_price=last_price,
            moving_average=ma_now,
            reason="moving average unavailable",
        )

    crossed_up = prev_price <= ma_prev and last_price > ma_now
    crossed_down = prev_price >= ma_prev and last_price < ma_now

    if crossed_up and not currently_long:
        return StrategyResult(
            signal=Signal.BUY,
            last_price=last_price,
            moving_average=ma_now,
            reason=f"price {last_price:.4f} crossed above MA{window}={ma_now:.4f}",
        )
    if crossed_down and currently_long:
        return StrategyResult(
            signal=Signal.SELL,
            last_price=last_price,
            moving_average=ma_now,
            reason=f"price {last_price:.4f} crossed below MA{window}={ma_now:.4f}",
        )
    return StrategyResult(
        signal=Signal.HOLD,
        last_price=last_price,
        moving_average=ma_now,
        reason="no crossover",
    )
