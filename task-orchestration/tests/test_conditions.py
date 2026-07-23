"""OPC 条件快照的行为测试。"""

from task_orchestration.conditions import OpcConditionProvider
from task_orchestration.models import Trigger


def _opc_trigger(value: object) -> Trigger:
    return Trigger(
        kind="opc",
        config={
            "plc_device_id": "line-1",
            "variable": "ready",
            "value": value,
        },
    )


def test_ignores_duplicate_or_stale_sequence_updates():
    provider = OpcConditionProvider(snapshot_ttl_seconds=30, clock=lambda: 100.0)

    assert provider.update("demo.json", "line-1", 2, {"ready": True})
    assert not provider.update("demo.json", "line-1", 2, {"ready": False})
    assert not provider.update("demo.json", "line-1", 1, {"ready": False})

    result = provider.evaluate("demo.json", _opc_trigger(True))

    assert result.satisfied
    assert result.reason is None


def test_merges_increasing_snapshots_without_losing_previous_variables():
    provider = OpcConditionProvider(snapshot_ttl_seconds=30, clock=lambda: 100.0)
    provider.update("demo.json", "line-1", 1, {"ready": True})
    provider.update("demo.json", "line-1", 2, {"temperature": 25})

    result = provider.evaluate("demo.json", _opc_trigger(True))

    assert result.satisfied


def test_isolates_snapshots_by_workflow_and_provider():
    provider = OpcConditionProvider(snapshot_ttl_seconds=30, clock=lambda: 100.0)
    provider.update("demo-a.json", "line-1", 1, {"ready": True})
    provider.update("demo-a.json", "line-2", 1, {"ready": False})

    assert provider.evaluate("demo-a.json", _opc_trigger(True)).satisfied
    assert not provider.evaluate("demo-b.json", _opc_trigger(True)).satisfied
    assert not provider.evaluate(
        "demo-a.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-2", "variable": "ready", "value": True},
        ),
    ).satisfied


def test_ignored_sequences_do_not_refresh_snapshot_ttl():
    now = [100.0]
    provider = OpcConditionProvider(snapshot_ttl_seconds=10, clock=lambda: now[0])
    provider.update("demo.json", "line-1", 2, {"ready": True})
    now[0] += 9

    assert not provider.update("demo.json", "line-1", 2, {"ready": True})

    now[0] += 2
    result = provider.evaluate("demo.json", _opc_trigger(True))

    assert not result.satisfied
    assert result.reason.code == "opc_snapshot_stale"
    assert result.reason.context == {
        "plc_device_id": "line-1",
        "variable": "ready",
        "expected": True,
        "actual": True,
        "updated_at": 100.0,
    }


def test_compares_boolean_numbers_and_strings_without_coercing_strings():
    provider = OpcConditionProvider(snapshot_ttl_seconds=30, clock=lambda: 100.0)
    provider.update(
        "demo.json",
        "line-1",
        1,
        {
            "bool_value": "BOOLEAN",
            "integer_value": 2,
            "float_value": 2.5,
            "string_value": "2",
        },
    )

    assert provider.evaluate(
        "demo.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-1", "variable": "bool_value", "value": True},
        ),
    ).satisfied
    assert provider.evaluate(
        "demo.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-1", "variable": "integer_value", "value": 2.0},
        ),
    ).satisfied
    assert provider.evaluate(
        "demo.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-1", "variable": "float_value", "value": 2.5},
        ),
    ).satisfied
    assert not provider.evaluate(
        "demo.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-1", "variable": "string_value", "value": 2},
        ),
    ).satisfied


def test_compares_boolean_false_and_never_equates_booleans_with_numbers():
    provider = OpcConditionProvider(snapshot_ttl_seconds=30, clock=lambda: 100.0)
    provider.update(
        "demo.json",
        "line-1",
        1,
        {
            "bool_false": False,
            "bool_true": True,
            "number_zero": 0,
            "number_one": 1,
        },
    )

    assert provider.evaluate(
        "demo.json",
        Trigger(
            kind="opc",
            config={"plc_device_id": "line-1", "variable": "bool_false", "value": False},
        ),
    ).satisfied
    for variable, value in (
        ("bool_false", 0),
        ("bool_true", 1),
        ("number_zero", False),
        ("number_one", True),
    ):
        assert not provider.evaluate(
            "demo.json",
            Trigger(
                kind="opc",
                config={"plc_device_id": "line-1", "variable": variable, "value": value},
            ),
        ).satisfied


def test_uses_per_variable_ttl_boundary_and_ignores_unrelated_updates():
    now = [100.0]
    provider = OpcConditionProvider(snapshot_ttl_seconds=10, clock=lambda: now[0])
    provider.update("demo.json", "line-1", 1, {"ready": True})

    now[0] += 10
    assert provider.evaluate("demo.json", _opc_trigger(True)).satisfied

    provider.update("demo.json", "line-1", 2, {"temperature": 25})
    now[0] += 0.1
    stale = provider.evaluate("demo.json", _opc_trigger(True))

    assert not stale.satisfied
    assert stale.reason.code == "opc_snapshot_stale"
    assert stale.reason.context == {
        "plc_device_id": "line-1",
        "variable": "ready",
        "expected": True,
        "actual": True,
        "updated_at": 100.0,
    }


def test_reports_missing_values_and_stale_snapshots():
    now = [100.0]
    provider = OpcConditionProvider(snapshot_ttl_seconds=10, clock=lambda: now[0])
    trigger = _opc_trigger(True)

    missing = provider.evaluate("demo.json", trigger)
    assert not missing.satisfied
    assert missing.reason.code == "opc_variable_missing"
    assert missing.reason.context == {
        "plc_device_id": "line-1",
        "variable": "ready",
        "expected": True,
        "actual": None,
        "updated_at": None,
    }
    assert missing.reason.message == "缺失 OPC 变量：ready"

    provider.update("demo.json", "line-1", 1, {"ready": False})
    mismatch = provider.evaluate("demo.json", trigger)
    assert not mismatch.satisfied
    assert mismatch.reason.code == "opc_value_mismatch"
    assert mismatch.reason.context == {
        "plc_device_id": "line-1",
        "variable": "ready",
        "expected": True,
        "actual": False,
        "updated_at": 100.0,
    }
    assert mismatch.reason.message == "OPC 变量值不匹配：ready（期望 True，当前值 False）"

    now[0] += 11
    stale = provider.evaluate("demo.json", trigger)
    assert not stale.satisfied
    assert stale.reason.code == "opc_snapshot_stale"
    assert stale.reason.context == {
        "plc_device_id": "line-1",
        "variable": "ready",
        "expected": True,
        "actual": False,
        "updated_at": 100.0,
    }


def test_value_mismatch_includes_plc_current_value_and_timestamp():
    provider = OpcConditionProvider(snapshot_ttl_seconds=10, clock=lambda: 20.0)
    trigger = Trigger(
        kind="opc",
        config={
            "plc_device_id": "szlab_poly_plc",
            "variable": "s09",
            "value": True,
        },
    )
    provider.update("demo.json", "szlab_poly_plc", 1, {"s09": False})

    result = provider.evaluate("demo.json", trigger)

    assert not result.satisfied
    assert result.reason.code == "opc_value_mismatch"
    assert result.reason.context == {
        "plc_device_id": "szlab_poly_plc",
        "variable": "s09",
        "expected": True,
        "actual": False,
        "updated_at": 20.0,
    }
