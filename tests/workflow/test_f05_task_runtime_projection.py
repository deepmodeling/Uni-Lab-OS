"""F05.3-B 任务运行投影（TaskRuntimeProjection）的行为合同测试。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.station_event_outbox import StationEventOutboxStore
from unilabos.workflow.store import StoreConflict, WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TASK_UUID = "20000000-0000-4000-8000-000000000001"
NODE_UUIDS = (
    "30000000-0000-4000-8000-000000000001",
    "30000000-0000-4000-8000-000000000002",
    "30000000-0000-4000-8000-000000000003",
)
JOB_UUIDS = (
    "40000000-0000-4000-8000-000000000001",
    "40000000-0000-4000-8000-000000000002",
    "40000000-0000-4000-8000-000000000003",
)
_CREATED_AT = "2026-08-05T00:00:00Z"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离的标准工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是 pytest 为单项测试提供的临时目录。
    产生：可写的本地工作流任务（WorkflowTask）权威；测试结束后关闭连接。
    """

    # ``opened_store`` 是本测试唯一的工作流任务（WorkflowTask）写权威。
    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _projection(store: WorkflowStore) -> Any:
    """延迟加载待实现的任务运行投影（TaskRuntimeProjection）。

    参数：``store`` 是标准工作流存储（WorkflowStore）。
    返回：绑定该存储的任务运行投影实例。
    异常：RED 阶段模块不存在时抛出 ``ModuleNotFoundError``。
    """

    # ``projection_module`` 是 F05.3-B 约定的唯一生产模块接缝。
    projection_module = importlib.import_module(
        "unilabos.workflow.task_runtime_projection"
    )
    return projection_module.TaskRuntimeProjection(store)


def _seed_task(store: WorkflowStore, *, job_count: int = 2) -> tuple[str, ...]:
    """写入一个标准工作流任务（WorkflowTask）及其待处理作业。

    参数：``store`` 是唯一写权威；``job_count`` 是要创建的一或两个工作流节点作业
    （WorkflowNodeJob）数量。
    返回：按拓扑顺序排列的工作流节点作业 UUID。
    异常：数量不在测试支持范围内时抛出 ``ValueError``；数据库约束异常原样传播。
    """

    if job_count not in {1, 2, 3}:
        raise ValueError("测试任务只支持一至三个工作流节点作业")
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.3-B 运行投影",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``selected_job_uuids`` 是本次任务真正拥有的稳定作业身份集合。
    selected_job_uuids = JOB_UUIDS[:job_count]
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', '{}',
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (TASK_UUID, _CREATED_AT, _CREATED_AT, WORKFLOW_UUID),
        )
        for topological_index, (node_uuid, job_uuid) in enumerate(
            zip(NODE_UUIDS, selected_job_uuids, strict=False)
        ):
            # ``topological_index`` 冻结兄弟作业的标准查询顺序，不代表执行尝试号。
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status,
                    attempt, param, feedback_data, return_info, control_data,
                    error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (
                    job_uuid,
                    _CREATED_AT,
                    _CREATED_AT,
                    TASK_UUID,
                    node_uuid,
                    topological_index,
                ),
            )
    return selected_job_uuids


def _aggregate(store: WorkflowStore) -> dict[str, Any]:
    """读取标准工作流任务（WorkflowTask）/作业聚合。

    参数：``store`` 是本地工作流权威。
    返回：包含一个任务投影和按拓扑顺序作业投影的字典。
    """

    return {
        "task": store.get_task(TASK_UUID),
        "jobs": store.list_jobs(TASK_UUID),
    }


