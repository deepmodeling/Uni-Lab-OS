from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from unilabos.basic.runtime import BasicDeviceNode, BasicRuntime
from unilabos.device_runtime import (
    ActionCancelled,
    ActionContext,
    BackendCapabilityError,
    DeviceNode,
)


class Driver:
    pass


def test_basic_node_implements_backend_neutral_contract() -> None:
    node = BasicDeviceNode(Driver(), "device-1", backend_name="hostlink")

    assert isinstance(node, DeviceNode)
    assert node.backend_name == "hostlink"
    assert node.identifier == "device-1"


def test_status_listeners_receive_backend_neutral_updates() -> None:
    node = BasicDeviceNode(Driver(), "device-1")
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
        feedback_callback=lambda action_id, data: received.append(
            (action_id, data)
        ),
    )

    context.publish_feedback({"progress": 0.5})
    assert received == [("action-1", {"progress": 0.5})]
    assert context.is_cancelled is False

    context.request_cancel()
    assert context.is_cancelled is True
    with pytest.raises(ActionCancelled, match="action-1"):
        context.raise_if_cancelled()


def test_missing_resource_transport_fails_explicitly() -> None:
    node = BasicDeviceNode(Driver(), "device-1", backend_name="hostlink")

    with pytest.raises(BackendCapabilityError, match="hostlink"):
        asyncio.run(node.update_resource([]))


def test_runtime_propagates_selected_backend_to_nodes() -> None:
    runtime = BasicRuntime(backend_name="hostlink")
    assert runtime.backend_name == "hostlink"


def test_migrated_virtual_driver_imports_without_ros() -> None:
    code = (
        "import sys; "
        "import unilabos.devices.virtual.virtual_centrifuge; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
