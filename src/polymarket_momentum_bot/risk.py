"""Risk controls + in-memory position bookkeeping.

The bot never exceeds:
- ``max_trade_size_usdc`` on any single order
- ``max_daily_spend_usdc`` cumulative spend per UTC day
- ``max_open_positions`` concurrent open positions
- per-market ``trade_cooldown_sec`` between trades

It also rejects markets that are too thin (``min_volume_usdc``,
``min_liquidity_usdc``, ``max_spread``) and honors the global ``kill_switch``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .config import BotConfig

log = logging.getLogger(__name__)


@dataclass
class Position:
    token_id: str
    size: float  # number of outcome shares held
    avg_price: float
    opened_at: float = field(default_factory=time.time)


@dataclass
class PositionBook:
    """Tracks open positions and daily spend. Purely in-memory."""

    positions: Dict[str, Position] = field(default_factory=dict)
    daily_spend_usdc: float = 0.0
    _spend_day: str = field(default_factory=lambda: _utc_day())
    last_trade_at: Dict[str, float] = field(default_factory=dict)

    def _roll_day(self) -> None:
        today = _utc_day()
        if today != self._spend_day:
            self.daily_spend_usdc = 0.0
            self._spend_day = today

    def is_long(self, token_id: str) -> bool:
        return token_id in self.positions and self.positions[token_id].size > 0

    def record_fill(self, token_id: str, side: str, size: float, price: float) -> None:
        """Update the book after a (simulated or real) fill."""
        self._roll_day()
        notional = size * price
        if side.upper() == "BUY":
            self.daily_spend_usdc += notional
            existing = self.positions.get(token_id)
            if existing is None:
                self.positions[token_id] = Position(token_id, size, price)
            else:
                total_size = existing.size + size
                if total_size <= 0:
                    self.positions.pop(token_id, None)
                else:
                    blended = (
                        existing.avg_price * existing.size + price * size
                    ) / total_size
                    existing.size = total_size
                    existing.avg_price = blended
        elif side.upper() == "SELL":
            existing = self.positions.get(token_id)
            if existing is not None:
                existing.size -= size
                if existing.size <= 1e-9:
                    self.positions.pop(token_id, None)
        self.last_trade_at[token_id] = time.time()


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    """Applies config-driven limits before any order is placed."""

    def __init__(self, config: BotConfig, book: Optional[PositionBook] = None) -> None:
        self.config = config
        self.book = book or PositionBook()

    # ----------------------------------------------------------- market gate

    def market_is_tradeable(
        self,
        volume_24h: float,
        liquidity: float,
        spread: Optional[float],
    ) -> Tuple[bool, str]:
        if volume_24h < self.config.min_volume_usdc:
            return False, f"24h volume {volume_24h:.0f} < min {self.config.min_volume_usdc:.0f}"
        if liquidity < self.config.min_liquidity_usdc:
            return False, f"liquidity {liquidity:.0f} < min {self.config.min_liquidity_usdc:.0f}"
        if spread is not None and spread > self.config.max_spread:
            return False, f"spread {spread:.4f} > max {self.config.max_spread:.4f}"
        return True, "ok"

    # ----------------------------------------------------------- order gate

    def check_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> Tuple[bool, str]:
        """Return ``(allowed, reason)`` for a proposed order."""
        self.book._roll_day()
        cfg = self.config

        if cfg.kill_switch:
            return False, "kill switch engaged"

        notional = size * price
        if notional <= 0:
            return False, "non-positive notional"
        if notional > cfg.max_trade_size_usdc:
            return False, (
                f"trade notional {notional:.2f} > max {cfg.max_trade_size_usdc:.2f}"
            )

        if side.upper() == "BUY":
            projected = self.book.daily_spend_usdc + notional
            if projected > cfg.max_daily_spend_usdc:
                return False, (
                    f"daily spend would be {projected:.2f} > "
                    f"max {cfg.max_daily_spend_usdc:.2f}"
                )
            if (
                token_id not in self.book.positions
                and len(self.book.positions) >= cfg.max_open_positions
            ):
                return False, (
                    f"already at max open positions ({cfg.max_open_positions})"
                )

        last = self.book.last_trade_at.get(token_id)
        if last is not None and (time.time() - last) < cfg.trade_cooldown_sec:
            remaining = cfg.trade_cooldown_sec - (time.time() - last)
            return False, f"cooldown active ({remaining:.0f}s remaining)"

        if cfg.market_allowlist and token_id not in cfg.market_allowlist:
            return False, "market not in allowlist"

        return True, "ok"