def _seed_material_source_task(
    store: WorkflowStore,
    *,
    with_action: bool,
) -> tuple[str, ...]:
    """写入一个含物料来源解析作业的标准工作流任务。

    参数：``store`` 是唯一写权威；``with_action`` 决定来源之后是否还有普通设备
    动作。返回：按拓扑顺序排列的作业 UUID。异常：数据库约束原样传播。
    """

    # ``job_uuids`` 的首项稳定归属于物料来源（MaterialSource）节点。
    job_uuids = _seed_task(store, job_count=2 if with_action else 1)
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_node_job
            SET executor_kind = 'material_source'
            WHERE uuid = ?
            """,
            (job_uuids[0],),
        )
    return job_uuids


def test_local_condition_evaluation_persists_selected_and_skipped_jobs(
    store: WorkflowStore,
) -> None:
    """本地条件结算不得借用设备完成路径，并且必须幂等记录未选分支。"""

    condition_job_uuid, selected_job_uuid, skipped_job_uuid = _seed_task(
        store,
        job_count=3,
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET executor_kind = 'condition' WHERE uuid = ?",
            (condition_job_uuid,),
        )
    projection = _projection(store)

    for _ in range(2):
        projection.project_local_control_evaluation(
            job_uuid=condition_job_uuid,
            selected_branch="if",
            skipped_job_uuids=[skipped_job_uuid],
        )

    aggregate = _aggregate(store)
    jobs = {job["uuid"]: job for job in aggregate["jobs"]}
    assert aggregate["task"]["status"] == "pending"
    assert jobs[condition_job_uuid]["status"] == "succeeded"
    assert jobs[condition_job_uuid]["return_info"] == {"selected_branch": "if"}
    assert jobs[selected_job_uuid]["status"] == "pending"
    assert jobs[skipped_job_uuid]["status"] == "skipped"
    assert jobs[skipped_job_uuid]["error_info"] == [{"code": "branch_not_selected"}]

    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=selected_job_uuid,
    )
    projection.project_job_finished(
        job_uuid=selected_job_uuid,
        scheduler_state="success",
        return_info={"ok": True},
    )

    assert store.get_task(TASK_UUID)["status"] == "succeeded"


def test_material_source_admission_projects_typed_result_atomically(
    store: WorkflowStore,
) -> None:
    """成功准入必须原子完成来源作业并保留普通动作待处理。

    参数：``store`` 是隔离工作流权威。返回无；断言物料来源解析作业
    （MaterialSourceResolutionJob）从 ``pending`` 直接到 ``succeeded``，写入
    有类型物料占位符（ResourceSlot）结果，父任务和普通动作仍为 ``pending``。
    """

    source_job_uuid, action_job_uuid = _seed_material_source_task(
        store,
        with_action=True,
    )
    projection = _projection(store)
    # ``binding`` 是任务物料准入（TaskMaterialAdmission）提交的稳定物料绑定。
    binding = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "task_exclusive",
    }

    projection.project_material_source_admission(
        TASK_UUID,
        {NODE_UUIDS[0]: binding},
    )

    aggregate = _aggregate(store)
    jobs_by_uuid = {job["uuid"]: job for job in aggregate["jobs"]}
    assert aggregate["task"]["status"] == "pending"
    assert jobs_by_uuid[source_job_uuid]["status"] == "succeeded"
    assert jobs_by_uuid[source_job_uuid]["return_info"] == {"material": binding}
    assert jobs_by_uuid[action_job_uuid]["status"] == "pending"


def test_material_source_admission_ignores_historical_failed_attempt(
    store: WorkflowStore,
) -> None:
    """成功准入只更新当前 attempt，旧失败作业保持失败。"""

    source_job_uuid, _action_job_uuid = _seed_material_source_task(
        store,
        with_action=True,
    )
    current_job_uuid = "40000000-0000-4000-8000-000000000099"
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_node_job
            SET status='failed', current_attempt=0
            WHERE uuid=?
            """,
            (source_job_uuid,),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status,
                attempt, param, feedback_data, return_info, control_data,
                error_info, current_attempt
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'material_source', '{}', 0, 'pending', 2, '{}', '{}',
                      '{}', '{}', '[]', 1)
            """,
            (
                current_job_uuid,
                _CREATED_AT,
                _CREATED_AT,
                TASK_UUID,
                NODE_UUIDS[0],
            ),
        )
    binding = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "task_exclusive",
    }
    _projection(store).project_material_source_admission(
        TASK_UUID,
        {NODE_UUIDS[0]: binding},
    )
    jobs_by_uuid = {job["uuid"]: job for job in _aggregate(store)["jobs"]}
    assert jobs_by_uuid[source_job_uuid]["status"] == "failed"
    assert jobs_by_uuid[current_job_uuid]["status"] == "succeeded"
    assert jobs_by_uuid[current_job_uuid]["return_info"] == {"material": binding}


def test_material_source_admission_requires_explicit_custody_policy(
    store: WorkflowStore,
) -> None:
    """物料来源准入必须显式提供物料保管策略（MaterialCustodyPolicy）。

    参数：``store`` 是隔离工作流权威。返回无；断言缺失
    ``custody_policy`` 的准入请求关闭失败，且来源作业仍保持待处理。
    """

    source_job_uuid, _ = _seed_material_source_task(store, with_action=True)
    projection = _projection(store)

    with pytest.raises(StoreConflict, match="物料来源保管策略不能为空"):
        projection.project_material_source_admission(
            TASK_UUID,
            {
                NODE_UUIDS[0]: {
                    "uuid": "50000000-0000-4000-8000-000000000001",
                    "resource_template_uuid": ("60000000-0000-4000-8000-000000000001"),
                }
            },
        )

    assert store.get_job(source_job_uuid)["status"] == "pending"


def test_material_source_admission_persists_backend_shaped_facts(
    store: WorkflowStore,
) -> None:
    """准入成功必须同时保存来源绑定和任务独占占有。

    参数：``store`` 是隔离工作流权威。返回无；断言 Backend 同名的 admission、
    binding 与 claim 表共享任务/节点/作业身份，并保留流角色、策略和库位。
    """

    source_job_uuid, _ = _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    material_uuid = "50000000-0000-4000-8000-000000000001"
    site_uuid = "70000000-0000-4000-8000-000000000001"

    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": material_uuid,
                "resource_template_uuid": ("60000000-0000-4000-8000-000000000001"),
                "site_uuid": site_uuid,
                "flow_role": "reagent",
                "custody_policy": "task_exclusive",
            }
        },
    )

    with store.transaction() as connection:
        admission = connection.execute(
            "SELECT * FROM workflow_task_material_admission "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
        binding = connection.execute(
            "SELECT * FROM workflow_task_material_binding WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
        claim = connection.execute(
            "SELECT * FROM workflow_task_material_claim WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
    assert dict(admission)["status"] == "admitted"
    assert dict(admission)["attempt"] == 1
    assert {
        "workflow_node_uuid": NODE_UUIDS[0],
        "workflow_node_job_uuid": source_job_uuid,
        "material_uuid": material_uuid,
        "site_uuid": site_uuid,
        "flow_role": "reagent",
        "custody_policy": "task_exclusive",
    }.items() <= dict(binding).items()
    assert {
        "material_uuid": material_uuid,
        "status": "active",
        "revision": 1,
    }.items() <= dict(claim).items()


def test_shared_source_binding_does_not_create_exclusive_claim(
    store: WorkflowStore,
) -> None:
    """共享来源只建立绑定，不应伪造任务独占 claim。

    参数：``store`` 是隔离工作流权威。返回无；断言 ``shared_source`` 仍完成来源
    准入，但 claim 表没有该任务记录。
    """

    _seed_material_source_task(store, with_action=True)
    _projection(store).project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": ("60000000-0000-4000-8000-000000000001"),
                "site_uuid": None,
                "flow_role": "primary_sample",
                "custody_policy": "shared_source",
            }
        },
    )

    with store.transaction() as connection:
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM workflow_task_material_claim "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()[0]
    assert claim_count == 0


def test_blocked_material_admission_is_revisioned_and_restart_visible(
    store: WorkflowStore,
) -> None:
    """受阻判定必须可重试、可解释并能在进程重启后恢复。

    参数：``store`` 是隔离工作流权威。返回无；断言两次判定复用同一 admission
    身份并推进 attempt/revision，任务等待原因和恢复列表来自持久事实。
    """

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    wait_resources = [
        {
            "scope": "material",
            "material_uuid": "50000000-0000-4000-8000-000000000001",
        }
    ]
    projection.project_material_source_blocked(
        TASK_UUID,
        reason="缺少目标试剂",
        wait_resources_by_node={NODE_UUIDS[0]: wait_resources},
    )
    projection.project_material_source_blocked(
        TASK_UUID,
        reason="缺少目标试剂",
        wait_resources_by_node={NODE_UUIDS[0]: wait_resources},
    )

    admission = projection.get_material_admission(TASK_UUID)
    assert admission is not None
    assert admission["status"] == "blocked"
    assert admission["attempt"] == 2
    assert admission["revision"] == 2
    assert store.get_task(TASK_UUID)["wait_reason"] == {
        "code": "material_unavailable",
        "message": "缺少目标试剂",
        "resources": wait_resources,
    }
    assert projection.list_blocked_material_tasks() == [TASK_UUID]


def test_blocked_material_admission_keeps_resources_on_their_source_job(
    store: WorkflowStore,
) -> None:
    """混合来源等待必须按来源节点保存，不得把固定物料显示到自动选料节点。"""

    source_job_uuids = _seed_task(store, job_count=2)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET executor_kind='material_source' "
            "WHERE workflow_task_uuid=?",
            (TASK_UUID,),
        )
    fixed_material_uuid = "50000000-0000-4000-8000-000000000001"

    _projection(store).project_material_source_blocked(
        TASK_UUID,
        wait_resources_by_node={
            NODE_UUIDS[0]: [
                {"scope": "material", "material_uuid": fixed_material_uuid}
            ],
            NODE_UUIDS[1]: [],
        },
    )

    fixed_source = store.get_job(source_job_uuids[0])["wait_reason"]
    automatic_source = store.get_job(source_job_uuids[1])["wait_reason"]
    assert fixed_source["resources"] == [
        {"scope": "material", "material_uuid": fixed_material_uuid},
    ]
    assert automatic_source == {
        "code": "material_unavailable",
        "message": "任务所需物料暂不可用",
    }


def test_blocked_material_admission_survives_store_reopen(tmp_path: Path) -> None:
    """关闭并重开数据库后仍能恢复受阻准入。

    参数：``tmp_path`` 是 pytest 隔离目录。返回无；断言增量 Schema 可重复运行，
    admission、任务等待原因与恢复索引均来自同一 SQLite 持久文件。
    """

    database = tmp_path / "workflow-restart.db"
    first_store = WorkflowStore(database)
    try:
        _seed_material_source_task(first_store, with_action=True)
        _projection(first_store).project_material_source_blocked(
            TASK_UUID,
            reason="等待补料",
        )
    finally:
        first_store.close()

    reopened_store = WorkflowStore(database)
    try:
        reopened_projection = _projection(reopened_store)
        assert reopened_projection.list_blocked_material_tasks() == [TASK_UUID]
        assert reopened_projection.get_material_admission(TASK_UUID)["status"] == (
            "blocked"
        )
        assert reopened_store.get_task(TASK_UUID)["wait_reason"]["message"] == (
            "等待补料"
        )
    finally:
        reopened_store.close()


def test_failed_task_releases_claim_only_after_cleanup_settlement(
    store: WorkflowStore,
) -> None:
    """失败任务在物理清理结算前不得释放独占物料。

    参数：``store`` 是隔离工作流权威。返回无；先把任务置为失败，断言 claim 仍
    active；再提交 ``cleanup_status=settled``，断言触发器幂等释放并推进修订。
    """

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": ("60000000-0000-4000-8000-000000000001"),
                "custody_policy": "task_exclusive",
            }
        },
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status = 'failed' WHERE uuid = ?",
            (TASK_UUID,),
        )
        active_status = connection.execute(
            "SELECT status FROM workflow_task_material_claim "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()[0]
    assert active_status == "active"

    projection.project_cleanup_settled(TASK_UUID)

    with store.transaction() as connection:
        claim = connection.execute(
            "SELECT status, revision, released_at "
            "FROM workflow_task_material_claim WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
    assert tuple(claim)[:2] == ("released", 2)
    assert claim["released_at"] is not None


def test_reconciled_attention_task_can_finish_cleanup_settlement(
    store: WorkflowStore,
) -> None:
    """物理对账清除不确定性后允许从 requires_attention 收敛为 settled。"""

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": (
                    "60000000-0000-4000-8000-000000000001"
                ),
                "custody_policy": "task_exclusive",
            }
        },
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status = 'failed', "
            "cleanup_status = 'requires_attention' WHERE uuid = ?",
            (TASK_UUID,),
        )

    aggregate = projection.project_cleanup_settled(TASK_UUID)

    assert aggregate["task"]["cleanup_status"] == "settled"
    with store.transaction() as connection:
        claim_status = connection.execute(
            "SELECT status FROM workflow_task_material_claim "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()[0]
    assert claim_status == "released"


def test_cleanup_settlement_rejects_jobs_that_still_need_reconciliation(
    store: WorkflowStore,
) -> None:
    """只要任一 Job 仍有不确定事实，任务级清理就不得释放执行锁。"""

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status='failed', "
            "cleanup_status='requires_attention' WHERE uuid=?",
            (TASK_UUID,),
        )
        connection.execute(
            "UPDATE workflow_node_job SET status='failed', "
            "uncertainty_reason='edge_result_unknown' WHERE uuid=?",
            (job_uuid,),
        )

    with pytest.raises(StoreConflict, match="仍有作业等待物理对账"):
        projection.project_cleanup_settled(TASK_UUID)

    assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
    assert projection.list_execution_locks(job_uuid)[0]["state"] == "reserved"


def test_paused_submission_accepts_succeeded_material_sources(
    store: WorkflowStore,
) -> None:
    """暂停调试提交接受先完成来源、后派发普通动作的合法聚合。"""

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
                "custody_policy": "task_exclusive",
            }
        },
    )

    aggregate = projection.project_submission(TASK_UUID, "paused")

    assert aggregate["task"]["status"] == "pending"
    assert {job["status"] for job in aggregate["jobs"]} == {
        "pending",
        "succeeded",
    }


def test_trace_context_is_persisted_without_changing_business_timestamps(
    store: WorkflowStore,
) -> None:
    """调度 Trace 是可恢复关联事实，但不是任务业务状态变化。"""

    _seed_task(store, job_count=1)
    projection = _projection(store)
    trace_context = {
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "vendor=value",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "span_id": "0123456789abcdef",
        "ignored": "must-not-be-persisted",
    }

    aggregate = projection.project_trace_context(TASK_UUID, trace_context)

    assert aggregate["task"]["trace_context"] == {
        "traceparent": trace_context["traceparent"],
        "tracestate": "vendor=value",
        "trace_id": trace_context["trace_id"],
        "span_id": trace_context["span_id"],
    }
    assert aggregate["task"]["update_time"] == _CREATED_AT

    with pytest.raises(StoreConflict, match="Trace ID"):
        projection.project_trace_context(
            TASK_UUID,
            {
                "traceparent": "00-fedcba9876543210fedcba9876543210-fedcba9876543210-01",
                "trace_id": "fedcba9876543210fedcba9876543210",
                "span_id": "fedcba9876543210",
            },
        )


def test_running_admission_replay_repairs_implicit_passthrough_binding(
    store: WorkflowStore,
) -> None:
    """运行中恢复必须补齐旧计划遗漏的隐式物料透传参数。

    参数：``store`` 是隔离工作流权威。返回无；断言已成功物料来源
    （MaterialSource）的幂等准入重放会从冻结边推导待处理动作参数，且不会
    改写任务或来源终态。
    """

    source_job_uuid, action_job_uuid = _seed_material_source_task(
        store,
        with_action=True,
    )
    binding = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "shared_source",
    }
    plan = {
        "version": 1,
        "nodes": [
            {
                "uuid": NODE_UUIDS[0],
                "kind": "material_source",
                "material_binding_targets": [],
            },
            {"uuid": NODE_UUIDS[1], "kind": "device_action"},
        ],
        "edges": [
            {
                "source_node_uuid": NODE_UUIDS[0],
                "target_node_uuid": NODE_UUIDS[1],
                "source_type": "ResourceSlot",
                "target_type": "ResourceSlot",
                "target_data_key": "beaker",
            }
        ],
    }
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status = 'running', execution_plan = ? "
            "WHERE uuid = ?",
            (json.dumps(plan), TASK_UUID),
        )
        connection.execute(
            "UPDATE workflow_node_job SET status = 'succeeded', return_info = ? "
            "WHERE uuid = ?",
            (json.dumps({"material": binding}), source_job_uuid),
        )

    _projection(store).project_material_source_admission(
        TASK_UUID,
        {NODE_UUIDS[0]: binding},
    )

    assert store.get_task(TASK_UUID)["status"] == "running"
    assert store.get_job(source_job_uuid)["status"] == "succeeded"
    assert store.get_job(action_job_uuid)["param"] == {
        "beaker": {"uuid": binding["uuid"]}
    }


def test_parent_source_admission_does_not_bind_child_source_mount(
    store: WorkflowStore,
) -> None:
    """选盒来源成功时，盒→枪头 mount 边只留给运行时解析，不得写进子级来源参数。

    参数：``store`` 是隔离工作流权威。返回无；断言父级物料来源准入会跳过指向
    下游物料来源的 ResourceSlot，子级作业保持待处理且参数不被写成冻结 UUID。
    """

    parent_job, child_job, action_job = _seed_task(store, job_count=3)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET executor_kind = 'material_source' "
            "WHERE uuid IN (?, ?)",
            (parent_job, child_job),
        )
        connection.execute(
            "UPDATE workflow_task SET execution_plan = ? WHERE uuid = ?",
            (
                json.dumps(
                    {
                        "version": 1,
                        "nodes": [
                            {
                                "uuid": NODE_UUIDS[0],
                                "kind": "material_source",
                                "material_binding_targets": [],
                            },
                            {
                                "uuid": NODE_UUIDS[1],
                                "kind": "material_source",
                                "runtime_material_source": True,
                                "runtime_mount_binding": {
                                    "source_node_uuid": NODE_UUIDS[0],
                                    "source_data_key": "material",
                                },
                                "material_binding_targets": [
                                    {
                                        "workflow_node_uuid": NODE_UUIDS[2],
                                        "param_key": "resource",
                                    }
                                ],
                            },
                            {"uuid": NODE_UUIDS[2], "kind": "device_action"},
                        ],
                        "edges": [
                            {
                                "source_node_uuid": NODE_UUIDS[0],
                                "target_node_uuid": NODE_UUIDS[1],
                                "source_type": "ResourceSlot",
                                "target_type": "ResourceSlot",
                                "source_data_key": "material",
                                "target_data_key": "mount",
                            }
                        ],
                    }
                ),
                TASK_UUID,
            ),
        )

    binding = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "shared_source",
    }
    _projection(store).project_material_source_admission(
        TASK_UUID,
        {NODE_UUIDS[0]: binding},
    )

    assert store.get_job(parent_job)["status"] == "succeeded"
    assert store.get_job(child_job)["status"] == "pending"
    assert store.get_job(child_job)["param"] == {}
    assert store.get_job(action_job)["param"] == {}
    assert store.get_task(TASK_UUID)["status"] == "pending"


def test_multiple_material_sources_targeting_one_action_keep_all_bindings(
    store: WorkflowStore,
) -> None:
    """同一动作的多个自动物料来源必须在一笔准入中累积全部参数。

    参数：``store`` 是隔离工作流权威。返回无；断言两个物料来源
    （MaterialSource）分别投影到同一动作的不同参数时，后写来源不会从事务开始
    时的陈旧作业快照覆盖先写来源。
    """

    source_job_1, source_job_2, action_job = _seed_task(store, job_count=3)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET executor_kind = 'material_source' "
            "WHERE uuid IN (?, ?)",
            (source_job_1, source_job_2),
        )
        connection.execute(
            "UPDATE workflow_task SET execution_plan = ? WHERE uuid = ?",
            (
                json.dumps(
                    {
                        "version": 1,
                        "nodes": [
                            {
                                "uuid": NODE_UUIDS[0],
                                "kind": "material_source",
                                "material_binding_targets": [
                                    {
                                        "workflow_node_uuid": NODE_UUIDS[2],
                                        "param_key": "solvent_pump_1",
                                    }
                                ],
                            },
                            {
                                "uuid": NODE_UUIDS[1],
                                "kind": "material_source",
                                "material_binding_targets": [
                                    {
                                        "workflow_node_uuid": NODE_UUIDS[2],
                                        "param_key": "solvent_pump_2",
                                    }
                                ],
                            },
                            {"uuid": NODE_UUIDS[2], "kind": "device_action"},
                        ],
                        "edges": [],
                    }
                ),
                TASK_UUID,
            ),
        )

    binding_1 = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "shared_source",
    }
    binding_2 = {
        "uuid": "50000000-0000-4000-8000-000000000002",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
        "custody_policy": "task_exclusive",
    }

    _projection(store).project_material_source_admission(
        TASK_UUID,
        {NODE_UUIDS[0]: binding_1, NODE_UUIDS[1]: binding_2},
    )

    assert store.get_job(action_job)["param"] == {
        "solvent_pump_1": {"uuid": binding_1["uuid"]},
        "solvent_pump_2": {"uuid": binding_2["uuid"]},
    }


def test_source_only_admission_completes_task_without_running_state(
    store: WorkflowStore,
) -> None:
    """只含来源的任务在准入成功后必须直接成功。

    参数：``store`` 是隔离工作流权威。返回无；断言协调器工作不会伪造
    ``running`` 或设备派发，唯一来源作业和父工作流任务（WorkflowTask）直接
    进入 ``succeeded``。异常：非法状态转换使测试失败。
    """

    (source_job_uuid,) = _seed_material_source_task(store, with_action=False)
    projection = _projection(store)

    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
                "custody_policy": "shared_source",
            }
        },
    )

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "succeeded"
    assert "started_at" not in aggregate["task"]
    assert aggregate["jobs"] == [store.get_job(source_job_uuid)]
    assert aggregate["jobs"][0]["status"] == "succeeded"


def test_blocked_material_source_admission_preserves_jobs_and_records_wait(
    store: WorkflowStore,
) -> None:
    """受阻准入保持作业待处理，同时记录可恢复的等待事实。

    参数：``store`` 是隔离工作流权威。返回无；断言任务物料准入受阻
    （TaskMaterialAdmissionBlocked）不写来源结果或部分绑定，但持久化 admission
    和任务等待原因，允许同一身份后续准入重试。异常：非法聚合由投影失败关闭。
    """

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    projection.project_material_source_blocked(TASK_UUID)

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "pending"
    assert aggregate["task"]["wait_reason"]["code"] == "material_unavailable"
    assert aggregate["jobs"][0]["status"] == "pending"
    assert aggregate["jobs"][0]["return_info"] == {}
    with store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workflow_task_material_binding "
                "WHERE workflow_task_uuid = ?",
                (TASK_UUID,),
            ).fetchone()[0]
            == 0
        )


def test_quantity_only_material_admission_records_wait_and_can_retry(
    store: WorkflowStore,
) -> None:
    """数量库存竞争失败应标记消费节点，成功重试后清空同一准入等待。"""

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_quantity_inventory_blocked(
        TASK_UUID,
        reason="当前内容物余量不足",
        wait_resources_by_job={
            job_uuid: [
                {
                    "scope": "material",
                    "material_uuid": "50000000-0000-4000-8000-000000000099",
                }
            ]
        },
    )

    blocked = _aggregate(store)
    assert blocked["task"]["wait_reason"]["code"] == ("quantity_inventory_unavailable")
    assert blocked["jobs"][0]["wait_reason"]["resources"] == [
        {
            "scope": "material",
            "material_uuid": "50000000-0000-4000-8000-000000000099",
        }
    ]

    projection.project_task_material_admission(TASK_UUID)
    admitted = _aggregate(store)
    assert admitted["task"]["wait_reason"] == {}
    assert admitted["jobs"][0]["wait_reason"] == {}
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT status FROM workflow_task_material_admission "
            "WHERE workflow_task_uuid=?",
            (TASK_UUID,),
        ).fetchone()
    assert row["status"] == "admitted"


def test_waiting_for_material_projects_to_pending_without_job_mutation(
    store: WorkflowStore,
) -> None:
    """内部等料不得泄漏为 Backend 之外的工作流任务状态。

    参数：``store`` 是隔离工作流权威。返回无；断言首次观察和重复观察都保持
    工作流任务（WorkflowTask）与全部作业为 ``pending``，且不刷新时间戳。
    """

    _seed_task(store)
    projection = _projection(store)
    before = _aggregate(store)

    projection.project_submission(TASK_UUID, "waiting_for_material")
    first = _aggregate(store)
    projection.project_submission(TASK_UUID, "waiting_for_material")

    assert first == before
    assert _aggregate(store) == before
    assert {job["status"] for job in first["jobs"]} == {"pending"}


def test_pre_dispatch_atomically_marks_exact_job_and_parent_task_running(
    store: WorkflowStore,
) -> None:
    """派发前投影只推进目标作业，并在同一事务启动父任务。

    参数：``store`` 是隔离工作流权威。返回无；断言目标作业为 ``dispatched``、
    兄弟作业仍为 ``pending``，父任务为 ``running`` 且记录开始时间。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)

    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=first_job_uuid)

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "running"
    assert "started_at" in aggregate["task"]
    assert {job["uuid"]: job["status"] for job in aggregate["jobs"]} == {
        first_job_uuid: "dispatched",
        second_job_uuid: "pending",
    }


