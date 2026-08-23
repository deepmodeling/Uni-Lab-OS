from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from uuid import uuid4

import pytest
from pylabrobot.resources import Coordinate

from unilabos.hostlink.local_runtime import HostLinkDeviceNode, HostLinkLocalRuntime
from unilabos.device_runtime import (
    ActionCancelled,
    ActionContext,
    BackendCapabilityError,
    DeviceNode,
)
from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceTreeSet,
)


class Driver:
    pass


class RecordingSnapshotService:
    def __init__(self) -> None:
        self.snapshots: list[ResourceTreeSet] = []
        self.received = threading.Event()

    async def snapshot_resource_tree(
        self,
        device_id: str,
        device_uuid: str,
        root_resource: ResourceTreeSet,
    ) -> ResourceTreeSet:
        assert device_id == "device-1"
        assert device_uuid == "device-uuid"
        self.snapshots.append(ResourceTreeSet.load(root_resource.dump()))
        self.received.set()
        return root_resource


def _tracked_container(name: str) -> RegularContainer:
    resource = RegularContainer(
        name=name,
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    resource.unilabos_uuid = str(uuid4())
    resource.unilabos_extra = {
        "unilabos_resource_class": "tracked-container"
    }
    return resource


def test_hostlink_node_implements_backend_neutral_contract() -> None:
    node = HostLinkDeviceNode(Driver(), "device-1")

    assert isinstance(node, DeviceNode)
    assert node.backend_name == "hostlink"
    assert node.identifier == "device-1"


def test_status_listeners_receive_backend_neutral_updates() -> None:
    node = HostLinkDeviceNode(Driver(), "device-1")
    received = []
    node.add_status_listener(
        lambda device_id, name, value: received.append((device_id, name, value))
    )

    node.emit_status("temperature", 25.0)

    assert received == [("device-1", "temperature", 25.0)]
    assert node.latest_status() == {"temperature": 25.0}


def test_action_context_carries_feedback_and_cancellation() -> None:
    received = []
    context = ActionContext(
        action_id="action-1",
        feedback_callback=lambda action_id, data: received.append((action_id, data)),
    )

    context.publish_feedback({"progress": 0.5})
    assert received == [("action-1", {"progress": 0.5})]
    assert context.is_cancelled is False

    context.request_cancel()
    assert context.is_cancelled is True
    with pytest.raises(ActionCancelled, match="action-1"):
        context.raise_if_cancelled()


def test_missing_resource_transport_fails_explicitly() -> None:
    node = HostLinkDeviceNode(Driver(), "device-1")

    with pytest.raises(BackendCapabilityError, match="hostlink"):
        asyncio.run(node.update_resource([]))


def test_device_node_automatically_snapshots_the_complete_tracked_root() -> None:
    root = _tracked_container("root")
    child = _tracked_container("child")
    sibling = _tracked_container("sibling")
    root.assign_child_resource(child, Coordinate(1, 2, 3))
    root.assign_child_resource(sibling, Coordinate(4, 5, 6))
    tracker = DeviceNodeResourceTracker()
    tracker.add_resource(root)
    service = RecordingSnapshotService()
    node = HostLinkDeviceNode(
        Driver(),
        "device-1",
        resource_uuid="device-uuid",
        resource_tracker=tracker,
    )
    node.set_resource_service(service)
    node.start()
    try:
        child.tracker.set_liquids([("solid", 3.0, "ug")])
        assert service.received.wait(timeout=2)
        assert len(service.snapshots) == 1
        snapshot = service.snapshots[0]
        assert set(snapshot.all_nodes_uuid) == {
            root.unilabos_uuid,
            child.unilabos_uuid,
            sibling.unilabos_uuid,
        }
        by_name = {
            item.res_content.name: item.res_content
            for item in snapshot.all_nodes
        }
        assert by_name["child"].substances == [("solid", 3.0, "ug")]
    finally:
        node.stop()


def test_runtime_propagates_selected_backend_to_nodes() -> None:
    runtime = HostLinkLocalRuntime()
    assert runtime.backend_name == "hostlink"


def test_run_async_func_uses_current_backend_and_executes_once() -> None:
    node = HostLinkDeviceNode(Driver(), "device-1")
    calls = []
    traced = []

    async def operation(value: int) -> int:
        calls.append(value)
        await asyncio.sleep(0)
        return value * 2

    node.start()
    try:
        future = node.run_async_func(
            operation,
            inner_trace_callback=traced.append,
            value=21,
        )
        assert future.result(timeout=1) == 42
        assert calls == [21]
        assert traced == [42]
    finally:
        node.stop()


def test_run_async_func_propagates_error_to_future_and_trace_callback() -> None:
    node = HostLinkDeviceNode(Driver(), "device-1")
    traced = []

    async def operation() -> None:
        raise ValueError("expected failure")

    node.start()
    try:
        future = node.run_async_func(
            operation,
            trace_error=False,
            inner_trace_callback=traced.append,
        )
        with pytest.raises(ValueError, match="expected failure"):
            future.result(timeout=1)
        assert len(traced) == 1
        assert isinstance(traced[0], ValueError)
    finally:
        node.stop()


def test_migrated_virtual_driver_imports_without_ros() -> None:
    code = (
        "import sys; "
        "import unilabos.devices.virtual.virtual_centrifuge; "
        "import unilabos.devices.virtual.workbench; "
        "import unilabos.devices.neware_battery_test_system.neware_battery_test_system; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_liquid_handling_package_keeps_optional_rviz_ros_import_lazy() -> None:
    code = (
        "import sys; "
        "from unilabos.config.config import BasicConfig; "
        "BasicConfig.backend = 'hostlink'; "
        "import unilabos.devices.liquid_handling; "
        "assert 'rclpy' not in sys.modules; "
        "assert 'unilabos.ros' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_liquid_handler_drivers_import_in_hostlink_without_ros_runtime() -> None:
    pytest.importorskip("pylibftdi")
    code = (
        "import sys; "
        "from unilabos.config.config import BasicConfig; "
        "BasicConfig.backend = 'hostlink'; "
        "from unilabos.devices.liquid_handling.liquid_handler_abstract "
        "import LiquidHandlerAbstract; "
        "from unilabos.devices.liquid_handling.prcxi.prcxi "
        "import PRCXI9300Handler; "
        "from unilabos.resources.plr_additional_res_reg import register; "
        "register(); "
        "assert LiquidHandlerAbstract and PRCXI9300Handler; "
        "assert 'rclpy' not in sys.modules; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
