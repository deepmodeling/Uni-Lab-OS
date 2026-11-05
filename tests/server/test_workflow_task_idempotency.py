"""整图任务的幂等提交，以及 runtime.db v1 → v2 迁移（第一条真实的表重建迁移）。"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from unilabos.server.database import DATABASE_SPECS
from unilabos.server.database.schema import rebuild_table_migration
from unilabos.server.database.tables.runtime.migrations import (
    _WORKFLOW_TASK_V2,
    _WORKFLOW_TASK_V2_INDEXES,
)
from unilabos.server.services.runtime.workflow.service import (
    WorkflowConflict,
    WorkflowService,
)

_V2_CHECK = """                    AND (
                        (idempotency_key IS NULL AND request_fingerprint = '')
                        OR (idempotency_key IS NOT NULL AND request_fingerprint <> '')
                    )"""
_V1_CHECK = """                    AND idempotency_key IS NULL
                    AND request_fingerprint = ''"""


def _workflow(service: WorkflowService, count: int = 2) -> str:
    workflow = service.create_workflow(name="幂等提交", tags=[], description=None, meta_data={})
    nodes = []
    for index in range(count):
        nodes.append({
            "uuid": str(uuid4()), "name": f"动作{index}", "type": "device_action",
            "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
            "param": {"index": index}, "pose": {"x": index, "y": 0},
            "execution_policy": {"depends_on": [nodes[-1]["uuid"]] if nodes else []},
            "meta_data": {"target_device_id": f"device-{index}"},
        })
    service.save_graph(workflow["uuid"], revision=workflow["revision"], nodes=nodes, edges=[])
    return workflow["uuid"]


def _create(service: WorkflowService, workflow_uuid: str, *, key=None, run_mode="normal"):
    return service.create_workflow_task(
        workflow_uuid=workflow_uuid, run_mode=run_mode, target_node_uuid=None,
        input_value={}, description=None, meta_data={}, idempotency_key=key,
    )


@pytest.fixture
def service():
    service = WorkflowService(":memory:")
    yield service
    service.close()


def test_same_key_returns_the_first_task_and_submits_once(service) -> None:
    submitted: list[str] = []
    service.set_task_submitter(submitted.append)
    workflow_uuid = _workflow(service)

    first = _create(service, workflow_uuid, key="click-1")
    replay = _create(service, workflow_uuid, key="click-1")

    assert replay["uuid"] == first["uuid"]
    assert submitted == [first["uuid"]]
    with pytest.raises(WorkflowConflict):
        _create(service, workflow_uuid, key="click-1", run_mode="step")


def test_without_a_key_every_submission_is_a_new_task(service) -> None:
    workflow_uuid = _workflow(service)
    assert _create(service, workflow_uuid)["uuid"] != _create(service, workflow_uuid)["uuid"]


def test_keys_are_scoped_per_execution_kind(service) -> None:
    workflow_uuid = _workflow(service)
    task = _create(service, workflow_uuid, key="shared-key")
    ad_hoc = service.create_ad_hoc_device_action_task(
        device_id="device-0", action_name="record", param={}, idempotency_key="shared-key",
        description=None, meta_data={},
    )
    assert ad_hoc["uuid"] != task["uuid"]


def _downgrade_to_v1(path) -> None:
    """把一个 v2 库改回 v1 的 workflow_task 结构（同一个重建工具反向执行）。"""

    assert _WORKFLOW_TASK_V2.count(_V2_CHECK) == 1
    v1_sql = _WORKFLOW_TASK_V2.replace(_V2_CHECK, _V1_CHECK)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        rebuild_table_migration(
            1, "test downgrade", table="workflow_task",
            create_sql=v1_sql, indexes=_WORKFLOW_TASK_V2_INDEXES,
        ).apply(connection)
        connection.execute("UPDATE schema_identity SET checksum = 'runtime-v1' WHERE database_key = 'runtime'")
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    finally:
        connection.close()


def _counts(path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("workflow_task", "workflow_node_run", "workflow_node_job", "workflow_runs")
        }
    finally:
        connection.close()


def test_v1_runtime_database_migrates_without_losing_tasks(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    workflow_uuid = _workflow(service, count=3)
    tasks = [_create(service, workflow_uuid)["uuid"] for _ in range(2)]
    service.close()
    before = _counts(path)
    assert before["workflow_task"] == 2 and before["workflow_node_run"] == 6

    _downgrade_to_v1(path)
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        # v1 的 CHECK：整图任务不能带幂等键
        connection.execute(
            "UPDATE workflow_task SET idempotency_key = 'k', request_fingerprint = 'f' WHERE uuid = ?",
            (tasks[0],),
        )
    connection.close()

    service = WorkflowService(path)
    try:
        # 迁移保留任务以及经 ON DELETE CASCADE 外键挂在任务下的节点运行与作业
        assert _counts(path) == before
        assert {task["uuid"] for task in service.list_tasks(page=1, page_size=10)["items"]} >= set(tasks)
        replayed = _create(service, workflow_uuid, key="after-migration")
        assert _create(service, workflow_uuid, key="after-migration")["uuid"] == replayed["uuid"]
    finally:
        service.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SPECS["runtime"].schema_version
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        history = connection.execute("SELECT version, description FROM schema_migration").fetchall()
        assert (2, "workflow_task 允许整图任务携带幂等键") in [tuple(row) for row in history]
        identity = connection.execute("SELECT checksum FROM schema_identity").fetchone()[0]
        assert identity == DATABASE_SPECS["runtime"].checksum
    finally:
        connection.close()
    target = DATABASE_SPECS["runtime"].schema_version
    assert list(tmp_path.glob(f"runtime.db.pre-v{target}-*.bak")), "迁移前应留备份"