def test_pre_dispatch_replay_is_zero_write(store: WorkflowStore) -> None:
    """同一派发意图的投递重放（DeliveryReplay）必须零写入。

    参数：``store`` 是隔离工作流权威。返回无；断言重复派发前通知不改变任务、
    作业或其时间戳。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
    first = _aggregate(store)

    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    assert _aggregate(store) == first


def test_pre_dispatch_persists_actual_device_binding_with_parameters(
    store: WorkflowStore,
) -> None:
    """动态选择的实际设备必须与最终参数一起保存在工站调度库。

    参数：``store`` 是隔离工作流权威。返回：无；断言作业持久保存实际参数与
    执行器快照，且不再向旧 Backend 发件箱写入重复事实。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    actual_param = {"temperature": 80}
    actual_executor = {
        "local_device_id": "reactor-b",
        "material_uuid": "92000000-0000-4000-8000-000000000002",
    }

    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        resolved_param=actual_param,
        actual_executor=actual_executor,
    )

    job = store.get_job(job_uuid)
    assert job["param"] == actual_param
    assert job["control_data"]["actual_executor"] == actual_executor
    assert StationEventOutboxStore(store).list_pending() == []


def test_capacity_gate_wait_persists_reason_without_fake_resource_waiter(
    store: WorkflowStore,
) -> None:
    """动态设备或库位容量等待不得伪造成一个可取得的资源锁。

    参数：``store`` 是隔离工作流权威。返回：无；断言作业保存稳定门禁原因，
    但不创建 ``execution_lock_waiter``，因此后续具体候选变化不会继承伪锁身份。
    异常：缺少等待代码或中文原因时生产投影失败关闭。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)

    projection.project_execution_lock_wait(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        execution_locks=[],
        wait_code="device_busy",
        wait_message="匹配设备当前全部忙碌",
    )

    job = store.get_job(job_uuid)
    assert job["status"] == "pending"
    assert job["wait_reason"]["code"] == "device_busy"
    with store.transaction() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM execution_lock_waiter "
            "WHERE workflow_node_job_uuid=? AND state='waiting'",
            (job_uuid,),
        ).fetchone()["count"]
    assert count == 0


def test_device_capacity_wait_preserves_candidate_diagnostics(
    store: WorkflowStore,
) -> None:
    """设备忙等待必须接受解析器给出的具体设备展示与诊断字段。

    参数：``store`` 是隔离工作流权威。返回无；断言固定设备选择器产生的
    Material UUID、本地 ID、名称和候选原因都进入同一个可展示等待事实，而不把
    正常的设备竞争升级为任务提交失败。未知字段仍由生产规范器拒绝。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    device = {
        "scope": "device",
        "device_id": "92000000-0000-4000-8000-000000000002",
        "local_device_id": "reactor-b",
        "device_name": "S04 磁力搅拌器",
        "wait_code": "device_busy",
        "wait_message": "目标设备当前正被另一个作业使用",
    }

    projection.project_execution_lock_wait(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        execution_locks=[],
        wait_code="device_busy",
        wait_message="固定设备当前忙碌，等待下一轮调度",
        wait_resources=[device],
    )

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "running"
    assert aggregate["jobs"][0]["status"] == "pending"
    wait_reason = aggregate["jobs"][0]["wait_reason"]
    assert wait_reason["code"] == "device_busy"
    assert wait_reason["message"] == "固定设备当前忙碌，等待下一轮调度"
    assert wait_reason["resources"] == [device]
    assert wait_reason["waiting_since"]


