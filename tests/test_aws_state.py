"""Offline tests for the DynamoDB state writer and metrics publisher."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from polymarket_momentum_bot.aws.state import MetricsPublisher, StateWriter, _to_dynamo


class _FakeTable:
    def __init__(self) -> None:
        self.puts: List[Dict[str, Any]] = []
        self.deletes: List[Dict[str, Any]] = []

    def put_item(self, *, Item: Dict[str, Any]) -> None:
        self.puts.append(Item)

    def delete_item(self, *, Key: Dict[str, Any]) -> None:
        self.deletes.append(Key)


class _FakeResource:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {}

    def Table(self, name: str) -> _FakeTable:
        self.tables.setdefault(name, _FakeTable())
        return self.tables[name]


class _FakeBoto3:
    def __init__(self) -> None:
        self.resource_obj = _FakeResource()
        self.cw_calls: List[Dict[str, Any]] = []

    def resource(self, name: str, **_kwargs: Any) -> _FakeResource:
        assert name == "dynamodb"
        return self.resource_obj

    def client(self, name: str, **_kwargs: Any) -> Any:
        assert name == "cloudwatch"
        outer = self

        class CW:
            def put_metric_data(self, **kwargs: Any) -> None:
                outer.cw_calls.append(kwargs)

        return CW()


def test_state_writer_disabled_without_env(monkeypatch):
    monkeypatch.delenv("STATE_TABLE", raising=False)
    monkeypatch.delenv("SIGNALS_TABLE", raising=False)
    monkeypatch.delenv("ORDERS_TABLE", raising=False)
    writer = StateWriter(boto3_module=_FakeBoto3())
    assert writer.enabled is False
    writer.heartbeat()
    writer.record_signal("tok", "q", "BUY", 0.5, 0.4, "reason")  # silent no-op


def test_state_writer_writes_all_tables():
    boto3 = _FakeBoto3()
    writer = StateWriter(
        bot_id="default",
        state_table="state",
        signals_table="signals",
        orders_table="orders",
        boto3_module=boto3,
    )
    assert writer.enabled is True

    writer.heartbeat(status="scanning", extra={"markets_fetched": 12})
    writer.record_signal(
        token_id="tok1",
        question="Will X happen?",
        signal="BUY",
        last_price=0.55,
        moving_average=0.52,
        reason="crossed above",
    )
    writer.record_order(
        token_id="tok1",
        side="BUY",
        size=9.0,
        price=0.55,
        dry_run=True,
        ok=True,
        response={"simulated": True},
    )
    writer.record_position("tok1", size=9.0, avg_price=0.55)
    writer.record_error("run_once", "boom")

    state = boto3.resource_obj.tables["state"].puts
    signals = boto3.resource_obj.tables["signals"].puts
    orders = boto3.resource_obj.tables["orders"].puts

    assert any(p["sk"] == "heartbeat" for p in state)
    assert any(p["sk"].startswith("position#") for p in state)
    assert any(p["sk"].startswith("error#") for p in state)
    assert len(signals) == 1
    assert signals[0]["signal"] == "BUY"
    assert len(orders) == 1
    assert orders[0]["ok"] is True


def test_position_with_zero_size_deletes():
    boto3 = _FakeBoto3()
    writer = StateWriter(
        bot_id="default", state_table="state", boto3_module=boto3
    )
    writer.record_position("tok1", size=0.0, avg_price=0.0)
    assert boto3.resource_obj.tables["state"].deletes == [
        {"bot_id": "default", "sk": "position#tok1"}
    ]


def test_to_dynamo_converts_floats():
    converted = _to_dynamo({"a": 1.5, "b": [2.0, {"c": 3.25}]})
    assert isinstance(converted["a"], Decimal)
    assert isinstance(converted["b"][1]["c"], Decimal)


def test_metrics_publisher_disabled_without_namespace(monkeypatch):
    monkeypatch.delenv("METRICS_NAMESPACE", raising=False)
    p = MetricsPublisher(boto3_module=_FakeBoto3())
    assert p.enabled is False
    p.put("x", 1)  # no-op, no error


def test_metrics_publisher_puts_metric():
    boto3 = _FakeBoto3()
    p = MetricsPublisher(namespace="NS", bot_id="b1", boto3_module=boto3)
    p.put("MarketsFetched", 42)
    assert len(boto3.cw_calls) == 1
    call = boto3.cw_calls[0]
    assert call["Namespace"] == "NS"
    assert call["MetricData"][0]["MetricName"] == "MarketsFetched"
    assert call["MetricData"][0]["Value"] == 42.0
