"""Edge 本地模式持久执行占用、等待与重启不确定性合同。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    MATERIAL_UUID,
    NODE_UUID,
    TASK_UUID,
    WORKFLOW_UUID,
    _seed_task,
)
from unilabos.app.scheduler.dispatch import CancelDispatchState, RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.execution_claim import get_execution_claim
from unilabos.workflow.execution_lock_lease import (
    mirror_execution_locks_from_permit,
    normalize_execution_lock_requests,
    try_acquire_execution_locks,
)
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_material_admission import record_admitted_materials
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

SECOND_TASK_UUID = "21000000-0000-4000-8000-000000000002"
SECOND_NODE_UUID = "31000000-0000-4000-8000-000000000002"
SECOND_JOB_UUID = "41000000-0000-4000-8000-000000000002"
RELEASE_NODE_UUID = "31000000-0000-4000-8000-000000000003"
RELEASE_JOB_UUID = "41000000-0000-4000-8000-000000000003"
SECOND_RELEASE_NODE_UUID = "31000000-0000-4000-8000-000000000004"
SECOND_RELEASE_JOB_UUID = "41000000-0000-4000-8000-000000000004"
SITE_UUID = "71000000-0000-4000-8000-000000000001"
_CREATED_AT = "2026-08-05T00:00:01Z"


class _AcceptingCancelDispatcher(RecordingDispatcher):
    """记录派发并同步确认本地执行器已接受取消。"""

    def cancel(self, job_id, on_accepted):
        """确认取消已受理，但不生成设备终态。"""

        del job_id
        on_accepted(True)
        return CancelDispatchState.REQUESTED


class _SilentCancelDispatcher(RecordingDispatcher):
    """记录取消请求，但模拟执行器永不返回受理结果。"""

    def cancel(self, job_id, on_accepted):
        """保持取消悬挂，用于验证本地 ACK 截止时间。"""

        del job_id, on_accepted
        return CancelDispatchState.REQUESTED


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离本地工作流权威。"""

    opened = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "lock_key": "/devices/reactor-a",
            "scope": "device",
            "material_uuid": "reactor-b",
        },
        {
            "lock_key": "material/material-a/exclusive",
            "scope": "material",
            "material_uuid": "material-b",
        },
        {
            "lock_key": "material/owner-a/site/site-a/exclusive",
            "scope": "material_site",
            "site_uuid": "site-b",
        },
    ],
)
def test_workflow_lock_descriptor_rejects_identity_mismatch(
    descriptor: dict[str, str],
) -> None:
    """三类物理描述显式身份与键不一致时均关闭失败。"""

    with pytest.raises(StoreConflict, match="物理身份.*lock_key"):
        normalize_execution_lock_requests([descriptor])


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "lock_key": "/devices/reactor-a",
            "scope": "device",
            "site_uuid": "unexpected-site",
        },
        {
            "lock_key": "material/material-a/exclusive",
            "scope": "material",
            "site_uuid": "unexpected-site",
        },
    ],
)
def test_workflow_non_site_lock_rejects_explicit_site_identity(
    descriptor: dict[str, str],
) -> None:
    """设备和整物料键不能携带键内不存在的 Site 身份。"""

    with pytest.raises(StoreConflict, match="物理身份.*lock_key"):
        normalize_execution_lock_requests([descriptor])


def test_workflow_lock_descriptor_fills_physical_identity_from_canonical_key() -> None:
    """兼容本地调用：省略描述字段时从严格规范键补齐可信物理身份。"""

    normalized = normalize_execution_lock_requests(
        [
            {"lock_key": "/devices/reactor-a", "scope": "device"},
            {
                "lock_key": "material/material-a/exclusive",
                "scope": "material",
            },
            {
                "lock_key": "material/owner-a/site/site-a/exclusive",
                "scope": "material_site",
            },
        ]
    )

    by_key = {request.lock_key: request for request in normalized}
    assert by_key["/devices/reactor-a"].material_uuid == "reactor-a"
    assert by_key["/devices/reactor-a"].site_uuid is None
    assert by_key["material/material-a/exclusive"].material_uuid == "material-a"
    assert by_key["material/material-a/exclusive"].site_uuid is None
    assert by_key[
        "material/owner-a/site/site-a/exclusive"
    ].material_uuid == "owner-a"
    assert by_key["material/owner-a/site/site-a/exclusive"].site_uuid == "site-a"


def _seed_second_task(
    store: WorkflowStore,
    *,
    device_id: str = "reactor-b",
) -> None:
    """追加一个竞争同一物料子库位的独立任务与作业。"""

    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": SECOND_NODE_UUID,
                "kind": "device_action",
                "device_id": device_id,
                "action_name": "place",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": False,
                },
                "material_requirements": [],
            }
        ],
        "handles": [],
        "edges": [],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (
                SECOND_TASK_UUID,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(execution_plan),
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (
                SECOND_JOB_UUID,
                _CREATED_AT,
                _CREATED_AT,
                SECOND_TASK_UUID,
                SECOND_NODE_UUID,
            ),
        )


def _seed_release_job(
    store: WorkflowStore,
    *,
    task_uuid: str,
    node_uuid: str,
    job_uuid: str,
    topological_index: int = 1,
) -> None:
    """在既有任务中追加一个负责结束访问区域的物料转移作业。

    参数：存储、父任务、节点和作业身份共同确定同一执行计划内的释放边界；
    ``topological_index`` 表示释放节点晚于入口节点。返回无。异常：底层 SQLite
    写入失败原样传播。测试只复用既有作业表，不创建新的访问区域专用表。
    """

    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                      'material_transfer', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (
                job_uuid,
                _CREATED_AT,
                _CREATED_AT,
                task_uuid,
                node_uuid,
                topological_index,
            ),
        )