def test_material_and_site_wait_preserve_human_readable_names(
    store: WorkflowStore,
) -> None:
    """物料与库位等待事实必须同时保存稳定身份和用户可读名称。"""

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    resources = [
        {
            "scope": "material",
            "material_uuid": "50000000-0000-4000-8000-000000000011",
            "material_name": "样品瓶 A",
        },
        {
            "scope": "material_site",
            "material_uuid": "50000000-0000-4000-8000-000000000012",
            "material_name": "S08 开盖机",
            "site_uuid": "60000000-0000-4000-8000-000000000011",
            "site_name": "INPUT-1",
        },
    ]

    projection.project_execution_lock_wait(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        execution_locks=[
            {
                "lock_key": (
                    "material/50000000-0000-4000-8000-000000000011/exclusive"
                ),
                "scope": "material",
                "material_uuid": "50000000-0000-4000-8000-000000000011",
            },
            {
                "lock_key": (
                    "material/50000000-0000-4000-8000-000000000012/site/"
                    "60000000-0000-4000-8000-000000000011/exclusive"
                ),
                "scope": "material_site",
                "material_uuid": "50000000-0000-4000-8000-000000000012",
                "site_uuid": "60000000-0000-4000-8000-000000000011",
            },
        ],
        wait_code="site_occupied",
        wait_message="目标库位当前不可用",
        wait_resources=resources,
    )

    assert store.get_job(job_uuid)["wait_reason"]["resources"] == resources


