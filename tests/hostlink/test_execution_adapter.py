from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import pytest

from unilabos.app.execution_adapter import (
    clear_execution_adapter,
    get_execution_adapter,
    set_execution_adapter,
)
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.app.web.utils.host_utils import get_host_node_info
from unilabos.app.ws_client import QueueItem, WebSocketClient
from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.config.config import BasicConfig
from unilabos.device_runtime.action import ActionContext
from unilabos.hostlink.backend import HostLinkBackendRuntime
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter
from unilabos.registry.action_policy import normalize_error_policy


class CommunicationError(RuntimeError):
    category = "communication"
    severity = "recoverable"


class AdapterDriver:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()

    def succeed(self, value: int) -> dict[str, int]:
        self.calls += 1
        return {"value": value}

    def mapped(self, driver_value: int) -> dict[str, int]:
        self.calls += 1
        return {"mapped_value": driver_value * 2}

    def fail(self) -> None:
        self.calls += 1
        raise CommunicationError("device offline")

    def wait_until_cancelled(self, action_context: ActionContext) -> None:
        self.calls += 1
        self.started.set()
        while True:
            action_context.raise_if_cancelled()
            time.sleep(0.01)

    def samples(self, sample_uuids: dict[str, str]) -> dict[str, Any]:
        return {"samples": sample_uuids}


class RecordingBridge:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, dict, dict | None]] = []
        self.decisions: list[dict[str, Any]] = []
        self.status_event = threading.Event()
        self.decision_event = threading.Event()

    def publish_job_status(
        self,
        data: dict,
        item: QueueItem,
        status: str,
        return_info: dict | None = None,
    ) -> None:
        self.statuses.append((item.job_id, status, data, return_info))
        if status in {"success", "failed", "canceled"}:
            self.status_event.set()

    def publish_job_error_decision_required(self, report: dict) -> bool:
        self.decisions.append(report)
        self.decision_event.set()
        return True


def _runtime() -> tuple[HostLinkBackendRuntime, AdapterDriver]:
    local = BasicRuntime("hostlink")
    spec = BasicDriverSpec(
        device_id="device-1",
        driver_class=AdapterDriver,
        config={},
        action_names=(
            "succeed",
            "mapped",
            "fail",
            "wait_until_cancelled",
            "samples",
        ),
        action_value_mappings={
            "succeed": {"type": "NativeAction"},
            "mapped": {
                "type": "NativeAction",
                "goal": {"wire_value": "driver_value"},
                "result": {
                    "value": "mapped_value",
                    "success": "success",
                    "return_info": "return_info",
                },
            },
            "fail": {
                "type": "NativeAction",
                "error_policy": normalize_error_policy(
                    {
                        "options": {
                            "CommunicationError": [
                                {"action": "retry", "label": "重试"},
                                {"action": "abort", "label": "终止"},
                                {
                                    "action": "operator_intervention",
                                    "label": "人工替代结果",
                                },
                            ]
                        }
                    }
                ),
            },
            "wait_until_cancelled": {"type": "NativeAction"},
            "samples": {"type": "UniLabJsonCommand"},
        },
    )
    node = local.add_driver(spec)
    runtime = HostLinkBackendRuntime(local, is_slave=False)
    local.start()
    return runtime, node.driver


def _item(action_name: str, *, retry_count: int = 0) -> QueueItem:
    job_id = str(uuid.uuid4())
    return QueueItem(
        task_type="job_call_back_status",
        device_id="device-1",
        action_name=action_name,
        task_id="task-1",
        job_id=job_id,
        notebook_id="notebook-1",
        device_action_key=f"/devices/device-1/{action_name}",
        node_id="node-1",
        retry_count=retry_count,
    )


