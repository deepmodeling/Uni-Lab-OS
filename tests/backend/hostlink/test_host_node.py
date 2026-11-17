from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import pytest

from unilabos.backend.hostlink.adapter_registry import (
    clear_execution_adapter,
    get_execution_adapter,
)
from unilabos.backend.hostlink.host_node import HostNode
from unilabos.server.backend.execution import JobExecutionBackend
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.runtime.action import ActionContext
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.server.backend.execution_queue import QueueItem
from unilabos.registry.action_policy import normalize_error_policy
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.protocol.runtime.workflow import WorkflowNodeWrite
from unilabos.server.services.runtime.workflow.service import WorkflowService


class CommunicationError(RuntimeError):
    category = "communication"
    severity = "recoverable"


def test_initializing_device_is_owned_but_not_schedulable():
    """初始化期间的服务路由仍可用，调度能力快照须等 post_init 完成。"""
    from types import SimpleNamespace

    pending = {"device": {"id": "bench", "ready": False}, "online": True}
    ready = {"device": {"id": "rack", "ready": True}, "online": True}
    runtime = SimpleNamespace(
        devices=lambda **kwargs: {"bench": pending, "rack": ready},
        local=SimpleNamespace(descriptors=lambda: []),
        server=SimpleNamespace(devices=lambda online: {"bench": pending, "rack": ready}),
    )
    host = object.__new__(HostNode)
    host.runtime = runtime
    assert set(host._device_snapshot(initial=True)) == {"rack"}
    assert set(host._device_snapshot(initial=False)) == {"rack"}
    pending["device"]["ready"] = True
    assert set(host._device_snapshot(initial=False)) == {"bench", "rack"}


class AdapterDriver:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

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

    def ignores_cancel(self) -> dict[str, bool]:
        """同步驱动、不检查取消：取消请求之后仍要跑完。"""
        self.calls += 1
        self.started.set()
        time.sleep(0.4)
        return {"done": True}

    def samples(self, sample_uuids: dict[str, str]) -> dict[str, Any]:
        return {"samples": sample_uuids}

    def overruns(self) -> dict[str, bool]:
        """同步驱动，超过硬超时仍在跑，由测试决定何时返回。"""
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return {"done": True}


class RecordingBridge:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, dict, dict | None]] = []
        self.decisions: list[dict[str, Any]] = []
        self.late: list[dict[str, Any]] = []
        self.status_event = threading.Event()
        self.decision_event = threading.Event()
        self.late_event = threading.Event()

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

    def publish_job_late_outcome(self, report: dict, _item: QueueItem | None = None) -> bool:
        self.late.append(report)
        self.late_event.set()
        return True


def _runtime() -> tuple[HostLinkBackend, AdapterDriver]:
    local = HostLinkLocalRuntime()
    spec = HostLinkDriverSpec(
        device_id="device-1",
        driver_class=AdapterDriver,
        config={},
        action_names=(
            "succeed",
            "mapped",
            "fail",
            "wait_until_cancelled",
            "ignores_cancel",
            "samples",
            "overruns",
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
            "ignores_cancel": {"type": "NativeAction"},
            "samples": {"type": "UniLabJsonCommand"},
            "overruns": {"type": "NativeAction", "timeout": 0.1},
        },
    )
    node = local.add_driver(spec)
    runtime = HostLinkBackend(local, is_slave=False)
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
    # HostLink HostNode：构造即启动并自行注册 adapter。
    adapter = HostNode("host_node", runtime, bridges=[])
    microbackend = JobExecutionBackend(
        host_node_getter=lambda: adapter,
        result_bridges=[bridge],
    )
    adapter.bridges = [microbackend]
    microbackend.start()
    try:
        yield adapter, microbackend, driver, bridge
    finally:
        clear_execution_adapter(adapter)
        microbackend.stop()
        adapter.stop()
        runtime.stop()
        HostNode.reset_state()


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