def test_runtime_journal_and_sse_invalidation_capture_dispatch_and_result(
    store: WorkflowStore,
) -> None:
    """派发与结果必须持久记录，并各自触发一次可重放的前端失效通知。"""

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)

    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=job_uuid,
        resolved_param={"resource": {"uuid": "material-1"}},
    )
    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"message": "done"},
    )

    page = store.list_task_runtime_events(
        TASK_UUID,
        after_sequence=0,
        limit=10,
    )
    assert [
        (event["kind"], event.get("from_status"), event.get("to_status"))
        for event in page["items"]
    ] == [
        ("task_transition", "pending", "running"),
        ("job_transition", "pending", "dispatched"),
        ("job_transition", "dispatched", "succeeded"),
        ("task_transition", "running", "succeeded"),
    ]
    assert page["items"][1]["param"] == {"resource": {"uuid": "material-1"}}
    assert page["items"][2]["return_info"] == {"message": "done"}
    assert page["next_cursor"] == page["items"][-1]["sequence"]
    assert page["has_more"] is False

    invalidations = [
        event
        for event in store.list_events(after_sequence=0, limit=100)
        if event["event"] == "workflow.runtime.changed"
    ]
    assert [event["data"] for event in invalidations] == [
        {"workflow_task_uuid": TASK_UUID},
        {"workflow_task_uuid": TASK_UUID},
    ]


