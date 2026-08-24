from types import SimpleNamespace

import pytest

from unilabos.server.scheduler.backend import (
    JobExecutionBackend,
    make_device_status_policy_resolver,
)
from unilabos.server.scheduler.status_incidents import StatusIncidentManager


def test_mirrored_registry_status_policy_is_resolved_and_normalized(
    monkeypatch,
) -> None:
    try:
        from unilabos.registry.registry import lab_registry
    except ImportError as exc:
        pytest.skip(f"当前环境未安装完整 unilabos_msgs: {exc}")
    device_id = "slave-pump-1"
    registry_name = "community.example.pump"
    host = SimpleNamespace(
        devices_instances={},
        devices_config=SimpleNamespace(
            all_nodes=[
                SimpleNamespace(
                    res_content=SimpleNamespace(id=device_id, klass=registry_name)
                )
            ]
        ),
        _slave_registry_configs={
            registry_name: {
                "class": {
                    "status_policies": {
                        "mode": {
                            "normal_values": ["Idle"],
                            "incidents": {
                                "Error": {
                                    "code": "pump.mode.error",
                                    "hold": True,
                                }
                            },
                        }
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        lab_registry,
        "device_type_registry",
        {
            registry_name: {
                "class": {
                    "status_policies": {
                        "mode": {"incidents": {"LocalOnly": {}}}
                    }
                }
            }
        },
    )

    policy = make_device_status_policy_resolver(lambda: host)(device_id, "mode")

    assert policy is not None
    assert policy["incidents"][0]["value"] == "Error"
    assert policy["incidents"][0]["code"] == "pump.mode.error"


def test_invalid_declared_policy_creates_fail_closed_device_hold() -> None:
    incidents = StatusIncidentManager()
    backend = JobExecutionBackend(
        status_incidents=incidents,
        status_policy_resolver=lambda _device_id, _prop: {
            "incidents": {
                "Error": {
                    "severity": "not-a-severity",
                }
            }
        },
    )

    backend.report_device_properties("pump-1", {"mode": "Error"})

    active = incidents.list()
    assert len(active) == 1
    assert active[0]["policy_id"] == "unilabos.status_policy.invalid"
    assert active[0]["severity"] == "critical"
    assert active[0]["mode"] == "interlock"
    assert incidents.is_device_held("pump-1")


def test_missing_policy_remains_observability_only() -> None:
    incidents = StatusIncidentManager()
    backend = JobExecutionBackend(
        status_incidents=incidents,
        status_policy_resolver=lambda _device_id, _prop: None,
    )

    backend.report_device_properties("pump-1", {"mode": "Unexpected"})

    assert incidents.list() == []
    assert incidents.holds() == []
