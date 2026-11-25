"""一台设备装配、构造或初始化失败时只让它进入维护模式：Host 与其余设备照常运行。

以前只有「持有的物料建不成 PLR 实例」走维护模式；权威里的设备状态与注册表对不上（例如设备 Site
不在注册表 ``available_sites`` 里）、驱动构造器抛错、``post_init`` / ``setup`` / ``initialize`` 抛错
都会从 ``populate_runtime`` / ``HostLinkLocalRuntime.start`` 抛出去，整个 Host 以 CRITICAL 退出。
现在它们与物料建不成同一份不可用记录（``maintenance`` / ``stage`` / ``reason``）；子设备没有宿主，一并不启动。
图配置错误（注册表里没有这个设备）仍让启动失败，见 ``test_backend_unsupported_devices``。
隔离只在 Host 进程（``isolate_device_failures=True``）；Slave 进程失败即退出，交给受管进程的重启策略
（``--root_devices_as_slaves``，见 ``tests/e2e/test_root_device_slaves.py``）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.client.host.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.client.host.hostlink.main_hostlink_run import populate_runtime
from unilabos.client.host.runtime import definition as definition_module
from unilabos.registry.registry import lab_registry
from unilabos.server.services.materials.graph import graph_resource_tree

MODULE = __name__


class PlainDriver:
    def __init__(self, device_id: str = "", **_kwargs) -> None:
        self.device_id = device_id
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


class BrokenConstructorDriver:
    def __init__(self, device_id: str = "", **_kwargs) -> None:
        raise RuntimeError("串口打不开")


class BrokenInitializeDriver(PlainDriver):
    def initialize(self) -> bool:
        raise ConnectionError("仪器没有应答")


@pytest.fixture
def registry(monkeypatch):
    for name, driver in (
        ("test_plain", "PlainDriver"),
        ("test_broken_constructor", "BrokenConstructorDriver"),
        ("test_broken_initialize", "BrokenInitializeDriver"),
    ):
        monkeypatch.setitem(lab_registry.device_type_registry, name, {
            "class": {"module": f"{MODULE}:{driver}", "type": "python"}})
    # 不接物料权威：只核验本地快照
    monkeypatch.setattr(definition_module, "apply_device_authority_state", lambda *args, **kwargs: None)


def _host_runtime() -> HostLinkLocalRuntime:
    return HostLinkLocalRuntime(isolate_device_failures=True)


def _device(device_id: str, template: str, parent: str | None = None) -> dict:
    return {"id": device_id, "uuid": str(uuid4()), "type": "device", "class": template,
            "template_name": template, "parent": parent, "config": {}}


def test_a_driver_whose_constructor_fails_enters_maintenance(registry) -> None:
    devices = graph_resource_tree([
        _device("pump", "test_broken_constructor"),
        _device("pump_valve", "test_plain", parent="pump"),
        _device("balance", "test_plain"),
    ])

    runtime = populate_runtime(_host_runtime(), devices)
    runtime.start()
    try:
        assert set(runtime.devices) == {"balance"}
        record = runtime.unavailable_devices["pump"]
        assert record["maintenance"] is True and record["stage"] == "construct"
        assert record["error_type"] == "RuntimeError" and "串口打不开" in record["reason"]
        assert record["registry_name"] == "test_broken_constructor" and record["backend"] == "hostlink"
        assert runtime.unavailable_devices["pump_valve"]["parent_device_id"] == "pump"
    finally:
        runtime.stop()


def test_a_driver_whose_initialize_fails_enters_maintenance(registry) -> None:
    devices = graph_resource_tree([
        _device("reader", "test_broken_initialize"),
        _device("reader_door", "test_plain", parent="reader"),
        _device("balance", "test_plain"),
    ])
    runtime = populate_runtime(_host_runtime(), devices)
    door = runtime.devices["reader_door"]

    runtime.start()
    try:
        assert set(runtime.devices) == {"balance"}
        assert runtime.devices["balance"]._ready
        record = runtime.unavailable_devices["reader"]
        assert record["maintenance"] is True and record["stage"] == "initialize"
        assert record["error_type"] == "ConnectionError" and "仪器没有应答" in record["reason"]
        # 子设备先于父设备启动；父设备起不来，它也停下并登记原因
        assert runtime.unavailable_devices["reader_door"]["parent_device_id"] == "reader"
        assert door.driver.cleaned is True and not door._ready
    finally:
        runtime.stop()


def test_authority_state_that_does_not_fit_the_registry_enters_maintenance(registry, monkeypatch) -> None:
    """权威里机械臂的 Site 不在注册表 ``available_sites`` 里：只有这台设备起不来。"""

    def apply(device_config, registry_entry, registry_name):
        if device_config.res_content.id == "arm":
            raise ValueError("物料权威中设备 arm 存在注册表 available_sites 未声明的 Site: ['held_material']")

    monkeypatch.setattr(definition_module, "apply_device_authority_state", apply)
    devices = graph_resource_tree([_device("arm", "test_plain"), _device("balance", "test_plain")])

    runtime = populate_runtime(_host_runtime(), devices)
    runtime.start()
    try:
        assert set(runtime.devices) == {"balance"}
        record = runtime.unavailable_devices["arm"]
        assert record["maintenance"] is True and record["stage"] == "definition"
        assert "held_material" in record["reason"] and record["registry_name"] == "test_plain"
    finally:
        runtime.stop()


def test_the_startup_failure_reaches_the_health_route(registry, monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from unilabos.client.host.hostlink import main_hostlink_run
    from unilabos.server.api import edge_proxy
    from unilabos.server.api.runtime.diagnostics import create_backend_app

    runtime = populate_runtime(_host_runtime(), graph_resource_tree([
        _device("reader", "test_broken_initialize"), _device("balance", "test_plain")]))
    runtime.start()
    try:
        monkeypatch.setattr(main_hostlink_run, "_runtime", SimpleNamespace(local=runtime))
        edge_proxy.configure_edge_proxy(False)
        client = TestClient(create_backend_app(get_scheduler=lambda: object()))

        [record] = client.get("/api/v1/health").json()["data"]["unavailable_devices"]
        assert record["device_id"] == "reader" and record["maintenance"] is True
        assert record["stage"] == "initialize"
    finally:
        runtime.stop()


def test_a_slave_process_still_fails_so_its_restart_policy_applies(registry) -> None:
    """Slave 进程保持失败即退出：构造失败从 populate 抛出，初始化失败从 start 抛出（已启动的设备收尾）。"""

    with pytest.raises(RuntimeError, match="串口打不开"):
        populate_runtime(HostLinkLocalRuntime(), graph_resource_tree([_device("pump", "test_broken_constructor")]))

    runtime = populate_runtime(HostLinkLocalRuntime(), graph_resource_tree([
        _device("reader", "test_broken_initialize"), _device("reader_door", "test_plain", parent="reader")]))
    door = runtime.devices["reader_door"]
    with pytest.raises(ConnectionError, match="仪器没有应答"):
        runtime.start()
    assert door.driver.cleaned is True
    assert runtime.unavailable_devices == {}
