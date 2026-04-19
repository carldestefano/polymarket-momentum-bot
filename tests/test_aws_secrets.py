"""Offline tests for the Secrets Manager adapter."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import pytest

from polymarket_momentum_bot.aws import secrets


class _FakeClient:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.calls = []

    def get_secret_value(self, *, SecretId: str) -> Dict[str, Any]:
        self.calls.append(SecretId)
        return {"SecretString": json.dumps(self._payload)}


def test_hydrate_noop_without_env(monkeypatch):
    monkeypatch.delenv("AWS_SECRET_ID", raising=False)
    assert secrets.hydrate_env_from_secret() is False


def test_hydrate_populates_env(monkeypatch):
    client = _FakeClient(
        {
            "PRIVATE_KEY": "0x" + "aa" * 32,
            "FUNDER_ADDRESS": "0xdeadbeef",
            "SIGNATURE_TYPE": "2",
            "CUSTOM_KEY": "custom_value",
        }
    )
    for k in ("PRIVATE_KEY", "FUNDER_ADDRESS", "SIGNATURE_TYPE", "CUSTOM_KEY"):
        monkeypatch.delenv(k, raising=False)

    ok = secrets.hydrate_env_from_secret(
        secret_id="test/wallet", client=client
    )
    assert ok is True
    assert os.environ["PRIVATE_KEY"].startswith("0x")
    assert os.environ["FUNDER_ADDRESS"] == "0xdeadbeef"
    assert os.environ["SIGNATURE_TYPE"] == "2"
    assert os.environ["CUSTOM_KEY"] == "custom_value"
    assert client.calls == ["test/wallet"]


def test_hydrate_does_not_override_existing_unless_asked(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "preset")
    client = _FakeClient({"PRIVATE_KEY": "fromsecret"})
    secrets.hydrate_env_from_secret(secret_id="s", client=client)
    assert os.environ["PRIVATE_KEY"] == "preset"

    secrets.hydrate_env_from_secret(secret_id="s", client=client, override=True)
    assert os.environ["PRIVATE_KEY"] == "fromsecret"


def test_fetch_secret_rejects_non_json():
    class Bad:
        def get_secret_value(self, *, SecretId: str):
            return {"SecretString": "not-json"}

    with pytest.raises(RuntimeError):
        secrets.fetch_secret("x", client=Bad())
