"""runtime.v1 endpoint 能力快照上报契约测试。

执行编排节点（ros2 / hostlink 各自的 HostNode）的 devices_names 与
action_value_mappings 投影为 device_routes + action_capabilities，微前端
设备页、单点动作表单与工作流画布节点目录以此为唯一数据源。
"""

from __future__ import annotations

from typing import Any

from unilabos.server.backend.capabilities import build_endpoint_capabilities
from unilabos.server.backend.coordinator import WorkflowBusinessCoordinator
from unilabos.server.services.history import HistoryService
from unilabos.server.services.runtime import RuntimeService


class _FakeAdapter:
    """执行适配器契约替身：devices_names + _action_value_mappings。"""

    def __init__(self) -> None:
        self.devices_names = {"host_node": "/devices", "prcxi": "/devices"}
        # route 元数据应能区分真正的 host_node 与同样暴露
        # manual_confirm 的普通设备。
        self._device_descriptors = {
            "host_node": {"registry_name": "host_node"},
            "prcxi": {"registry_name": "virtual_workbench"},
        }
        self._action_value_mappings = {
            "host_node": {
                "transfer_resource": {
                    "type": "UniLabJsonCommand",
                    "schema": {"goal": {"properties": {"site": {"type": "string"}}}},
                    "goal_default": {"site": ""},
                    "placeholder_keys": {
                        "resource": "unilabos_resources",
                        "site": "unilabos_sites",
                    },
                    "handles": {"input": [], "output": []},
                    "always_free": True,
                    "materials_need_lock": ["resource"],
                },
                "apply_deduct_resource": {
                    # 运行期 type 可能被 resolve 成类对象：应字符串化
                    "type": _FakeAdapter,
                    "schema": {},
                    "placeholder_keys": {"resource": "unilabos_deduct_resource"},
                },
            },
            "prcxi": {
                "run_protocol": {"type": "UniLabJsonCommand", "schema": {}},
                "broken": "not-a-dict",  # 非法条目应被跳过
            },
        }


class _ExecutorWithAdapter:
    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def execution_adapter(self) -> Any:
        return self._adapter


def test_build_endpoint_capabilities_projects_devices_and_actions() -> None:
    routes, capabilities = build_endpoint_capabilities(
        _FakeAdapter(), observed_at_ms=1000
    )
    assert [route.device_uuid for route in routes] == ["host_node", "prcxi"]
    assert all(route.enabled and route.selected for route in routes)
    assert routes[0].config == {"registry_name": "host_node", "is_host_node": True}
    assert routes[1].config == {"registry_name": "virtual_workbench"}

    by_key = {
        (item.device_uuid, item.action_name): item for item in capabilities
    }
    assert set(by_key) == {
        ("host_node", "apply_deduct_resource"),
        ("host_node", "transfer_resource"),
        ("prcxi", "run_protocol"),
    }

    transfer = by_key[("host_node", "transfer_resource")]
    assert transfer.action_type == "UniLabJsonCommand"
    assert transfer.concurrency_mode == "unbounded"  # always_free → unbounded
    assert transfer.descriptor["placeholder_keys"] == {
        "resource": "unilabos_resources",
        "site": "unilabos_sites",
    }
    assert transfer.descriptor["always_free"] is True
    assert len(transfer.descriptor_hash) == 64  # canonical_hash 裸 hex

    deduct = by_key[("host_node", "apply_deduct_resource")]
    assert deduct.concurrency_mode == "exclusive"
    # 类对象 type 字符串化进 action_type，不进 descriptor
    assert deduct.action_type == "_FakeAdapter"
    assert "type" not in deduct.descriptor


def test_coordinator_publishes_capability_snapshot(tmp_path) -> None:
    runtime = RuntimeService(tmp_path / "runtime.db")
    history = HistoryService(tmp_path / "history.db")
    adapter = _FakeAdapter()
    coordinator = WorkflowBusinessCoordinator(
        runtime,
        history,
        _ExecutorWithAdapter(adapter),
        endpoint_uuid="hostlink:edge-1",
        transport="hostlink",
        host_uuid="edge-1",
        instance_name="host",
    )

    coordinator.publish_endpoint_capabilities()
    endpoint = runtime.get_endpoint_snapshot("hostlink:edge-1")
    assert [route.device_uuid for route in endpoint.device_routes] == [
        "host_node",
        "prcxi",
    ]
    assert {item.action_name for item in endpoint.action_capabilities} == {
        "transfer_resource",
        "apply_deduct_resource",
        "run_protocol",
    }
    first_version = endpoint.version

    # 相同内容由哈希去重，版本保持不变。
    coordinator.publish_endpoint_capabilities()
    assert runtime.get_endpoint_snapshot("hostlink:edge-1").version == first_version

    # 设备增加：快照更新
    adapter.devices_names["balance"] = "/devices"
    adapter._action_value_mappings["balance"] = {
        "tare": {"type": "UniLabJsonCommand", "schema": {}}
    }
    coordinator.publish_endpoint_capabilities()
    refreshed = runtime.get_endpoint_snapshot("hostlink:edge-1")
    assert refreshed.version > first_version
    assert {route.device_uuid for route in refreshed.device_routes} == {
        "host_node",
        "prcxi",
        "balance",
    }
