"""F05.3-B 任务运行投影（TaskRuntimeProjection）的行为合同测试。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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
    }
    binding_2 = {
        "uuid": "50000000-0000-4000-8000-000000000002",
        "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
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
            }
        },
    )

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "succeeded"
    assert "started_at" not in aggregate["task"]
    assert aggregate["jobs"] == [store.get_job(source_job_uuid)]
    assert aggregate["jobs"][0]["status"] == "succeeded"


def test_blocked_material_source_admission_is_zero_write(
    store: WorkflowStore,
) -> None:
    """受阻准入必须保持任务和来源作业待处理且不发布部分绑定。

    参数：``store`` 是隔离工作流权威。返回无；断言任务物料准入受阻
    （TaskMaterialAdmissionBlocked）不刷新时间戳、不写 ``return_info``，允许同一
    身份后续准入重试（AdmissionRetry）。异常：非法聚合由投影失败关闭。
    """

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    before = _aggregate(store)

    projection.project_material_source_blocked(TASK_UUID)

    assert _aggregate(store) == before
    assert before["task"]["status"] == "pending"
    assert before["jobs"][0]["status"] == "pending"
    assert before["jobs"][0]["return_info"] == {}


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


def test_queued_task_accepts_succeeded_source_jobs_and_pending_actions(
    store: WorkflowStore,
) -> None:
    """批量排队时来源已准入、普通动作待执行必须保持 pending。

    参数：``store`` 是隔离工作流权威。返回无；断言第二个批量子任务在共享
    设备忙时不会因为来源协调作业已经 succeeded 而被错误取消。
    """

    _seed_material_source_task(store, with_action=True)
    projection = _projection(store)
    projection.project_material_source_admission(
        TASK_UUID,
        {
            NODE_UUIDS[0]: {
                "uuid": "50000000-0000-4000-8000-000000000001",
                "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
            }
        },
    )

    aggregate = projection.project_submission(TASK_UUID, "running")

    assert aggregate["task"]["status"] == "pending"
    assert {job["status"] for job in aggregate["jobs"]} == {"pending", "succeeded"}


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
    assert page["items"][1]["param"] == {
        "resource": {"uuid": "material-1"}
    }
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
    with store.transaction() as connection:
        connection.execute("CREATE TABLE workflow_runs(marker TEXT NOT NULL)")
        connection.execute("CREATE TABLE job_runs(marker TEXT NOT NULL)")
        connection.execute("INSERT INTO workflow_runs(marker) VALUES ('sentinel')")
        connection.execute("INSERT INTO job_runs(marker) VALUES ('sentinel')")
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
        legacy_workflows = connection.execute(
            "SELECT marker FROM workflow_runs"
        ).fetchall()
        legacy_jobs = connection.execute("SELECT marker FROM job_runs").fetchall()
    assert [row["marker"] for row in legacy_workflows] == ["sentinel"]
    assert [row["marker"] for row in legacy_jobs] == ["sentinel"]
    assert all(
        job["status"] != "execution_unknown" for job in store.list_jobs(TASK_UUID)
    )