def _project_authoritative_shared_continuation(
    store: WorkflowStore,
) -> tuple[TaskRuntimeProjection, dict[str, str], dict[str, str]]:
    """建立“共享区间资源 + 当前 Job 新增资源”的权威 Permit 场景。"""

    _seed_task(store, with_material=False)
    _seed_release_job(
        store,
        task_uuid=TASK_UUID,
        node_uuid=SECOND_NODE_UUID,
        job_uuid=SECOND_JOB_UUID,
    )
    projection = TaskRuntimeProjection(store)
    shared = {"lock_key": "/devices/reactor-a", "scope": "device"}
    own = {"lock_key": "/devices/reactor-b", "scope": "device"}
    interval_id = "interval-authoritative-shared"
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[shared],
        resource_plan_id="plan-authoritative-shared",
        resource_interval_ids=[interval_id],
        resource_interval_ids_by_lock={shared["lock_key"]: [interval_id]},
    )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=[shared, own],
        dispatch_permit={
            "effect_uuid": "61000000-0000-4000-8000-000000000011",
            "claim_uuid": "51000000-0000-4000-8000-000000000011",
            "parameter_hash": "authoritative-shared-continuation",
            "expected_change_set": {},
            "fences": [
                {"lock_key": shared["lock_key"], "fencing_token": 1},
                {"lock_key": own["lock_key"], "fencing_token": 22},
            ],
        },
        resource_plan_id="plan-authoritative-shared",
        resource_interval_ids=[interval_id],
        resource_interval_ids_by_lock={shared["lock_key"]: [interval_id]},
        preheld_lock_keys=[shared["lock_key"]],
        preheld_job_uuids=[JOB_UUID],
    )
    return projection, shared, own


def test_authoritative_claim_requires_persisted_fence_snapshot(
    store: WorkflowStore,
) -> None:
    """权威 Permit Job 的完整 Fence 快照缺失时必须关闭失败。"""

    _projection, _shared, _own = _project_authoritative_shared_continuation(store)
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT control_data FROM workflow_node_job WHERE uuid=?",
            (SECOND_JOB_UUID,),
        ).fetchone()
        assert row is not None
        control_data = json.loads(row["control_data"])
        control_data.pop("dispatch_fences")
        connection.execute(
            "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
            (json.dumps(control_data), SECOND_JOB_UUID),
        )

    with pytest.raises(StoreConflict, match="缺少权威 Fence"):
        with store.transaction() as connection:
            get_execution_claim(connection, job_uuid=SECOND_JOB_UUID)


def test_active_authoritative_claim_requires_every_owned_new_resource_lease(
    store: WorkflowStore,
) -> None:
    """活动 Job 的新增资源 Lease 丢失时不得只凭 Fence 快照继续派发。"""

    _projection, _shared, own = _project_authoritative_shared_continuation(store)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_lock_lease SET deleted_at=? "
            "WHERE workflow_node_job_uuid=? AND lock_key=?",
            (_CREATED_AT, SECOND_JOB_UUID, own["lock_key"]),
        )

    with pytest.raises(StoreConflict, match="自有新增资源 Lease"):
        with store.transaction() as connection:
            get_execution_claim(connection, job_uuid=SECOND_JOB_UUID)


def test_authoritative_claim_allows_preheld_resource_without_current_job_lease(
    store: WorkflowStore,
) -> None:
    """共享区间的 preheld 键可由活动前驱持有，但 Claim 仍返回完整 Fence。"""

    projection, shared, own = _project_authoritative_shared_continuation(store)
    claim = projection.get_execution_claim(SECOND_JOB_UUID)
    assert claim is not None
    assert {item["lock_key"] for item in claim["fences"]} == {
        shared["lock_key"],
        own["lock_key"],
    }
    assert {
        lease["lock_key"] for lease in projection.list_execution_locks(SECOND_JOB_UUID)
    } == {own["lock_key"]}


def test_active_authoritative_claim_requires_active_preheld_workflow_lease(
    store: WorkflowStore,
) -> None:
    """活动 Job 的 preheld 键必须仍由声明前驱的活动工作流 Lease 证明。"""

    projection, shared, _own = _project_authoritative_shared_continuation(store)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_lock_lease SET state='released', "
            "released_at=update_time WHERE workflow_node_job_uuid=? AND lock_key=?",
            (JOB_UUID, shared["lock_key"]),
        )

    with pytest.raises(StoreConflict, match="preheld.*活动前驱 Lease"):
        projection.require_dispatchable_execution_claim(SECOND_JOB_UUID)


def test_terminal_authoritative_claim_reads_stable_fence_snapshot_without_leases(
    store: WorkflowStore,
) -> None:
    """Job 终态后即使 Lease 已清理，审计读取仍返回原始完整 Fence。"""

    projection, shared, own = _project_authoritative_shared_continuation(store)
    before = projection.get_execution_claim(SECOND_JOB_UUID)
    assert before is not None
    projection.project_dispatch_accepted(SECOND_JOB_UUID)
    projection.project_job_finished(job_uuid=SECOND_JOB_UUID, scheduler_state="success")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_lock_lease SET deleted_at=? WHERE workflow_node_job_uuid=?",
            (_CREATED_AT, SECOND_JOB_UUID),
        )

    after = projection.get_execution_claim(SECOND_JOB_UUID)
    assert after is not None
    assert after["fences"] == before["fences"]
    assert {item["lock_key"] for item in after["fences"]} == {
        shared["lock_key"],
        own["lock_key"],
    }


def _access_region_request(*, release_job_uuid: str) -> dict[str, str]:
    """构造 Backend 业务语义一致的跨节点访问区域占用请求。

    参数：``release_job_uuid`` 是真正释放长锁的后续物料转移作业。返回：不依赖
    SQLite 表名的公开锁请求。异常：无；固定 UUID 与规范小写区域键均由测试控制。
    """

    region_key = "s08-s09-reagent-corridor"
    return {
        "lock_key": f"access_region/{MATERIAL_UUID}/{region_key}",
        "scope": "access_region",
        "material_uuid": MATERIAL_UUID,
        "access_region_key": region_key,
        "lease_owner_job_uuid": release_job_uuid,
    }


def test_execution_lock_rejects_plc_access_region_scope(
    store: WorkflowStore,
) -> None:
    """持久资源门禁不得接受由 PLC 负责的访问区域软件锁。

    参数：``store`` 是隔离工作流权威。返回无；断言旧作用域失败关闭且 Job 保持
    pending。异常：旧访问区域进入租约表会使测试失败。
    """

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    with pytest.raises(StoreConflict, match="scope"):
        projection.project_pre_dispatch(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            execution_locks=[_access_region_request(release_job_uuid=RELEASE_JOB_UUID)],
        )
    assert store.get_job(JOB_UUID)["status"] == "pending"


