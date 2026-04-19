"""Typed configuration loaded from environment variables / ``.env``.

All settings have safe defaults so the bot can run in dry-run mode without any
credentials. Live trading requires ``PRIVATE_KEY`` and ``FUNDER_ADDRESS``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at import time
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class BotConfig:
    """All runtime settings for the bot."""

    # Safety
    dry_run: bool = True
    kill_switch: bool = False

    # Wallet / CLOB
    private_key: Optional[str] = None
    signature_type: int = 0  # 0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE
    funder_address: Optional[str] = None
    chain_id: int = 137
    clob_host: str = "https://clob.polymarket.com"

    # Strategy
    ma_window: int = 20
    price_interval: str = "max"
    price_fidelity: int = 60

    # Risk
    max_trade_size_usdc: float = 5.0
    max_daily_spend_usdc: float = 25.0
    max_open_positions: int = 3
    min_volume_usdc: float = 5_000.0
    min_liquidity_usdc: float = 500.0
    max_spread: float = 0.05
    trade_cooldown_sec: int = 300
    category_allowlist: List[str] = field(default_factory=list)
    market_allowlist: List[str] = field(default_factory=list)

    # Runtime
    poll_interval_sec: int = 60
    max_markets: int = 20
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: Optional[str] = ".env") -> "BotConfig":
        """Load config from a ``.env`` file (if present) plus process env."""
        if env_file:
            load_dotenv(env_file, override=False)

        cfg = cls(
            dry_run=_get_bool("DRY_RUN", True),
            kill_switch=_get_bool("KILL_SWITCH", False),
            private_key=os.getenv("PRIVATE_KEY") or None,
            signature_type=_get_int("SIGNATURE_TYPE", 0),
            funder_address=os.getenv("FUNDER_ADDRESS") or None,
            chain_id=_get_int("CHAIN_ID", 137),
            clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
            ma_window=_get_int("MA_WINDOW", 20),
            price_interval=os.getenv("PRICE_INTERVAL", "max"),
            price_fidelity=_get_int("PRICE_FIDELITY", 60),
            max_trade_size_usdc=_get_float("MAX_TRADE_SIZE_USDC", 5.0),
            max_daily_spend_usdc=_get_float("MAX_DAILY_SPEND_USDC", 25.0),
            max_open_positions=_get_int("MAX_OPEN_POSITIONS", 3),
            min_volume_usdc=_get_float("MIN_VOLUME_USDC", 5_000.0),
            min_liquidity_usdc=_get_float("MIN_LIQUIDITY_USDC", 500.0),
            max_spread=_get_float("MAX_SPREAD", 0.05),
            trade_cooldown_sec=_get_int("TRADE_COOLDOWN_SEC", 300),
            category_allowlist=_get_list("CATEGORY_ALLOWLIST"),
            market_allowlist=_get_list("MARKET_ALLOWLIST"),
            poll_interval_sec=_get_int("POLL_INTERVAL_SEC", 60),
            max_markets=_get_int("MAX_MARKETS", 20),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        return cfg

    def require_live_credentials(self) -> None:
        """Raise ``ValueError`` if the settings needed for live trading are missing."""
        missing = []
        if not self.private_key:
            missing.append("PRIVATE_KEY")
        if not self.funder_address:
            missing.append("FUNDER_ADDRESS")
        if self.signature_type not in (0, 1, 2):
            missing.append("SIGNATURE_TYPE (must be 0, 1, or 2)")
        if missing:
            raise ValueError(
                "Live trading requires these env vars: " + ", ".join(missing)
            )