@pytest.fixture
def execution_stack():
    runtime, driver = _runtime()
    bridge = RecordingBridge()
    adapter = HostLinkExecutionAdapter(
        runtime,
        devices_config=object(),
        resources_config=object(),
        bridges=[],
    )
    microbackend = JobExecutionBackend(
        host_node_getter=lambda: adapter,
        result_bridges=[bridge],
    )
    adapter.bridges = [microbackend]
    adapter.start()
    microbackend.start()
    set_execution_adapter(adapter)
    try:
        yield adapter, microbackend, driver, bridge
    finally:
        clear_execution_adapter(adapter)
        microbackend.stop()
        adapter.stop()
        runtime.stop()


def test_hostlink_adapter_executes_microbackend_job(
    execution_stack,
) -> None:
    adapter, microbackend, _driver, bridge = execution_stack
    item = _item("succeed")

    assert get_execution_adapter(0) is adapter
    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "NativeAction",
            "action_args": {"value": 7},
        }
    )

    assert bridge.status_event.wait(2)
    job_id, status, data, return_info = bridge.statuses[-1]
    assert job_id == item.job_id
    assert status == "success"
    assert data["return_value"] == {"value": 7}
    assert return_info["return_value"] == {"value": 7}


def test_hostlink_adapter_honors_action_goal_and_result_mapping(
    execution_stack,
) -> None:
    _adapter, microbackend, _driver, bridge = execution_stack
    item = _item("mapped")

    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "NativeAction",
            "action_args": {"wire_value": 6},
        }
    )

    assert bridge.status_event.wait(2)
    _job_id, status, data, return_info = bridge.statuses[-1]
    assert status == "success"
    assert data["return_value"] == {"mapped_value": 12}
    assert data["value"] == 12
    assert data["success"] is True
    assert json.loads(data["return_info"])["return_value"] == {
        "mapped_value": 12
    }
    assert return_info["return_value"] == {"mapped_value": 12}


def test_hostlink_failure_waits_for_scheduler_then_retry_releases_failed(
    execution_stack,
) -> None:
    _adapter, microbackend, driver, bridge = execution_stack
    item = _item("fail", retry_count=2)
    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "NativeAction",
            "action_args": {},
            "retry_count": item.retry_count,
        }
    )

    assert bridge.decision_event.wait(2)
    assert not bridge.status_event.is_set()
    report = bridge.decisions[-1]
    assert report["job_id"] == item.job_id
    assert report["node_id"] == "node-1"
    assert report["retry_count"] == 2
    assert report["exception_type"] == "CommunicationError"

    assert microbackend.handle_action_error_decision(
        report["decision_id"],
        item.job_id,
        {
            "decision_id": report["decision_id"],
            "job_id": item.job_id,
            "device_id": item.device_id,
            "action": "retry",
            "scheduler_updated": True,
        },
    )
    assert bridge.status_event.wait(2)
    assert bridge.statuses[-1][1] == "failed"
    assert bridge.statuses[-1][3]["error_resolution"]["selected_action"] == "retry"
    assert driver.calls == 1


def test_hostlink_operator_intervention_replaces_failure_result(
    execution_stack,
) -> None:
    _adapter, microbackend, _driver, bridge = execution_stack
    item = _item("fail")
    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "NativeAction",
            "action_args": {},
        }
    )
    assert bridge.decision_event.wait(2)
    report = bridge.decisions[-1]

    assert microbackend.handle_action_error_decision(
        report["decision_id"],
        item.job_id,
        {
            "decision_id": report["decision_id"],
            "job_id": item.job_id,
            "device_id": item.device_id,
            "action": "operator_intervention",
            "result": {"confirmed": True},
            "scheduler_updated": True,
        },
    )
    assert bridge.status_event.wait(2)
    assert bridge.statuses[-1][1] == "success"
    assert bridge.statuses[-1][3]["return_value"] == {"confirmed": True}
    assert bridge.statuses[-1][2]["raw_return_info"]["suc"] is False