def test_cleanup_settled_releases_regular_task_claim(
    store: WorkflowStore,
) -> None:
    """异常任务完成物理清理时必须幂等回收普通资源 Claim。

    参数：``store`` 是隔离工作流权威。返回无。异常：清理状态和 Claim 释放不在
    同一事务闭环时由断言暴露。
    """

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state="failed")

    projection.project_cleanup_settled(TASK_UUID)

    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    release_states = {
        item["state"] for item in projection.list_execution_locks(JOB_UUID)
    }
    assert release_states == {
        "released"
    }


def test_resource_interval_handoff_keeps_claim_across_jobs(
    store: WorkflowStore,
) -> None:
    """连续区间完成前一 Job 后，Claim 必须转移到后继 Job。"""

    _seed_task(store, with_material=False)
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(
                {
                    "workflow_uuid": WORKFLOW_UUID,
                    "nodes": [
                        {"uuid": NODE_UUID, "resource_defaults": ["robot"]},
                        {"uuid": SECOND_NODE_UUID, "resource_defaults": ["robot"]},
                    ],
                    "edges": [
                        {
                            "source_node_uuid": NODE_UUID,
                            "target_node_uuid": SECOND_NODE_UUID,
                        }
                    ],
                }
            ),
            {
                "robot": {
                    "canonical_key": "/devices/reactor-a",
                    "kind": "device",
                }
            },
        )
    )
    first_interval = str(plan["intervals"][0]["interval_id"])
    second_acquire = next(
        item for item in plan["acquire_sets"] if item["node_uuid"] == NODE_UUID
    )
    with store.transaction() as connection:
        execution_plan = json.loads(
            connection.execute(
                "SELECT execution_plan FROM workflow_task WHERE uuid=?",
                (TASK_UUID,),
            ).fetchone()[0]
        )
        execution_plan["resource_plan"] = plan
        execution_plan["nodes"].append(
            {
                **execution_plan["nodes"][0],
                "uuid": SECOND_NODE_UUID,
                "resource_plan_id": plan["plan_id"],
                "resource_interval_ids": [first_interval],
                "resource_acquire_set_id": second_acquire["acquire_set_id"],
            }
        )
        execution_plan["nodes"][0].update(
            {
                "resource_plan_id": plan["plan_id"],
                "resource_interval_ids": [first_interval],
                "resource_acquire_set_id": next(
                    item
                    for item in plan["acquire_sets"]
                    if item["node_uuid"] == NODE_UUID
                )["acquire_set_id"],
            }
        )
        execution_plan["edges"].append(
            {
                "uuid": "interval-handoff-edge",
                "source_node_uuid": NODE_UUID,
                "target_node_uuid": SECOND_NODE_UUID,
                "dependency_only": True,
            }
        )
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(execution_plan), TASK_UUID),
        )
    _seed_release_job(
        store,
        task_uuid=TASK_UUID,
        node_uuid=SECOND_NODE_UUID,
        job_uuid=SECOND_JOB_UUID,
    )
    projection = TaskRuntimeProjection(store)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    transient_lock = {
        "lock_key": "/devices/transient-inspector",
        "scope": "device",
    }
    first = projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock, transient_lock],
        resource_plan_id=str(plan["plan_id"]),
        resource_interval_ids=[first_interval],
        resource_interval_ids_by_lock={lock["lock_key"]: [first_interval]},
        resource_acquire_set_id=str(
            next(
                item
                for item in plan["acquire_sets"]
                if item["node_uuid"] == NODE_UUID
            )["acquire_set_id"]
        ),
    )
    assert first["jobs"][0]["status"] == "dispatched"
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state="success")

    with store.transaction() as connection:
        lease = connection.execute(
            "SELECT * FROM execution_lock_lease WHERE lock_key=? AND state != 'released'",
            (lock["lock_key"],),
        ).fetchone()
        assert lease is not None
        assert lease["workflow_node_job_uuid"] == JOB_UUID
        transient = connection.execute(
            "SELECT state FROM execution_lock_lease WHERE lock_key=?",
            (transient_lock["lock_key"],),
        ).fetchone()
        assert transient is not None and transient["state"] == "released"

    second = projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=[lock],
        dispatch_permit={
            "effect_uuid": "61000000-0000-4000-8000-000000000001",
            "claim_uuid": "51000000-0000-4000-8000-000000000004",
            "parameter_hash": "interval-handoff",
            "expected_change_set": {},
            "fences": [
                {
                    "lock_key": lock["lock_key"],
                    "fencing_token": 2,
                }
            ],
        },
        resource_plan_id=str(plan["plan_id"]),
        resource_interval_ids=[first_interval],
        resource_interval_ids_by_lock={lock["lock_key"]: [first_interval]},
        resource_acquire_set_id=str(second_acquire["acquire_set_id"]),
        preheld_lock_keys=[lock["lock_key"]],
        preheld_job_uuids=[JOB_UUID],
    )
    assert second["jobs"][-1]["status"] == "dispatched"
    with store.transaction() as connection:
        lease = connection.execute(
            "SELECT * FROM execution_lock_lease WHERE lock_key=? AND state != 'released'",
            (lock["lock_key"],),
        ).fetchone()
        claim = get_execution_claim(connection, job_uuid=SECOND_JOB_UUID)
        old_claim = get_execution_claim(connection, job_uuid=JOB_UUID)
        assert lease is not None and lease["workflow_node_job_uuid"] == SECOND_JOB_UUID
        assert claim is not None
        assert old_claim is not None and old_claim["state"] == "released"

    projection.project_job_finished(job_uuid=SECOND_JOB_UUID, scheduler_state="success")
    assert {
        lease["state"]
        for lease in projection.list_execution_locks(SECOND_JOB_UUID)
    } == {"released"}


