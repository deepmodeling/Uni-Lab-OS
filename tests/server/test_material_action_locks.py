from __future__ import annotations

import threading
import time
from typing import Any

from unilabos.server.protocol.control import ExecuteJobContent
from unilabos.server.protocol.materials import InventoryRequirement
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.material_locks import MaterialActionLockManager


class _RecordingAdapter:
    def __init__(self) -> None:
        self._action_value_mappings = {
            "device-a": {"use": {"materials_need_lock": ["material"]}},
            "device-b": {"use": {"materials_need_lock": ["material"]}},
        }
        self.goals: list[Any] = []
        self.goal_event = threading.Event()

    def send_goal(self, item, **_kwargs) -> None:
        self.goals.append(item)
        self.goal_event.set()

    def cancel_goal(self, _job_id: str) -> None:
        return None


class _FirstDispatchFailsAdapter(_RecordingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.attempted_job_ids: list[str] = []
        self.first_started = threading.Event()
        self.allow_failure = threading.Event()

    def send_goal(self, item, **_kwargs) -> None:
        self.attempted_job_ids.append(item.job_id)
        if item.job_id == "job-1":
            self.first_started.set()
            assert self.allow_failure.wait(2)
            raise RuntimeError("simulated dispatch failure")
        super().send_goal(item, **_kwargs)


def _payload(job_id: str, device_id: str, material_uuid: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "task_id": "task-1",
        "node_id": f"node-{job_id}",
        "device_id": device_id,
        "action": "use",
        "action_type": "NativeAction",
        "action_args": {"material": {"uuid": material_uuid}},
        "materials_need_lock": ["material"],
    }


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.01)


def test_multi_material_reservation_is_sorted_and_fifo() -> None:
    locks = MaterialActionLockManager()

    assert locks.request("job-1", ["material-b", "material-a"])
    assert locks.held_by("job-1") == ("material-a", "material-b")
    assert not locks.request("job-2", ["material-a"])
    assert not locks.request("job-3", ["material-a"])

    assert locks.release("job-1") == ["job-2"]
    assert locks.release("job-2") == ["job-3"]


def test_later_waiter_cannot_capture_a_material_needed_by_earlier_waiter() -> None:
    locks = MaterialActionLockManager()

    assert locks.request("owner", ["material-a"])
    assert not locks.request("first", ["material-a", "material-b"])
    assert not locks.request("later", ["material-b"])
    assert locks.request("disjoint", ["material-c"])

    assert locks.release("owner") == ["first"]
    assert locks.held_by("first") == ("material-a", "material-b")
    assert locks.waiting_jobs() == ("later",)


def test_releasing_unrelated_job_does_not_bypass_earlier_conflicting_waiter() -> None:
    locks = MaterialActionLockManager()

    assert locks.request("owner-a", ["material-a"])
    assert locks.request("owner-c", ["material-c"])
    assert not locks.request("first", ["material-a", "material-b"])
    assert not locks.request("later", ["material-b"])

    # material-c 与等待队列无关；释放它不能让 later 越过 first 抢到 b。
    assert locks.release("owner-c") == []
    assert locks.waiting_jobs() == ("first", "later")
    assert locks.release("owner-a") == ["first"]


def test_backend_serializes_actions_that_need_the_same_material_uuid() -> None:
    adapter = _RecordingAdapter()
    backend = JobExecutionBackend(host_node_getter=lambda: adapter)
    backend.start()
    try:
        backend.dispatch(_payload("job-1", "device-a", "material-1"))
        backend.dispatch(_payload("job-2", "device-b", "material-1"))
        _wait_until(lambda: len(adapter.goals) == 1)
        assert adapter.goals[0].job_id == "job-1"
        assert backend._material_locks.waiting_jobs() == ("job-2",)

        backend.publish_job_status(
            {}, adapter.goals[0], "success", {"return_value": None}
        )
        _wait_until(lambda: len(adapter.goals) == 2)
        assert adapter.goals[1].job_id == "job-2"

        backend.publish_job_status(
            {}, adapter.goals[1], "success", {"return_value": None}
        )
        assert backend.wait_idle()
        assert backend._material_locks.held_by("job-2") == ()
    finally:
        backend.stop()


def test_dispatch_failure_releases_material_lock_in_finally_path() -> None:
    adapter = _FirstDispatchFailsAdapter()
    backend = JobExecutionBackend(host_node_getter=lambda: adapter)
    backend.start()
    try:
        backend.dispatch(_payload("job-1", "device-a", "material-1"))
        assert adapter.first_started.wait(2)
        backend.dispatch(_payload("job-2", "device-b", "material-1"))
        assert backend._material_locks.waiting_jobs() == ("job-2",)
        adapter.allow_failure.set()

        _wait_until(lambda: adapter.attempted_job_ids == ["job-1", "job-2"])
        assert adapter.goals[0].job_id == "job-2"
    finally:
        adapter.allow_failure.set()
        backend.stop()


def test_declared_material_parameter_requires_authoritative_uuid() -> None:
    try:
        JobExecutionBackend._material_uuids_from_arguments(
            ["material"], {"material": {"id": "local-name-only"}}
        )
    except ValueError as exc:
        assert "无法解析权威物料 UUID" in str(exc)
    else:
        raise AssertionError("a non-authoritative resource id must not be locked")


def test_execute_job_protocol_preserves_material_lock_declaration() -> None:
    content = ExecuteJobContent(
        job_uuid="job-1",
        task_uuid="task-1",
        node_uuid="node-1",
        attempt_group_uuid="attempt-1",
        device_uuid="device-a",
        action_name="use",
        action_args={"material": {"uuid": "material-1"}},
        materials_need_lock=["material"],
        inventory_requirements=[
            InventoryRequirement(
                key="solvent",
                kind="reagent",
                template_uuid="solvent-template",
                quantity=10,
                unit="ul",
            )
        ],
        scheduler_revision=1,
    )

    assert content.model_dump(mode="json")["materials_need_lock"] == ["material"]
    assert content.model_dump(mode="json")["inventory_requirements"] == [
        {
            "key": "solvent",
            "kind": "reagent",
            "material_uuid": None,
            "template_uuid": "solvent-template",
            "lot_uuid": None,
            "parent_material_uuid": None,
            "site_uuid": None,
            "quantity": 10.0,
            "unit": "ul",
        }
    ]