def test_microbackend_cancels_running_hostlink_action(execution_stack) -> None:
    adapter, microbackend, driver, bridge = execution_stack
    item = _item("wait_until_cancelled")
    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "NativeAction",
            "action_args": {},
        }
    )
    assert driver.started.wait(2)
    assert adapter.get_goal_status(item.job_id) == 2
    assert microbackend.cancel_job(item.job_id)
    assert bridge.status_event.wait(2)
    assert bridge.statuses[-1][1] == "canceled"


def test_backend_controlled_microbackend_rejects_scheduler_conflict(
    execution_stack,
) -> None:
    adapter, microbackend, driver, bridge = execution_stack
    running = _item("wait_until_cancelled")
    conflicting = _item("wait_until_cancelled")
    microbackend.dispatch(
        {
            "job_id": running.job_id,
            "task_id": running.task_id,
            "node_id": running.node_id,
            "device_id": running.device_id,
            "action": running.action_name,
            "action_type": "NativeAction",
            "action_args": {},
        }
    )
    assert driver.started.wait(2)

    microbackend.dispatch(
        {
            "job_id": conflicting.job_id,
            "task_id": conflicting.task_id,
            "node_id": conflicting.node_id,
            "device_id": conflicting.device_id,
            "action": conflicting.action_name,
            "action_type": "NativeAction",
            "action_args": {},
        }
    )
    deadline = time.time() + 2
    while time.time() < deadline and not any(
        job_id == conflicting.job_id for job_id, *_ in bridge.statuses
    ):
        time.sleep(0.01)

    rejected = next(
        status for status in bridge.statuses if status[0] == conflicting.job_id
    )
    assert rejected[1] == "failed"
    assert rejected[3]["error_info"]["exception_type"] == (
        "SchedulerDispatchConflict"
    )
    assert adapter.get_goal_status(conflicting.job_id) == 0
    assert driver.calls == 1

    assert microbackend.cancel_job(running.job_id)


def test_hostlink_injects_declared_system_sample_parameter(execution_stack) -> None:
    _adapter, microbackend, _driver, bridge = execution_stack
    item = _item("samples")
    microbackend.dispatch(
        {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": item.node_id,
            "device_id": item.device_id,
            "action": item.action_name,
            "action_type": "UniLabJsonCommand",
            "action_args": {},
            "sample_material": {"sample-1": "material-1"},
        }
    )
    assert bridge.status_event.wait(2)
    assert bridge.statuses[-1][2]["return_value"] == {
        "samples": {"sample-1": "material-1"}
    }


def test_websocket_reports_hostlink_devices_and_action_locks(execution_stack) -> None:
    _adapter, _microbackend, _driver, _bridge = execution_stack
    client = WebSocketClient()
    messages: list[dict[str, Any]] = []
    client.is_disabled = False
    client.is_connected = lambda: True  # type: ignore[method-assign]
    client.message_processor.send_message = lambda message: messages.append(message) or True

    client.report_all_action_locks()
    client.publish_host_ready()

    lock_messages = [
        message for message in messages if message["action"] == "report_action_lock"
    ]
    assert lock_messages
    locks = lock_messages[-1]["data"]["locks"]
    assert {lock["action_name"] for lock in locks} == {
        "succeed",
        "mapped",
        "fail",
        "wait_until_cancelled",
        "samples",
    }
    ready = next(
        message for message in messages if message["action"] == "host_node_ready"
    )
    assert ready["data"]["devices"] == [
        {
            "device_id": "device-1",
            "namespace": "/devices",
            "device_key": "/devices/device-1",
            "is_online": True,
            "machine_name": BasicConfig.machine_name,
        }
    ]


def test_hostlink_adapter_exposes_backend_neutral_status_page_shape(
    execution_stack,
    monkeypatch,
) -> None:
    adapter, _microbackend, _driver, _bridge = execution_stack
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)

    info = get_host_node_info()

    assert info["available"] is True
    assert info["host_node_id"] == adapter.device_id
    assert info["devices"]["device-1"]["is_online"] is True
    assert "/devices/device-1/mapped" in info["action_clients"]