@pytest.mark.parametrize("scheduler_state", ["failed", "canceled"])
def test_resource_interval_failure_or_cancel_preserves_uncertain_lease(
    store: WorkflowStore,
    scheduler_state: str,
) -> None:
    """连续区间中失败/取消不能把可能仍在物理现场的 Lease 自动释放。"""

    _seed_task(store, with_material=False)
    interval_id = "interval-uncertain"
    with store.transaction() as connection:
        execution_plan = json.loads(
            connection.execute(
                "SELECT execution_plan FROM workflow_task WHERE uuid=?",
                (TASK_UUID,),
            ).fetchone()[0]
        )
        execution_plan["resource_plan"] = {
            "plan_id": "plan-uncertain",
            "intervals": [
                {
                    "interval_id": interval_id,
                    "node_uuids": [NODE_UUID],
                    "release_node_uuid": RELEASE_NODE_UUID,
                }
            ],
        }
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(execution_plan), TASK_UUID),
        )

    projection = TaskRuntimeProjection(store)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock],
        resource_plan_id="plan-uncertain",
        resource_interval_ids=[interval_id],
        resource_interval_ids_by_lock={lock["lock_key"]: [interval_id]},
        resource_acquire_set_id="acquire-uncertain",
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET expected_change_set=? WHERE uuid=?",
            (json.dumps({"kind": "material_transfer"}), JOB_UUID),
        )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_job_finished(
        job_uuid=JOB_UUID,
        scheduler_state=scheduler_state,
        return_info={"device_state": "unknown"},
    )

    job = store.get_job(JOB_UUID)
    assert job["uncertainty_reason"]
    assert {
        lease["state"]
        for lease in projection.list_execution_locks(JOB_UUID)
        if lease["state"] != "released"
    } == {"uncertain"}


def test_material_parent_lock_blocks_child_site_until_explicit_result(
    store: WorkflowStore,
) -> None:
    """整物料占用跨任务阻止其子库位，明确结果后按原等待身份重试。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store)
    projection = TaskRuntimeProjection(store)
    whole_material = f"material/{MATERIAL_UUID}/exclusive"
    child_site = f"material/{MATERIAL_UUID}/site/{SITE_UUID}/exclusive"

    first = projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
            {
                "lock_key": whole_material,
                "scope": "material",
                "material_uuid": MATERIAL_UUID,
            },
        ],
    )
    assert first["jobs"][0]["status"] == "dispatched"
    projection.project_dispatch_accepted(JOB_UUID)

    blocked = projection.project_pre_dispatch(
        task_uuid=SECOND_TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-b", "scope": "device"},
            {
                "lock_key": child_site,
                "scope": "material_site",
                "material_uuid": MATERIAL_UUID,
                "site_uuid": SITE_UUID,
            },
        ],
    )
    blocked_job = blocked["jobs"][0]
    assert blocked_job["status"] == "pending"
    assert blocked["task"]["status"] == "pending"
    assert blocked_job["wait_reason"]["code"] == "operation_lease"
    assert blocked_job["wait_reason"]["blocking_job_uuid"] == JOB_UUID
    assert blocked_job["wait_reason"]["resources"] == [
        {"scope": "device", "device_id": "reactor-b"},
        {
            "scope": "material_site",
            "material_uuid": MATERIAL_UUID,
            "site_uuid": SITE_UUID,
        },
    ]
    with store.transaction() as connection:
        waiting_count = connection.execute(
            "SELECT COUNT(*) FROM execution_lock_waiter WHERE state = 'waiting'"
        ).fetchone()[0]
    assert waiting_count == 2

    projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state="success")
    released = projection.list_execution_locks(JOB_UUID)
    assert {lease["state"] for lease in released} == {"released"}

    admitted = projection.project_pre_dispatch(
        task_uuid=SECOND_TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-b", "scope": "device"},
            {
                "lock_key": child_site,
                "scope": "material_site",
                "material_uuid": MATERIAL_UUID,
                "site_uuid": SITE_UUID,
            },
        ],
    )
    assert admitted["jobs"][0]["status"] == "dispatched"
    assert admitted["jobs"][0]["wait_reason"] == {}


def test_malformed_active_lease_key_fails_closed_before_new_acquisition(
    store: WorkflowStore,
) -> None:
    """损坏活动 Lease 不能退化成字符串相等比较并静默绕过层级互斥。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[{"lock_key": "/devices/reactor-a", "scope": "device"}],
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_lock_lease SET lock_key='material//exclusive' "
            "WHERE workflow_node_job_uuid=?",
            (JOB_UUID,),
        )

    with pytest.raises(StoreConflict, match="活动执行锁租约"):
        projection.project_pre_dispatch(
            task_uuid=SECOND_TASK_UUID,
            job_uuid=SECOND_JOB_UUID,
            execution_locks=[
                {"lock_key": "/devices/reactor-b", "scope": "device"}
            ],
        )


