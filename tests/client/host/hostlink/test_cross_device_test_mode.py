"""FRAMEWORK_GAPS #7：``--test_mode`` 也要覆盖驱动发起的跨设备调用。

Host 派发只给声明 ``run_in_test_mode = True`` 的驱动真实执行；但一个在测试模式下真跑的工作站
调用子设备（``node.call_device_action[_async]``）时绕过了这道闸门，直接驱动真实硬件子设备。
跨设备调用现在按同一规则处理：目标驱动没有声明 ``run_in_test_mode`` 时返回模拟结果，
跨机调用由目标所在进程按目标驱动判定。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from unilabos.client.host.hostlink.backend import HostLinkBackend
from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig

TOOL_MAPPING = {
    "tool": {
        "type": "UniLabJsonCommand",
        "handles": {"output": [{"handler_key": "reading", "data_key": "reading"}]},
    }
}


class HardwareDriver:
    """真实硬件替身：被真正调用时记账。"""

    calls: list[str] = []

    def __init__(self, device_id=None, config=None):
        self.device_id = device_id

    def tool(self, value: int = 0) -> dict:
        HardwareDriver.calls.append(self.device_id)
        return {"reading": value * 10}

    async def tool_async(self, value: int = 0) -> dict:
        return self.tool(value)


class SimulatedDriver(HardwareDriver):
    run_in_test_mode = True


class StationDriver:
    """测试模式下真跑的工作站：调用子设备。"""

    run_in_test_mode = True

    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None

    def post_init(self, node) -> None:
        self.node = node

    def drive(self, target: str) -> dict:
        return self.node.call_device_action(target, "tool", {"value": 2})

    async def drive_async(self, target: str) -> dict:
        return await self.node.call_device_action_async(target, "tool", {"value": 3})


def _spec(device_id: str, driver_class: type) -> HostLinkDriverSpec:
    return HostLinkDriverSpec(
        device_id,
        driver_class,
        {},
        action_names=("tool", "tool_async", "drive", "drive_async"),
        action_value_mappings=dict(TOOL_MAPPING),
    )


@pytest.fixture(autouse=True)
def _reset_calls():
    HardwareDriver.calls = []
    yield
    HardwareDriver.calls = []


def _local_runtime() -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(_spec("station", StationDriver))
    runtime.add_driver(_spec("hardware", HardwareDriver))
    runtime.add_driver(_spec("simulator", SimulatedDriver))
    return runtime


@pytest.mark.parametrize("with_backend", [False, True])
def test_cross_device_calls_are_mocked_for_children_that_do_not_opt_in(monkeypatch, with_backend) -> None:
    monkeypatch.setattr(BasicConfig, "test_mode", True)
    runtime = _local_runtime()
    backend = HostLinkBackend(runtime, is_slave=False) if with_backend else None
    runtime.start()
    try:
        mocked = runtime.call_action("station", "drive", target="hardware")
        assert mocked == {"test_mode": True, "action_name": "tool", "reading": {}}
        mocked_async = asyncio.run(runtime.call_action_async("station", "drive_async", target="hardware"))
        assert mocked_async["test_mode"] is True
        assert HardwareDriver.calls == []  # 真实硬件一次都没被调用

        # 声明了 run_in_test_mode 的子设备照常真实执行
        assert runtime.call_action("station", "drive", target="simulator") == {"reading": 20}
        assert asyncio.run(runtime.call_action_async("station", "drive_async", target="simulator")) == {"reading": 30}
        assert HardwareDriver.calls == ["simulator", "simulator"]
    finally:
        if backend is not None:
            backend.stop()
        else:
            runtime.stop()


def test_cross_device_calls_run_for_real_outside_test_mode(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "test_mode", False)
    runtime = _local_runtime()
    runtime.start()
    try:
        assert runtime.call_action("station", "drive", target="hardware") == {"reading": 20}
        assert HardwareDriver.calls == ["hardware"]
    finally:
        runtime.stop()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_cross_machine_calls_are_decided_by_the_target_process(monkeypatch) -> None:
    """跨机调用：调用方带上测试模式，目标所在进程按目标驱动的 run_in_test_mode 判定。"""

    monkeypatch.setattr(BasicConfig, "test_mode", True)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 5.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "test-mode-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host_runtime = HostLinkLocalRuntime()
    host_runtime.add_driver(_spec("station", StationDriver))
    host_runtime.add_driver(_spec("host_hardware", HardwareDriver))
    slave_runtime = HostLinkLocalRuntime()
    slave_runtime.add_driver(_spec("slave_station", StationDriver))
    slave_runtime.add_driver(_spec("slave_hardware", HardwareDriver))
    slave_runtime.add_driver(_spec("slave_simulator", SimulatedDriver))
    host = HostLinkBackend(host_runtime, is_slave=False)
    slave = HostLinkBackend(slave_runtime, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert _wait_until(lambda: "slave_simulator" in host.devices())
        # Host 上的工作站 -> Slave 上的设备
        assert host.call_action("station", "drive", target="slave_hardware")["test_mode"] is True
        assert host.call_action("station", "drive", target="slave_simulator") == {"reading": 20}
        # Slave 上的工作站 -> Host 上的设备
        assert host.call_action("slave_station", "drive", target="host_hardware")["test_mode"] is True
        assert HardwareDriver.calls == ["slave_simulator"]
    finally:
        slave.stop()
        host.stop()


def test_slave_descriptor_declares_run_in_test_mode() -> None:
    """设备描述带上驱动的 run_in_test_mode：驱动在 Slave 进程里时 Host 只能从描述得知。"""

    runtime = _local_runtime()
    descriptors = {item["id"]: item for item in runtime.descriptors()}
    assert descriptors["simulator"]["run_in_test_mode"] is True
    assert "run_in_test_mode" not in descriptors["hardware"]


def test_host_dispatched_jobs_follow_the_slave_driver_declaration(monkeypatch) -> None:
    """测试模式下 Host 派发的作业：Slave 上声明 run_in_test_mode 的设备照常派过去真实执行（与它在 Host
    里运行时一样），没声明的由 Host 返回模拟结果、不触达驱动。修复前 Slave 上的设备一律被 Host 模拟。"""

    import uuid

    from tests.client.host.hostlink.test_host_node import RecordingBridge
    from unilabos.client.host.hostlink.adapter_registry import clear_execution_adapter
    from unilabos.client.host.hostlink.host_node import HostNode
    from unilabos.server.execution_queue import QueueItem

    monkeypatch.setattr(BasicConfig, "test_mode", True)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 5.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "test-mode-dispatch-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave_runtime = HostLinkLocalRuntime()
    slave_runtime.add_driver(_spec("slave_hardware", HardwareDriver))
    slave_runtime.add_driver(_spec("slave_simulator", SimulatedDriver))
    slave = HostLinkBackend(slave_runtime, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    adapter = None
    try:
        slave.start()
        adapter = HostNode("host_node", host, bridges=[])
        bridge = RecordingBridge()
        adapter.bridges = [bridge]
        assert _wait_until(lambda: {"slave_hardware", "slave_simulator"} <= set(adapter.devices_names))
        outcomes = {}
        for device_id in ("slave_simulator", "slave_hardware"):
            bridge.status_event.clear()
            job_id = str(uuid.uuid4())
            item = QueueItem(
                task_type="job_call_back_status", device_id=device_id, action_name="tool", task_id="task-1",
                job_id=job_id, notebook_id="notebook-1", device_action_key=f"/devices/{device_id}/tool",
            )
            adapter.send_goal(item, "UniLabJsonCommand", {"value": 4}, {})
            assert bridge.status_event.wait(5), bridge.statuses
            outcomes[device_id] = next(entry for entry in bridge.statuses if entry[0] == job_id)
        assert HardwareDriver.calls == ["slave_simulator"]
        assert outcomes["slave_simulator"][1] == "success" and "40" in str(outcomes["slave_simulator"][3])
        assert outcomes["slave_hardware"][1] == "success" and "test_mode" in str(outcomes["slave_hardware"][3])
    finally:
        if adapter is not None:
            clear_execution_adapter(adapter)
            adapter.stop()
            HostNode.reset_state()
        slave.stop()
        host.stop()
