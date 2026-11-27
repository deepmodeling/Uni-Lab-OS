"""工站调用身份的永久占用合同。

调用身份 ``(backend_task_uuid, invocation_key)`` 一经使用即永久占用：任务软删除
只表示"不再执行"，不表示"可以再创建一次"。旧实现的唯一索引与去重查询都带
``deleted_at IS NULL``，会让软删除后的同一调用键再建出一个任务，即同一次调用被
执行两次；中心任务包重投会直接踩中。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput


def _stable_uuid(value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"station-invocation-identity:{value}"))


def _persist_station_task(
    store: WorkflowStore,
    *,
    backend_task_uuid: str,
    invocation_key: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    """用单节点冻结计划持久化一次工站调用。"""

    task_uuid = _stable_uuid(f"task:{invocation_key}")
    node_uuid = _stable_uuid(f"node:{invocation_key}")
    plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": node_uuid,
                "kind": "device_action",
                "device_id": "simulated_device",
                "action_name": "run",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {"goal": {"type": "object", "additionalProperties": True}},
                    "additionalProperties": False,
                },
                "material_requirements": [],
            }
        ],
        "handles": [],
        "edges": [],
    }
    prepared = PreparedTaskInput(
        workflow_snapshot={},
        resolved_input={},
        execution_plan=plan,
        jobs=[
            {
                "uuid": _stable_uuid(f"job:{invocation_key}"),
                "workflow_node_uuid": node_uuid,
                "topological_index": 0,
                "executor_kind": "device_action",
                "execution_policy": {},
                "execution_timeout_seconds": 0,
                "param": {},
            }
        ],
    )
    return store.create_task_with_jobs(
        workflow_uuid=_stable_uuid(f"workflow:{invocation_key}"),
        task_uuid=task_uuid,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        priority="normal",
        plan_builder=lambda _graph: prepared,
        applied_graph={},
        backend_task_uuid=backend_task_uuid,
        invocation_key=invocation_key,
        request_fingerprint=request_fingerprint,
    )


def _mark_task_deleted(store: WorkflowStore, task_uuid: str) -> None:
    """直接把任务置为软删除，用于验证软删除不释放调用身份。"""

    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET deleted_at = ? WHERE uuid = ?",
            ("2026-10-14T00:00:00Z", task_uuid),
        )


def _deleted_at(store: WorkflowStore, task_uuid: str) -> str | None:
    """直接读任务行的软删除标记（``get_task`` 会过滤已删除任务）。"""

    with store.transaction() as connection:
        row = connection.execute(
            "SELECT deleted_at FROM workflow_task WHERE uuid = ?",
            (task_uuid,),
        ).fetchone()
    assert row is not None, f"任务 {task_uuid} 不存在"
    return row["deleted_at"]


def test_soft_deleted_task_keeps_its_invocation_identity(tmp_path: Path) -> None:
    """任务软删除后重放同一调用键必须取回原任务，而不是创建第二个。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    backend_task_uuid = _stable_uuid("backend-task")
    invocation_key = "single-station-call"
    fingerprint = "fingerprint-unchanged"

    first = _persist_station_task(
        store,
        backend_task_uuid=backend_task_uuid,
        invocation_key=invocation_key,
        request_fingerprint=fingerprint,
    )
    task_uuid = first["uuid"]
    assert _deleted_at(store, task_uuid) is None

    _mark_task_deleted(store, task_uuid)
    assert _deleted_at(store, task_uuid) is not None

    # 只读去重入口：必须仍然命中已删除的原任务。
    replayed = store.get_station_task_by_invocation(
        backend_task_uuid=backend_task_uuid,
        invocation_key=invocation_key,
        request_fingerprint=fingerprint,
    )
    assert replayed is not None, "软删除释放了调用身份，重放会创建第二个任务"
    assert replayed["uuid"] == task_uuid
    assert _deleted_at(store, replayed["uuid"]) is not None

    # 写入路径也必须拒绝创建第二个任务，即使调用方绕过只读去重。
    second = _persist_station_task(
        store,
        backend_task_uuid=backend_task_uuid,
        invocation_key=invocation_key,
        request_fingerprint=fingerprint,
    )
    assert second["uuid"] == task_uuid
    assert second["_station_submission_created"] is False


def test_invocation_identity_conflicts_on_changed_request(tmp_path: Path) -> None:
    """同一调用键配不同请求内容必须冲突，不能在保留身份时静默接受。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    backend_task_uuid = _stable_uuid("backend-task-conflict")
    invocation_key = "conflicting-call"

    _persist_station_task(
        store,
        backend_task_uuid=backend_task_uuid,
        invocation_key=invocation_key,
        request_fingerprint="original-fingerprint",
    )

    with pytest.raises(StoreConflict):
        store.get_station_task_by_invocation(
            backend_task_uuid=backend_task_uuid,
            invocation_key=invocation_key,
            request_fingerprint="changed-fingerprint",
        )