@pytest.mark.parametrize(
    ("lock", "identity_field"),
    [
        (
            {
                "lock_key": f"material/{MATERIAL_UUID}/exclusive",
                "scope": "material",
            },
            "material_uuid",
        ),
        (
            {
                "lock_key": (
                    f"material/{MATERIAL_UUID}/site/{SITE_UUID}/exclusive"
                ),
                "scope": "material_site",
            },
            "site_uuid",
        ),
    ],
)
def test_active_lease_rejects_whitespace_in_persisted_physical_identity(
    store: WorkflowStore,
    lock: dict[str, str],
    identity_field: str,
) -> None:
    """活动 Lease 的原始物料或库位身份不规范时必须关闭失败。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock],
    )
    with store.transaction() as connection:
        if identity_field == "material_uuid":
            connection.execute(
                "UPDATE execution_lock_lease SET material_uuid=? "
                "WHERE workflow_node_job_uuid=?",
                (f" {MATERIAL_UUID} ", JOB_UUID),
            )
        else:
            connection.execute(
                "UPDATE execution_lock_lease SET site_uuid=? "
                "WHERE workflow_node_job_uuid=?",
                (f" {SITE_UUID} ", JOB_UUID),
            )

    with pytest.raises(StoreConflict, match="活动执行锁租约"):
        projection.project_pre_dispatch(
            task_uuid=SECOND_TASK_UUID,
            job_uuid=SECOND_JOB_UUID,
            execution_locks=[
                {"lock_key": "/devices/reactor-b", "scope": "device"}
            ],
        )


def test_authoritative_permit_replay_rejects_corrupt_lease_identity(
    store: WorkflowStore,
) -> None:
    """库存 Permit 重放也不能接受键、scope 与冗余身份互相矛盾的镜像。"""

    _seed_task(store, with_material=False)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    claim_uuid = "51000000-0000-4000-8000-000000000099"
    with store.transaction() as connection:
        mirror_execution_locks_from_permit(
            connection,
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            requests=[lock],
            claim_uuid=claim_uuid,
            fencing_tokens={lock["lock_key"]: 41},
        )
        connection.execute(
            "UPDATE execution_lock_lease SET material_uuid='other-device' "
            "WHERE workflow_node_job_uuid=?",
            (JOB_UUID,),
        )

    with pytest.raises(StoreConflict, match="活动执行锁租约"):
        with store.transaction() as connection:
            mirror_execution_locks_from_permit(
                connection,
                task_uuid=TASK_UUID,
                job_uuid=JOB_UUID,
                requests=[lock],
                claim_uuid=claim_uuid,
                fencing_tokens={lock["lock_key"]: 41},
            )


def test_workflow_store_backfills_legacy_active_lease_identity(
    tmp_path: Path,
) -> None:
    """升级旧库时从规范键补齐空物理身份，避免合法活动 Lease 被误判损坏。"""

    database_path = tmp_path / "legacy_execution_lock.db"
    opened = WorkflowStore(database_path)
    try:
        _seed_task(opened, with_material=False)
        TaskRuntimeProjection(opened).project_pre_dispatch(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            execution_locks=[
                {"lock_key": "/devices/reactor-a", "scope": "device"}
            ],
        )
        with opened.transaction() as connection:
            connection.execute(
                "UPDATE execution_lock_lease SET material_uuid=NULL "
                "WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            )
    finally:
        opened.close()

    reopened = WorkflowStore(database_path)
    try:
        with reopened.transaction() as connection:
            row = connection.execute(
                "SELECT material_uuid,site_uuid FROM execution_lock_lease "
                "WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            ).fetchone()
        assert row is not None
        assert row["material_uuid"] == "reactor-a"
        assert row["site_uuid"] is None
    finally:
        reopened.close()


def test_task_material_claim_blocks_foreign_local_action_without_source(
    store: WorkflowStore,
) -> None:
    """本地准入也必须按动作实际物料拒绝越过其他 Task 的任务级独占。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store)
    with store.transaction() as connection:
        owner_job = connection.execute(
            "SELECT * FROM workflow_node_job WHERE uuid=?",
            (JOB_UUID,),
        ).fetchone()
        assert owner_job is not None
        record_admitted_materials(
            connection,
            task_uuid=TASK_UUID,
            source_jobs=(owner_job,),
            bindings={
                NODE_UUID: {
                    "material_uuid": MATERIAL_UUID,
                    "resource_template_uuid": (
                        "61000000-0000-4000-8000-000000000001"
                    ),
                    "site_uuid": None,
                    "flow_role": "reagent",
                    "custody_policy": "task_exclusive",
                }
            },
        )

        decision = try_acquire_execution_locks(
            connection,
            task_uuid=SECOND_TASK_UUID,
            job_uuid=SECOND_JOB_UUID,
            requests=(
                {
                    "lock_key": f"material/{MATERIAL_UUID}/exclusive",
                    "scope": "material",
                    "material_uuid": MATERIAL_UUID,
                },
            ),
        )

    assert decision.acquired is False
    assert decision.blocking_task_uuid == TASK_UUID
    assert decision.blocking_job_uuid is None