def test_local_demo_workflow_task_dispatches_parameterized_hostlink_action(
    execution_stack,
) -> None:
    _adapter, microbackend, driver, _bridge = execution_stack
    service = WorkflowService(":memory:")
    executor = BackendScheduler(service, microbackend)
    service.set_task_submitter(executor.submit)
    executor.start(recover=True)
    try:
        workflow = service.create_workflow(
            name="three-site heating demo",
            tags=["demo"],
            description=None,
            meta_data={},
        )
        node_uuid = str(uuid.uuid4())
        service.save_graph(
            workflow["uuid"],
            revision=workflow["revision"],
            nodes=[
                WorkflowNodeWrite(
                    uuid=node_uuid,
                    name="parameterized HostLink action",
                    type="device_action",
                    material_uuid=str(uuid.uuid4()),
                    action_name="succeed",
                    action_type="NativeAction",
                    param={"value": 11},
                    meta_data={"target_device_id": "device-1"},
                )
            ],
            edges=[],
        )
        task = service.create_workflow_task(
            workflow_uuid=workflow["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description="value=11",
            meta_data={},
        )
        deadline = time.monotonic() + 3
        current = service.get_workflow_task(task["uuid"])
        while current["status"] not in {"succeeded", "failed"}:
            if time.monotonic() >= deadline:
                pytest.fail("workflow task did not reach a terminal state")
            time.sleep(0.02)
            current = service.get_workflow_task(task["uuid"])

        assert current["status"] == "succeeded"
        assert driver.calls == 1
        assert current["output"][node_uuid] == {
            "suc": True,
            "suc_type": "normal",
            "return_value": {"value": 11},
        }
    finally:
        service.set_task_submitter(None)
        executor.stop()
        service.close()


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
    assert bridge.statuses[-1][1] == "failed"
    assert bridge.statuses[-1][3]["suc"] is False
    assert bridge.statuses[-1][3]["suc_type"] == "cancellation"


def test_cancel_waits_for_a_driver_that_cannot_be_interrupted(execution_stack) -> None:
    """同步驱动无法被打断：取消只是请求，驱动真实返回前 job 保持活动，返回后按取消收口。"""

    _adapter, microbackend, driver, bridge = execution_stack
    item = _item("ignores_cancel")
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
    assert microbackend.cancel_job(item.job_id)

    time.sleep(0.15)
    terminal = [status for status in bridge.statuses if status[0] == item.job_id and status[1] != "running"]
    assert terminal == [], "驱动还在跑，不能先报取消"
    assert microbackend.device_manager.get_job_info(item.job_id) is not None

    assert bridge.status_event.wait(2)
    final = [status for status in bridge.statuses if status[0] == item.job_id][-1]
    assert final[1] == "failed"
    assert final[3]["suc_type"] == "cancellation"
    assert not bridge.decisions, "取消后的返回不进失败决策链"
    deadline = time.monotonic() + 2
    while microbackend.device_manager.get_job_info(item.job_id) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert microbackend.device_manager.get_job_info(item.job_id) is None


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
    assert bridge.decision_event.wait(2)
    pending = next(
        report for report in bridge.decisions if report["job_id"] == conflicting.job_id
    )
    assert pending["exception_type"] == "SchedulerDispatchConflict"
    assert not any(job_id == conflicting.job_id for job_id, *_ in bridge.statuses)
    assert microbackend.handle_action_error_decision(
        pending["decision_id"],
        conflicting.job_id,
        {
            "decision_id": pending["decision_id"],
            "job_id": conflicting.job_id,
            "device_id": conflicting.device_id,
            "action": "abort",
            "scheduler_updated": True,
        },
    )
    rejected = next(status for status in bridge.statuses if status[0] == conflicting.job_id)
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


def test_sync_driver_outliving_its_hard_timeout_keeps_the_hold_until_it_returns(execution_stack) -> None:
    """HostLink 本地运行时先报硬超时，同步线程仍在跑：attempt 照常结算但带 timeout_hold，
    线程返回后真实结果作为迟到结果转交，而不是被丢弃（ADR 0008）。"""

    _adapter, microbackend, driver, bridge = execution_stack
    item = _item("overruns")
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
    assert bridge.decision_event.wait(2)
    report = bridge.decisions[-1]
    assert report["exception_type"] == "TimeoutException"
    assert microbackend.handle_action_error_decision(
        report["decision_id"],
        item.job_id,
        {"decision_id": report["decision_id"], "job_id": item.job_id, "device_id": item.device_id,
         "action": "abort", "scheduler_updated": True},
    )
    assert bridge.status_event.wait(2)
    _job, status, _data, return_info = [entry for entry in bridge.statuses if entry[0] == item.job_id][-1]
    assert status == "failed"
    assert return_info["timeout_hold"]["kind"] == "timeout"
    assert return_info["error_info"]["driver_still_running"] is True
    assert bridge.late == []

    driver.release.set()
    assert bridge.late_event.wait(3)
    (late,) = bridge.late
    assert late["job_id"] == item.job_id and late["status"] == "success"
    assert late["return_info"]["return_value"] == {"done": True}
    # 迟到结果不产生第二个终态
    assert [entry[1] for entry in bridge.statuses if entry[0] == item.job_id and entry[1] != "running"] == ["failed"]
