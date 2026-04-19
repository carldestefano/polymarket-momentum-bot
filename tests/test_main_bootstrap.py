"""Offline test that main._bootstrap_aws is a no-op without any AWS env vars."""

from __future__ import annotations

from polymarket_momentum_bot.config import BotConfig
from polymarket_momentum_bot.main import _bootstrap_aws


def test_bootstrap_no_aws_env(monkeypatch):
    for k in (
        "AWS_SECRET_ID",
        "CONFIG_TABLE",
        "STATE_TABLE",
        "SIGNALS_TABLE",
        "ORDERS_TABLE",
        "METRICS_NAMESPACE",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = BotConfig()
    cfg2, writer, metrics = _bootstrap_aws(cfg)
    assert cfg2 is cfg
    assert writer is None
    assert metrics is None