def test_foreign_local_job_active_use_blocks_new_task_material_claim(
    store: WorkflowStore,
) -> None:
    """本地写模型的反向竞态也关闭：先有外国动作时不能再建立任务独占。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store)
    with pytest.raises(StoreConflict, match="正在被其他任务的动作使用"):
        with store.transaction() as connection:
            active = try_acquire_execution_locks(
                connection,
                task_uuid=SECOND_TASK_UUID,
                job_uuid=SECOND_JOB_UUID,
                requests=(
                    {
                        "lock_key": f"material/{MATERIAL_UUID}/exclusive",
                        "scope": "material",
                        "material_uuid": MATERIAL_UUID,
                    },
                ),
            )
            assert active.acquired is True
            owner_job = connection.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=?",
                (JOB_UUID,),
            ).fetchone()
            assert owner_job is not None
            record_admitted_materials(
                connection,
                task_uuid=TASK_UUID,
                source_jobs=(owner_job,),
                bindings={
                    NODE_UUID: {
                        "material_uuid": MATERIAL_UUID,
                        "resource_template_uuid": (
                            "61000000-0000-4000-8000-000000000001"
                        ),
                        "site_uuid": None,
                        "flow_role": "reagent",
                        "custody_policy": "task_exclusive",
                    }
                },
            )


def test_inventory_permit_is_mirrored_without_second_workflow_arbitration(
    store: WorkflowStore,
) -> None:
    """工作流库只保留库存 Permit 审计镜像，不建立第二套仲裁结果。"""

    _seed_task(store, with_material=False)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    claim_uuid = "51000000-0000-4000-8000-000000000001"
    with store.transaction() as connection:
        decision = mirror_execution_locks_from_permit(
            connection,
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            requests=[lock],
            claim_uuid=claim_uuid,
            fencing_tokens={lock["lock_key"]: 37},
        )
        waiters = connection.execute(
            "SELECT COUNT(*) FROM execution_lock_waiter WHERE workflow_node_job_uuid=?",
            (JOB_UUID,),
        ).fetchone()[0]
        metadata = json.loads(
            connection.execute(
                "SELECT meta_data FROM execution_lock_lease WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            ).fetchone()[0]
        )

    assert decision.acquired is True
    assert decision.claim_uuid == claim_uuid
    assert decision.fencing_tokens == ((lock["lock_key"], 37),)
    assert waiters == 0
    assert metadata["authority"] == "inventory_dispatch_permit"


def test_authoritative_mirror_rejects_missing_preheld_predecessor_lease(
    store: WorkflowStore,
) -> None:
    """Permit 声明预持有时，工作流库必须拒绝不存在的前驱 Lease。"""

    _seed_task(store, with_material=False)
    _seed_release_job(
        store,
        task_uuid=TASK_UUID,
        node_uuid=SECOND_NODE_UUID,
        job_uuid=SECOND_JOB_UUID,
    )
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    interval_id = "interval-missing-preheld"

    with pytest.raises(StoreConflict, match="预持有"):
        with store.transaction() as connection:
            mirror_execution_locks_from_permit(
                connection,
                task_uuid=TASK_UUID,
                job_uuid=SECOND_JOB_UUID,
                requests=[lock],
                claim_uuid="51000000-0000-4000-8000-000000000002",
                fencing_tokens={lock["lock_key"]: 11},
                resource_plan_id="plan-missing-preheld",
                resource_interval_ids=[interval_id],
                resource_interval_ids_by_lock={lock["lock_key"]: [interval_id]},
                preheld_lock_keys=[lock["lock_key"]],
                preheld_job_uuids=[JOB_UUID],
            )

    with store.transaction() as connection:
        assert get_execution_claim(connection, job_uuid=SECOND_JOB_UUID) is None


def test_authoritative_mirror_rejects_released_preheld_predecessor_lease(
    store: WorkflowStore,
) -> None:
    """已释放的前驱 Lease 不得被 Permit 当作仍连续持有。"""

    _seed_task(store, with_material=False)
    _seed_release_job(
        store,
        task_uuid=TASK_UUID,
        node_uuid=SECOND_NODE_UUID,
        job_uuid=SECOND_JOB_UUID,
    )
    projection = TaskRuntimeProjection(store)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}
    interval_id = "interval-released-preheld"
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock],
        resource_plan_id="plan-released-preheld",
        resource_interval_ids=[interval_id],
        resource_interval_ids_by_lock={lock["lock_key"]: [interval_id]},
    )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state="success")
    assert {
        lease["state"] for lease in projection.list_execution_locks(JOB_UUID)
    } == {"released"}

    with pytest.raises(StoreConflict, match="预持有"):
        with store.transaction() as connection:
            mirror_execution_locks_from_permit(
                connection,
                task_uuid=TASK_UUID,
                job_uuid=SECOND_JOB_UUID,
                requests=[lock],
                claim_uuid="51000000-0000-4000-8000-000000000003",
                fencing_tokens={lock["lock_key"]: 12},
                resource_plan_id="plan-released-preheld",
                resource_interval_ids=[interval_id],
                resource_interval_ids_by_lock={lock["lock_key"]: [interval_id]},
                preheld_lock_keys=[lock["lock_key"]],
                preheld_job_uuids=[JOB_UUID],
            )

    with store.transaction() as connection:
        assert get_execution_claim(connection, job_uuid=SECOND_JOB_UUID) is None


def test_claim_is_stable_per_attempt_and_fence_increases_per_resource(
    store: WorkflowStore,
) -> None:
    """同一 Job 尝试重放复用 Claim，后续 Job 获得更大的资源栅栏。

    参数：``store`` 是隔离工作流权威。返回无；断言 Claim UUID、完整资源集合和
    Fence 单调性。异常：派发重放生成新身份或资源复用未递增栅栏会使测试失败。
    """

    _seed_task(store, with_material=False)
    _seed_second_task(store, device_id="reactor-a")
    projection = TaskRuntimeProjection(store)
    lock = {"lock_key": "/devices/reactor-a", "scope": "device"}

    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock],
    )
    with store.transaction() as connection:
        first = get_execution_claim(connection, job_uuid=JOB_UUID)
    assert first is not None
    assert first["resource_keys"] == ["/devices/reactor-a"]
    assert first["fences"][0]["fencing_token"] == 1

    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[lock],
    )
    with store.transaction() as connection:
        replay = get_execution_claim(connection, job_uuid=JOB_UUID)
    assert replay == first

    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state="success")
    projection.project_pre_dispatch(
        task_uuid=SECOND_TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=[lock],
    )
    with store.transaction() as connection:
        second = get_execution_claim(connection, job_uuid=SECOND_JOB_UUID)
    assert second is not None
    assert second["claim_uuid"] != first["claim_uuid"]
    assert second["fences"][0]["fencing_token"] == 2


def test_persistent_waiter_allows_higher_priority_task_to_pass(
    store: WorkflowStore,
) -> None:
    """较早排队的低优先级作业不能反向阻塞新到的高优先级作业。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store, device_id="reactor-a")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET priority=1 WHERE uuid=?",
            (TASK_UUID,),
        )
        connection.execute(
            "UPDATE workflow_task SET priority=10 WHERE uuid=?",
            (SECOND_TASK_UUID,),
        )
    projection = TaskRuntimeProjection(store)
    lock = [{"lock_key": "/devices/reactor-a", "scope": "device"}]
    projection.project_execution_lock_wait(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=lock,
    )

    admitted = projection.project_pre_dispatch(
        task_uuid=SECOND_TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=lock,
    )

    assert admitted["jobs"][0]["status"] == "dispatched"


