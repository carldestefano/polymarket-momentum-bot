import pytest

from polymarket_momentum_bot.config import BotConfig


def test_defaults_are_safe():
    cfg = BotConfig()
    assert cfg.dry_run is True
    assert cfg.kill_switch is False
    assert cfg.chain_id == 137
    assert cfg.ma_window == 20


def test_from_env_parses(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("KILL_SWITCH", "yes")
    monkeypatch.setenv("MAX_TRADE_SIZE_USDC", "12.5")
    monkeypatch.setenv("CATEGORY_ALLOWLIST", "politics, sports ,")
    monkeypatch.setenv("SIGNATURE_TYPE", "2")

    cfg = BotConfig.from_env(env_file=None)
    assert cfg.dry_run is False
    assert cfg.kill_switch is True
    assert cfg.max_trade_size_usdc == 12.5
    assert cfg.category_allowlist == ["politics", "sports"]
    assert cfg.signature_type == 2


def test_require_live_credentials():
    cfg = BotConfig()  # no key / funder
    with pytest.raises(ValueError):
        cfg.require_live_credentials()

    cfg.private_key = "0x" + "11" * 32
    cfg.funder_address = "0xabc"
    cfg.require_live_credentials()  # no raise