def test_runtime_event_page_uses_exclusive_cursor(store: WorkflowStore) -> None:
    """任务运行日志页必须使用严格排他、单调递增的持久游标。"""

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    first = store.list_task_runtime_events(
        TASK_UUID,
        after_sequence=0,
        limit=1,
    )
    second = store.list_task_runtime_events(
        TASK_UUID,
        after_sequence=first["next_cursor"],
        limit=1,
    )

    assert first["has_more"] is True
    assert second["has_more"] is False
    assert first["items"][0]["sequence"] < second["items"][0]["sequence"]


def test_first_legacy_success_maps_job_to_succeeded_but_task_stays_running(
    store: WorkflowStore,
) -> None:
    """首个本地 ``success`` 只完成一个作业，不能提前完成多作业任务。

    参数：``store`` 是隔离工作流权威。返回无；断言本地状态映射为 Backend
    ``succeeded``，父任务仍为 ``running``。
    """

    first_job_uuid, _second_job_uuid = _seed_task(store)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=first_job_uuid)

    projection.project_job_finished(
        job_uuid=first_job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )

    aggregate = _aggregate(store)
    assert aggregate["jobs"][0]["status"] == "succeeded"
    assert aggregate["jobs"][0]["return_info"] == {"completed": True}
    assert aggregate["task"]["status"] == "running"
    assert "finished_at" not in aggregate["task"]