def test_persistent_waiter_aging_eventually_beats_fresh_priority(
    store: WorkflowStore,
) -> None:
    """低优先级等待者经过足够老化后阻止新到高优先级作业插队。"""

    _seed_task(store, with_material=False)
    _seed_second_task(store, device_id="reactor-a")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET priority=1 WHERE uuid=?",
            (TASK_UUID,),
        )
        connection.execute(
            "UPDATE workflow_task SET priority=10 WHERE uuid=?",
            (SECOND_TASK_UUID,),
        )
    projection = TaskRuntimeProjection(store)
    lock = [{"lock_key": "/devices/reactor-a", "scope": "device"}]
    projection.project_execution_lock_wait(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=lock,
    )
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE execution_lock_waiter
            SET enqueued_at='2026-08-30T00:00:00Z'
            WHERE workflow_node_job_uuid=? AND state='waiting'
            """,
            (JOB_UUID,),
        )

    blocked = projection.project_pre_dispatch(
        task_uuid=SECOND_TASK_UUID,
        job_uuid=SECOND_JOB_UUID,
        execution_locks=lock,
        aging_interval_seconds=30,
    )

    assert blocked["jobs"][0]["status"] == "pending"
    assert blocked["jobs"][0]["wait_reason"]["blocking_job_uuid"] == JOB_UUID


def test_restart_restores_inflight_job_and_retains_lease(
    store: WorkflowStore,
) -> None:
    """调度器重启恢复在途路由和原执行占用，不伪造设备失败或重发动作。"""

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    projection.project_dispatch_accepted(JOB_UUID)

    dispatcher = RecordingDispatcher()
    bridge = TaskSchedulerBridge(
        store,
        scheduler=EdgeScheduler(dispatcher=dispatcher),
    )
    try:
        recovered = bridge.recover_active_tasks()
    finally:
        bridge.close()

    assert [aggregate["task"]["uuid"] for aggregate in recovered] == [TASK_UUID]
    job = store.get_job(JOB_UUID)
    assert job["status"] == "running"
    assert dispatcher.dispatched == []
    task = store.get_task(TASK_UUID)
    assert task["status"] == "running"
    assert not task.get("attention_reason")
    assert {lease["state"] for lease in projection.list_execution_locks(JOB_UUID)} == {
        "running"
    }


def test_restart_projects_edge_committed_outcome_before_marking_job_unknown(
    store: WorkflowStore,
) -> None:
    """Edge 已落盘结果必须先重放，不能被重启扫描误判为 UNKNOWN。"""

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[{"lock_key": "/devices/reactor-a", "scope": "device"}],
    )
    projection.project_dispatch_accepted(JOB_UUID)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())

    def replay(*, feedback_listener, outcome_listener, finished_listener):
        """模拟重启重放一条成功结果；监听器为注入端口，返回重放计数。"""

        del feedback_listener, outcome_listener
        finished_listener(JOB_UUID, True, {"completed": True}, "normal")
        return {"feedback": 0, "outcomes": 1}

    scheduler.replay_persisted_edge_projections = replay  # type: ignore[method-assign]
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        recovered = bridge.recover_active_tasks()
    finally:
        bridge.close()

    assert [item["task"]["uuid"] for item in recovered] == [TASK_UUID]
    assert bridge.error_control.snapshot()["errors"] == []
    assert store.get_job(JOB_UUID)["status"] == "succeeded"
    assert store.get_task(TASK_UUID)["status"] == "succeeded"
    assert {lease["state"] for lease in projection.list_execution_locks(JOB_UUID)} == {
        "released"
    }


def test_live_device_conflict_persists_waiter_before_retry(
    store: WorkflowStore,
) -> None:
    """同进程设备忙也必须写等待原因，不能只依赖易失内存锁。"""

    first_task = _seed_task(store, with_material=False)
    _seed_second_task(store, device_id="reactor-a")
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(first_task)
        blocked = bridge.submit(store.get_task(SECOND_TASK_UUID))
    finally:
        bridge.close()

    blocked_job = next(job for job in blocked["jobs"] if job["uuid"] == SECOND_JOB_UUID)
    assert blocked_job["status"] == "pending"
    assert blocked_job["wait_reason"]["code"] == "operation_lease"
    assert blocked_job["wait_reason"]["blocking_job_uuid"] == JOB_UUID
    with store.transaction() as connection:
        waiting = connection.execute(
            "SELECT lock_key, state FROM execution_lock_waiter "
            "WHERE workflow_node_job_uuid = ?",
            (SECOND_JOB_UUID,),
        ).fetchall()
    assert [(row["lock_key"], row["state"]) for row in waiting] == [
        ("/devices/reactor-a", "waiting")
    ]


def test_local_cancel_keeps_execution_lock_until_device_terminal(
    store: WorkflowStore,
) -> None:
    """取消受理不能提前释放锁，设备取消终态到达后才完成结算。"""

    task = _seed_task(store, with_material=False)
    dispatcher = _AcceptingCancelDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(task)
        requested = bridge.cancel(
            TASK_UUID,
            command_uuid="51000000-0000-4000-8000-000000000001",
        )
        requested_job = next(
            job for job in requested["jobs"] if job["uuid"] == JOB_UUID
        )
        assert requested["task"]["status"] == "canceling"
        assert requested_job["status"] == "cancel_requested"
        assert requested_job["cancel_accepted_at"] is not None
        assert requested_job.get("cancel_ack_deadline_at") is None
        assert requested_job["cancel_complete_deadline_at"] is not None
        assert {
            lease["state"] for lease in bridge._projection.list_execution_locks(JOB_UUID)
        } == {"running"}

        scheduler.on_job_finished(
            JOB_UUID,
            False,
            {"stopped": True},
            "canceled",
        )
        settled = store.get_task(TASK_UUID)
        assert settled["status"] == "canceled"
        assert settled["cleanup_status"] == "settled"
        assert store.get_job(JOB_UUID)["status"] == "canceled"
        assert {
            lease["state"] for lease in bridge._projection.list_execution_locks(JOB_UUID)
        } == {"released"}
    finally:
        bridge.close()


def test_local_cancel_acceptance_timeout_keeps_running_with_uncertain_claim(
    store: WorkflowStore,
) -> None:
    """执行器不确认取消时保持运行主状态并保留不确定占用。

    参数：``store`` 是隔离工作流写模型。返回无；断言超时只改变物理对账事实，
    不创建新的 Job 主状态。异常：计时或投影失败会使测试失败。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=_SilentCancelDispatcher())
    bridge = TaskSchedulerBridge(
        store,
        scheduler=scheduler,
        cancel_ack_timeout_seconds=0.02,
        cancel_complete_timeout_seconds=0.1,
    )
    try:
        bridge.submit(task)
        bridge.cancel(
            TASK_UUID,
            command_uuid="51000000-0000-4000-8000-000000000002",
        )
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if store.get_job(JOB_UUID).get("uncertainty_reason"):
                break
            time.sleep(0.01)
        assert store.get_job(JOB_UUID)["status"] == "running"
        assert store.get_job(JOB_UUID)["uncertainty_reason"] == (
            "local_cancel_acceptance_timeout"
        )
        assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
        assert {
            lease["state"] for lease in bridge._projection.list_execution_locks(JOB_UUID)
        } == {"uncertain"}
    finally:
        bridge.close()


