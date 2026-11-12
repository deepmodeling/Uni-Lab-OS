"""设备托管与作业状态同事务提交：观察者看不到"节点已成功、托管还是 reserved"的窗口。

每次提交后立即用**另一条** SQLite 连接读库（模拟 ``/workflow-device-tenancies`` 等观察者），
断言作业状态一旦可见，由它派生的托管转换、使用权 / 准入与执行 Claim 已经同时可见；崩溃在
"终态已落、后续释放未跑"之间也不会留下不一致。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.server.test_workflow_resource_custody import tenancy_request
from tests.server.test_workflow_resource_intervals import make_task, request_for
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from unilabos.server.services.runtime.workflow.service import WorkflowService

_SETTLED = {"succeeded", "failed", "canceled", "timeout", "skipped", "execution_unknown"}


def read_state(path, job_uuid: str) -> Dict[str, Any]:
    """独立连接读一个作业及由它派生的占用（只读已提交的数据）。"""

    reader = sqlite3.connect(path)
    reader.row_factory = sqlite3.Row
    try:
        job = reader.execute("SELECT status FROM workflow_node_job WHERE uuid=?", (job_uuid,)).fetchone()
        transition = reader.execute(
            "SELECT * FROM workflow_tenancy_transition WHERE job_uuid=?", (job_uuid,)
        ).fetchone()
        tenancies = {}
        if transition is not None:
            for field in ("acquire_uuid", "release_uuid"):
                if transition[field]:
                    row = reader.execute("SELECT state FROM workflow_device_tenancy WHERE tenancy_uuid=?",
                                         (transition[field],)).fetchone()
                    tenancies[field] = row["state"]
        claim = reader.execute("SELECT state FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
        admission = reader.execute("SELECT state FROM workflow_resource_admission WHERE job_uuid=?",
                                   (job_uuid,)).fetchone()
        return {
            "status": job["status"],
            "transition": transition["state"] if transition is not None else None,
            "tenancies": tenancies,
            "uses": reader.execute("SELECT COUNT(*) FROM workflow_resource_use WHERE job_uuid=?",
                                   (job_uuid,)).fetchone()[0],
            "admission": admission["state"] if admission is not None else None,
            "claim": claim["state"] if claim is not None else None,
        }
    finally:
        reader.close()


def observe_commits(service: WorkflowService, path, job_uuid: str) -> List[Dict[str, Any]]:
    """挂在 store 事务上：每次提交后立刻从独立连接读一次。"""

    snapshots: List[Dict[str, Any]] = []
    original = service.transaction

    @contextmanager
    def transaction():
        with original() as conn:
            yield conn
        snapshots.append(read_state(path, job_uuid))

    service.transaction = transaction  # type: ignore[method-assign]
    return snapshots


@pytest.fixture
def stack(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    claims = ExecutionClaims(service)
    epoch = claims.activate_authority()
    yield service, path, ResourceIntervals(service), claims, epoch
    service.close()


def start(service, intervals, claims, epoch, request) -> None:
    """与调度器派发一致：区间准入 → 执行 Claim → running。"""

    assert intervals.admit(request, epoch)
    claims.reserve(request, authority_epoch=epoch)
    service.mark_workflow_node_job_running(request.owner_uuid)


def load_sample(service, intervals, claims, epoch, task, sample):
    load = tenancy_request(service, task, 0, sample, acquire="reactor")
    start(service, intervals, claims, epoch, load)
    service.record_workflow_node_job_terminal(load.owner_uuid, status="succeeded", return_info={}, error_info=[])
    return load


@pytest.mark.parametrize("status,expected", [
    ("succeeded", "loaded"), ("failed", "retained"), ("canceled", "retained"), ("timeout", "retained"),
])
def test_load_custody_is_visible_with_the_terminal_status(stack, status, expected) -> None:
    service, path, intervals, claims, epoch = stack
    task = make_task(service, ("robot",))
    load = tenancy_request(service, task, 0, str(uuid4()), acquire="reactor")
    start(service, intervals, claims, epoch, load)
    snapshots = observe_commits(service, path, load.owner_uuid)

    # 调度器 _settle_attempt 写终态用的就是这个入口；之后的 release_job 一次都不调用
    service.record_workflow_node_job_terminal(load.owner_uuid, status=status, return_info={}, error_info=[])

    (committed,) = snapshots
    assert committed["status"] == status
    assert committed["tenancies"] == {"acquire_uuid": expected}
    assert committed["transition"] == ("settled" if status == "succeeded" else "retained")
    assert committed["uses"] == 0 and committed["admission"] == "released" and committed["claim"] == "released"


def test_unload_releases_custody_in_the_same_commit(stack) -> None:
    service, path, intervals, claims, epoch = stack
    task = make_task(service, ("robot", "robot"))
    sample = str(uuid4())
    load_sample(service, intervals, claims, epoch, task, sample)
    unload = tenancy_request(service, task, 1, sample, release="reactor")
    start(service, intervals, claims, epoch, unload)
    snapshots = observe_commits(service, path, unload.owner_uuid)
    service.record_workflow_node_job_terminal(unload.owner_uuid, status="succeeded", return_info={}, error_info=[])
    (committed,) = snapshots
    assert committed["status"] == "succeeded" and committed["tenancies"] == {"release_uuid": "released"}
    with TestClient(create_workflow_app(service)) as client:
        assert client.get("/api/v1/workflow-device-tenancies").json()["data"] == []


def test_execution_unknown_retains_custody_in_the_same_commit(stack) -> None:
    service, path, intervals, claims, epoch = stack
    task = make_task(service, ("robot",))
    load = tenancy_request(service, task, 0, str(uuid4()), acquire="reactor")
    start(service, intervals, claims, epoch, load)
    snapshots = observe_commits(service, path, load.owner_uuid)
    assert service.mark_workflow_node_job_execution_unknown(load.owner_uuid, "Host 断连")
    (committed,) = snapshots
    assert committed["status"] == "execution_unknown"
    assert committed["tenancies"] == {"acquire_uuid": "retained"} and committed["transition"] == "retained"
    # 结果未知不释放任何占用
    # 装载动作的使用权：机械臂、目标设备与样品三把锁
    assert committed["uses"] == 3 and committed["admission"] == "active" and committed["claim"] == "uncertain"


def test_decision_pending_does_not_move_custody(stack) -> None:
    """等待决策（intervention_required）不是结算：托管保持原样，结算后才转换。"""

    service, path, intervals, claims, epoch = stack
    task = make_task(service, ("robot",))
    load = tenancy_request(service, task, 0, str(uuid4()), acquire="reactor")
    start(service, intervals, claims, epoch, load)
    service.mark_workflow_node_job_decision_pending(load.owner_uuid, {"decision_id": "d-1", "options": []})
    intervals.release_job(load.owner_uuid)
    assert read_state(path, load.owner_uuid)["tenancies"] == {"acquire_uuid": "reserved"}
    service.record_workflow_node_job_terminal(load.owner_uuid, status="succeeded", return_info={}, error_info=[])
    assert read_state(path, load.owner_uuid)["tenancies"] == {"acquire_uuid": "loaded"}


# ── 调度器：终态之后不再有单独的托管 / 区间事务 ───────────────────


class Executor:
    def __init__(self) -> None:
        self.dispatched: List[Dict[str, Any]] = []
        self.callback = None

    def add_job_finished_listener(self, callback) -> None:
        self.callback = callback

    def dispatch(self, payload) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_scheduler_terminal_commit_already_carries_the_derived_release(tmp_path, monkeypatch) -> None:
    """在调度器原本的第二步（_release_job_resources）开始的那一刻，独立连接已经读到收敛结果；
    即使这一步因崩溃没有运行，重新打开库也没有不一致。"""

    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    seen: List[Dict[str, Any]] = []

    def crashed_before_release(self, job_uuid: str, *, canceled: bool) -> None:
        seen.append(read_state(path, job_uuid))

    monkeypatch.setattr(BackendScheduler, "_release_job_resources", crashed_before_release)
    try:
        task = make_task(service, ("reactor",))
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        job_uuid = executor.dispatched[0]["job_id"]
        assert read_state(path, job_uuid)["uses"] == 1
        executor.callback(job_uuid, True, {}, "normal", {})
        wait_for(lambda: bool(seen))
        assert seen[0] == {"status": "succeeded", "transition": None, "tenancies": {}, "uses": 0,
                           "admission": "released", "claim": "released"}
    finally:
        scheduler.stop()
        service.close()
    reopened = WorkflowService(path)
    try:
        assert read_state(path, job_uuid)["uses"] == 0
        assert ResourceIntervals(reopened).snapshot() == []
    finally:
        reopened.close()