def test_last_success_aggregates_multi_job_task_to_succeeded(
    store: WorkflowStore,
) -> None:
    """只有最后一个作业成功后，父任务才投影为成功终态。

    参数：``store`` 是隔离工作流权威。返回无；断言两个作业均为 ``succeeded``，
    工作流任务（WorkflowTask）具有唯一成功终态和完成时间。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)
    for job_uuid in (first_job_uuid, second_job_uuid):
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success",
            return_info={"job_uuid": job_uuid},
        )

    aggregate = _aggregate(store)
    assert [job["status"] for job in aggregate["jobs"]] == [
        "succeeded",
        "succeeded",
    ]
    assert aggregate["task"]["status"] == "succeeded"
    assert "finished_at" in aggregate["task"]


def test_failed_job_dominates_task_without_overwriting_sibling_result(
    store: WorkflowStore,
) -> None:
    """失败业务终态不得阻止兄弟明确结果落盘，也不得被兄弟成功覆盖。

    参数：``store`` 是隔离工作流权威。返回无；断言失败作业使父任务快速失败，
    迟到兄弟成功仍写入自身作业但父任务继续为 ``failed``。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)
    for job_uuid in (first_job_uuid, second_job_uuid):
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    projection.project_job_finished(
        job_uuid=first_job_uuid,
        scheduler_state="failed",
        error_info=[{"code": "device_action_failed"}],
    )
    projection.project_job_finished(
        job_uuid=second_job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )

    aggregate = _aggregate(store)
    assert [job["status"] for job in aggregate["jobs"]] == [
        "failed",
        "succeeded",
    ]
    assert aggregate["task"]["status"] == "failed"