def test_local_cancel_completion_timeout_keeps_uncertain_lock(
    store: WorkflowStore,
) -> None:
    """取消已受理但设备无终态时保持运行，不能提前复用设备。

    参数：``store`` 是隔离工作流写模型。返回无；断言运行主状态与物理不确定原因
    分离持久化。异常：计时或占用状态错误会使测试失败。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=_AcceptingCancelDispatcher())
    bridge = TaskSchedulerBridge(
        store,
        scheduler=scheduler,
        cancel_ack_timeout_seconds=0.01,
        cancel_complete_timeout_seconds=0.03,
    )
    try:
        bridge.submit(task)
        bridge.cancel(
            TASK_UUID,
            command_uuid="51000000-0000-4000-8000-000000000003",
        )
        assert store.get_job(JOB_UUID).get("cancel_accepted_at") is not None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if store.get_job(JOB_UUID).get("uncertainty_reason") == (
                "local_cancel_completion_timeout"
            ):
                break
            time.sleep(0.01)
        job = store.get_job(JOB_UUID)
        assert job["status"] == "running"
        assert job["uncertainty_reason"] == "local_cancel_completion_timeout"
        assert {
            lease["state"] for lease in bridge._projection.list_execution_locks(JOB_UUID)
        } == {"uncertain"}
    finally:
        bridge.close()


@pytest.mark.parametrize("accepted", [False, True])
def test_restart_during_cancel_restores_deadline_and_pauses_on_unknown(
    store: WorkflowStore, accepted: bool,
) -> None:
    """重启保留取消意图和原截止时间；超时生成独立错误，保留在途占用。"""
    task = _seed_task(store, with_material=False)
    first_bridge = TaskSchedulerBridge(
        store,
        scheduler=EdgeScheduler(dispatcher=(
            _AcceptingCancelDispatcher() if accepted else _SilentCancelDispatcher()
        )),
        cancel_ack_timeout_seconds=30.0,
        cancel_complete_timeout_seconds=60.0,
    )
    try:
        first_bridge.submit(task)
        first_bridge.cancel(
            TASK_UUID,
            command_uuid="51000000-0000-4000-8000-000000000004",
        )
        before = store.get_job(JOB_UUID)
        assert before["status"] == "cancel_requested"
        assert first_bridge.error_control.snapshot()["mode"] == "RUNNING"
    finally:
        first_bridge.close()

    timers = []

    class DeferredTimer:
        """仅记录待触发回调；测试主动触发，无真实等待。"""
        def __init__(self, interval, function, *, kwargs):
            self.interval = interval
            self.function = function
            self.kwargs = kwargs
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    dispatcher = RecordingDispatcher()
    recovered_bridge = TaskSchedulerBridge(
        store,
        scheduler=EdgeScheduler(dispatcher=dispatcher),
        timer_factory=DeferredTimer,
    )
    try:
        recovered = recovered_bridge.recover_active_tasks()
        assert [aggregate["task"]["uuid"] for aggregate in recovered] == [TASK_UUID]
        job = store.get_job(JOB_UUID)
        assert job["status"] == "cancel_requested"
        assert dispatcher.dispatched == []
        deadline_field = "cancel_complete_deadline_at" if accepted else "cancel_ack_deadline_at"
        assert job[deadline_field] == before[deadline_field]
        assert len(timers) == 1
        assert 0 < timers[0].interval <= (60 if accepted else 30)
        assert {
            lease["state"]
            for lease in recovered_bridge._projection.list_execution_locks(JOB_UUID)
        } == {"running"}
        timers[0].function(**timers[0].kwargs)
        job = store.get_job(JOB_UUID)
        assert job["status"] == "running"
        assert job["uncertainty_reason"] == (
            "local_cancel_completion_timeout" if accepted else "local_cancel_acceptance_timeout"
        )
        snapshot = recovered_bridge.error_control.snapshot()
        assert snapshot["mode"] == "PAUSED"
        assert snapshot["errors"][0]["error_phase"] == "cancel_unknown"
        assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
        assert {
            lease["state"]
            for lease in recovered_bridge._projection.list_execution_locks(JOB_UUID)
        } == {"uncertain"}
        recovered_bridge._on_job_cancel_uncertain(JOB_UUID, job["uncertainty_reason"])
        assert recovered_bridge.error_control.snapshot()["error_epoch"] == snapshot["error_epoch"]
    finally:
        recovered_bridge.close()


def test_cancel_terminal_wins_before_queued_timeout_without_false_error(
    store: WorkflowStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实取消回执先取得控制屏障时，排队的超时不能再制造 UNKNOWN 错误。"""
    import threading

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=_SilentCancelDispatcher())
    bridge = TaskSchedulerBridge(store, scheduler=scheduler, cancel_ack_timeout_seconds=30)
    read_started = threading.Event()
    allow_read = threading.Event()
    real_get_job = store.get_job

    def delayed_timeout_read(job_uuid: str) -> dict[str, Any]:
        value = real_get_job(job_uuid)
        if threading.current_thread().name == "cancel-timeout-race":
            read_started.set()
            assert allow_read.wait(2)
        return value

    timeout = threading.Thread(
        target=bridge._on_cancel_timeout,
        kwargs={"job_uuid": JOB_UUID, "expected_accepted": False},
        name="cancel-timeout-race",
    )
    try:
        bridge.submit(task)
        bridge.cancel(TASK_UUID)
        bridge._cancel_cancel_timer(JOB_UUID)
        monkeypatch.setattr(store, "get_job", delayed_timeout_read)
        with scheduler.execution_control_lock:
            timeout.start()
            # 旧实现可在控制屏障外提前读到 cancel_requested；新实现须等待回执。
            read_started.wait(0.05)
            scheduler.on_job_finished(JOB_UUID, False, {"stopped": True}, "canceled")
            allow_read.set()
        timeout.join(2)
        assert not timeout.is_alive()
        assert store.get_job(JOB_UUID)["status"] == "canceled"
        assert store.get_task(TASK_UUID)["status"] == "canceled"
        snapshot = bridge.error_control.snapshot()
        assert snapshot["mode"] == "RUNNING"
        assert snapshot["errors"] == []
    finally:
        allow_read.set()
        if timeout.ident is not None:
            timeout.join(2)
        bridge.close()
