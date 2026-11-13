"""FRAMEWORK_GAPS #22：设备图里一台只能跑在 ROS 2 上的设备不能让整个 HostLink runtime 起不来。

跳过它（连同挂在它下面的子设备），其余设备照常启动；跳过的设备写 ERROR 日志，
并在 endpoint 能力快照（设备目录 ``/api/v1/devices``）、``/api/v1/hostlink/peers`` 与
``/api/v1/health`` 里写明原因，不静默丢掉。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from unilabos.backend.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.backend.hostlink.main_hostlink_run import populate_runtime
from unilabos.backend.runtime import definition as definition_module
from unilabos.registry.registry import lab_registry
from unilabos.server.services.materials.graph import graph_resource_tree

MODULE = __name__


class PlainDriver:
    def __init__(self, device_id: str = "", **_kwargs) -> None:
        self.device_id = device_id


class RosOnlyDriver:
    def __init__(self, device_id: str = "", **_kwargs) -> None:
        raise AssertionError("只支持 ROS 2 的驱动不应在 HostLink 上构造")


def _device(device_id: str, template: str, parent: str | None = None) -> dict:
    return {"id": device_id, "uuid": f"00000000-0000-4000-8000-{abs(hash(device_id)) % 10**12:012d}",
            "type": "device", "class": template, "template_name": template, "parent": parent, "config": {}}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setitem(lab_registry.device_type_registry, "test_plain", {
        "class": {"module": f"{MODULE}:PlainDriver", "type": "python"}})
    monkeypatch.setitem(lab_registry.device_type_registry, "test_ros_only", {
        "class": {"module": f"{MODULE}:RosOnlyDriver", "type": "python", "supported_backends": ["ros2"]}})
    # 不接物料权威：只核验本地快照
    monkeypatch.setattr(definition_module, "apply_device_authority_state", lambda *args, **kwargs: None)


def test_ros2_only_device_is_skipped_and_the_rest_of_the_graph_starts(registry) -> None:
    devices = graph_resource_tree([
        _device("arm", "test_ros_only"),
        _device("arm_gripper", "test_plain", parent="arm"),
        _device("balance", "test_plain"),
    ])
    runtime = populate_runtime(HostLinkLocalRuntime(), devices)
    runtime.start()
    try:
        assert set(runtime.devices) == {"balance"}
        skipped = runtime.unavailable_devices
        assert set(skipped) == {"arm", "arm_gripper"}
        assert skipped["arm"]["registry_name"] == "test_ros_only"
        assert skipped["arm"]["supported_backends"] == ["ros2"]
        assert skipped["arm"]["backend"] == "hostlink"
        assert "ros2" in skipped["arm"]["reason"]
        assert skipped["arm_gripper"]["parent_device_id"] == "arm"
        assert "arm" in skipped["arm_gripper"]["reason"]
    finally:
        runtime.stop()


def test_other_invalid_devices_still_fail_the_startup(registry) -> None:
    """只对"本 backend 跑不了"放行；注册表缺失等真正的配置错误照旧让启动失败。"""

    from unilabos.backend.runtime.exception import DeviceClassInvalid

    devices = graph_resource_tree([_device("ghost", "test_missing_registry_entry")])
    with pytest.raises(DeviceClassInvalid):
        populate_runtime(HostLinkLocalRuntime(), devices)


def test_endpoint_snapshot_carries_the_skipped_device_as_a_disabled_route() -> None:
    from unilabos.server.backend.capabilities import build_endpoint_capabilities

    adapter = SimpleNamespace(
        devices_names={"balance": "/devices/balance"},
        _action_value_mappings={"balance": {}},
        unavailable_devices={"arm": {"registry_name": "test_ros_only", "backend": "hostlink",
                                     "supported_backends": ["ros2"], "reason": "只支持 ros2"}},
    )
    routes, capabilities = build_endpoint_capabilities(adapter, observed_at_ms=1)

    by_device = {route.device_uuid: route for route in routes}
    assert by_device["balance"].enabled is True
    arm = by_device["arm"]
    assert arm.enabled is False and arm.selected is False
    assert arm.config["registry_name"] == "test_ros_only"
    assert arm.config["unavailable"]["supported_backends"] == ["ros2"]
    assert all(capability.device_uuid != "arm" for capability in capabilities)


def test_hostlink_diagnostics_and_health_list_the_skipped_device(registry, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from unilabos.backend.hostlink import main_hostlink_run
    from unilabos.server.api import edge_proxy
    from unilabos.server.api.runtime.diagnostics import create_backend_app

    runtime = populate_runtime(HostLinkLocalRuntime(), graph_resource_tree([
        _device("arm", "test_ros_only"), _device("balance", "test_plain")]))
    monkeypatch.setattr(main_hostlink_run, "_runtime", SimpleNamespace(local=runtime))
    edge_proxy.configure_edge_proxy(False)
    client = TestClient(create_backend_app(get_scheduler=lambda: object()))

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert [item["device_id"] for item in health.json()["unavailable_devices"]] == ["arm"]
    peers = client.get("/api/v1/hostlink/peers").json()["data"]
    assert peers["unavailable_devices"][0]["device_id"] == "arm"
    assert peers["unavailable_devices"][0]["supported_backends"] == ["ros2"]