def test_terminal_replay_is_idempotent_and_conflicting_terminal_is_zero_write(
    store: WorkflowStore,
) -> None:
    """相同终态重放幂等，状态或结果冲突必须拒绝且零写入。

    参数：``store`` 是隔离工作流权威。返回无；断言终态身份与结果共同构成稳定
    事实，冲突时抛出 ``StoreConflict`` 并回滚完整任务/作业聚合。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    terminal = _aggregate(store)

    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    assert _aggregate(store) == terminal

    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="failed",
            error_info=[{"code": "late_conflict"}],
        )
    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success",
            return_info={"completed": False},
        )
    assert _aggregate(store) == terminal


@pytest.mark.parametrize(
    "unsupported_scheduler_state",
    ["execution_unknown", "interrupted"],
)
def test_projection_never_writes_legacy_history_or_execution_unknown(
    store: WorkflowStore,
    unsupported_scheduler_state: str,
) -> None:
    """短期投影不得写遗留历史，也不得伪造执行未知事实。

    参数：``store`` 是隔离工作流权威；``unsupported_scheduler_state`` 是禁止写入
    标准表的遗留或未来状态。返回无；断言遗留历史哨兵保持不变，非法输入关闭失败。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    created_legacy_tables = [name for name in ("workflow_runs", "job_runs") if not store.db.table_exists(name)]
    with store.transaction() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS workflow_runs(marker TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS job_runs(marker TEXT NOT NULL)")
        for name in created_legacy_tables:
            connection.execute(f"INSERT INTO {name}(marker) VALUES ('sentinel')")
        legacy_before = {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}").fetchall()]
            for name in ("workflow_runs", "job_runs")
        }
    projection = _projection(store)
    projection.project_submission(TASK_UUID, "waiting_for_material")
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    before = _aggregate(store)
    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state=unsupported_scheduler_state,
        )
    assert _aggregate(store) == before

    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    with store.transaction() as connection:
        for name in ("workflow_runs", "job_runs"):
            after = [tuple(row) for row in connection.execute(f"SELECT * FROM {name}").fetchall()]
            assert after == legacy_before[name]
        # 测试哨兵不能污染后续使用真实历史表结构的 PostgreSQL 用例。
        for name in created_legacy_tables:
            connection.execute(f"DROP TABLE {name}")
    assert all(
        job["status"] != "execution_unknown" for job in store.list_jobs(TASK_UUID)
    )


def test_execution_process_restart_fails_inflight_and_cancels_pending_nodes(
    store: WorkflowStore,
) -> None:
    """执行进程重启必须在一个事务中终止整张活动 DAG。

    参数：``store`` 是隔离工作流写模型。返回无；断言在途 Job 明确失败、未派发
    Job 取消、父 Task 失败且旧执行占用释放。异常：事务状态不一致会使
    测试失败。
    """

    first_job_uuid, second_job_uuid = _seed_task(store, job_count=2)
    projection = _projection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=first_job_uuid,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    projection.project_dispatch_accepted(first_job_uuid)

    aggregate = projection.project_execution_process_restarted(TASK_UUID)

    assert aggregate is not None
    jobs = {job["uuid"]: job for job in aggregate["jobs"]}
    assert jobs[first_job_uuid]["status"] == "failed"
    assert jobs[first_job_uuid]["error_info"][0]["code"] == (
        "execution_process_restarted"
    )
    assert jobs[second_job_uuid]["status"] == "canceled"
    assert jobs[second_job_uuid]["error_info"][0]["code"] == (
        "task_aborted_by_runtime_restart"
    )
    assert aggregate["task"]["status"] == "failed"
    assert aggregate["task"]["cleanup_status"] == "pending"
    assert projection.list_execution_locks(first_job_uuid)[0]["state"] == "released"
    assert StationEventOutboxStore(store).list_pending() == []
    with store.read() as connection:
        result = connection.execute(
            "SELECT outcome FROM workflow_node_job_result WHERE workflow_node_job_uuid = ?",
            (first_job_uuid,),
        ).fetchone()
    assert result["outcome"] == "failed"
