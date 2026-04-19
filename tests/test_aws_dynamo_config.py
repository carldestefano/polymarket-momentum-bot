"""Offline tests for the DynamoDB config overlay."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from polymarket_momentum_bot.aws.dynamo_config import (
    ConfigOverrides,
    fetch_config_overrides,
)
from polymarket_momentum_bot.config import BotConfig


class _FakeTable:
    def __init__(self, item: Dict[str, Any] | None) -> None:
        self._item = item
        self.puts: list[Dict[str, Any]] = []

    def get_item(self, *, Key: Dict[str, Any]) -> Dict[str, Any]:
        if self._item is None:
            return {}
        return {"Item": dict(self._item)}

    def put_item(self, *, Item: Dict[str, Any]) -> None:
        self.puts.append(Item)


def test_no_table_name_returns_empty(monkeypatch):
    monkeypatch.delenv("CONFIG_TABLE", raising=False)
    overrides = fetch_config_overrides()
    assert overrides.values == {}


def test_applies_overrides_with_type_coercion():
    cfg = BotConfig()
    overrides = ConfigOverrides(
        values={
            "dry_run": False,
            "kill_switch": "yes",
            "max_trade_size_usdc": Decimal("10.5"),
            "max_open_positions": Decimal("7"),
            "category_allowlist": ["politics", "sports"],
            "market_allowlist": "m1, m2",
            "log_level": "DEBUG",
            # unknown field should be ignored silently
            "unknown": "ignored",
        }
    )
    overrides.apply_to(cfg)

    assert cfg.dry_run is False
    assert cfg.kill_switch is True
    assert cfg.max_trade_size_usdc == 10.5
    assert cfg.max_open_positions == 7
    assert cfg.category_allowlist == ["politics", "sports"]
    assert cfg.market_allowlist == ["m1", "m2"]
    assert cfg.log_level == "DEBUG"


def test_fetch_with_fake_table():
    table = _FakeTable(
        {"bot_id": "default", "dry_run": False, "max_trade_size_usdc": Decimal("3")}
    )
    overrides = fetch_config_overrides(
        table_name="configtable", bot_id="default", client=table
    )
    assert "dry_run" in overrides.values
    assert overrides.values["dry_run"] is False


def test_fetch_missing_item_is_empty():
    table = _FakeTable(None)
    overrides = fetch_config_overrides(
        table_name="configtable", bot_id="default", client=table
    )
    assert overrides.values == {}


def test_invalid_override_is_ignored(caplog):
    cfg = BotConfig()
    overrides = ConfigOverrides(values={"max_trade_size_usdc": "not-a-number"})
    overrides.apply_to(cfg)
    # default preserved
    assert cfg.max_trade_size_usdc == 5.0
