"""作者命名资源从 ExecutionPlan 到双库 Claim/Fence 的端到端合同。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, persist_task, stable_uuid
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionRequest,
    DispatchResource,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.app.scheduler.resource_lock import named_resource_lock_key
from unilabos.config.config import BasicConfig
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.resource_lock_plan import (
    resource_plan_for_node,
    serialize_resource_plan,
)
from unilabos.workflow.store import WorkflowStore

_OLD_SCOPE_CHECK = "scope IN ('device', 'material', 'material_site')"
_NEW_SCOPE_CHECK = "scope IN ('device', 'material', 'material_site', 'resource')"


def _downgrade_scope_check(database: Path, tables: Sequence[str]) -> None:
    """把新库的 scope CHECK 精确降成旧定义，模拟原地升级输入。"""

    connection = sqlite3.connect(database)
    try:
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        try:
            for table in tables:
                cursor = connection.execute(
                    "UPDATE sqlite_schema SET sql=replace(sql, ?, ?) "
                    "WHERE type='table' AND name=? AND instr(sql, ?) > 0",
                    (_NEW_SCOPE_CHECK, _OLD_SCOPE_CHECK, table, _NEW_SCOPE_CHECK),
                )
                assert cursor.rowcount == 1
            connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        finally:
            connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()


def _submit_named_mutex_task(
    runtime: CoreRuntime,
    *,
    task_name: str,
    mutex_alias: str,
    device_ids: Sequence[str],
    priority: str = "normal",
) -> tuple[dict[str, Any], tuple[str, ...], dict[str, Any]]:
    """经真实 ExecutionPlan/Bridge 提交一个根作用域命名互斥任务。"""

    node_uuids = tuple(
        stable_uuid(f"node:{task_name}:{index}") for index in range(len(device_ids))
    )
    job_uuids = tuple(
        stable_uuid(f"job:{task_name}:{index}") for index in range(len(device_ids))
    )
    nodes = [
        {
            "uuid": node_uuid,
            "kind": "device_action",
            "device_id": device_id,
            "material_uuid": runtime.device_materials[device_id],
            "action_name": "run",
            "action_type": "UniLabJsonCommand",
            "param": {},
            "param_schema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": False,
            },
            "execution_policy": {},
            "action_resource_contract": {},
            "material_requirements": [],
        }
        for node_uuid, device_id in zip(node_uuids, device_ids)
    ]
    edges = [
        {
            "uuid": stable_uuid(f"edge:{task_name}:{index}:{index + 1}"),
            "source_node_uuid": node_uuids[index],
            "target_node_uuid": node_uuids[index + 1],
            "source_handle_uuid": "",
            "target_handle_uuid": "",
            "dependency_only": True,
            "source_data_key": "",
            "target_data_key": "",
            "source_type": "",
            "target_type": "",
        }
        for index in range(len(node_uuids) - 1)
    ]
    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "workflow_uuid": stable_uuid(f"workflow:{task_name}"),
            "resources": [mutex_alias],
        },
        planned_nodes=nodes,
        planned_edges=edges,
    )
    assert plan is not None and plan.binding_state == "bound"
    serialized = serialize_resource_plan(plan)
    for node in nodes:
        projection = resource_plan_for_node(plan, node["uuid"])
        node["resource_plan_id"] = plan.plan_id
        node["resource_interval_ids"] = [
            interval["interval_id"] for interval in projection["intervals"]
        ]
        node["resource_acquire_set_id"] = next(
            (
                acquire["acquire_set_id"]
                for acquire in projection["acquire_sets"]
            ),
            "",
        )
    aggregate = runtime.submit_frozen(
        task_name=task_name,
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": nodes,
            "handles": [],
            "edges": edges,
            "capabilities": list(plan.capabilities),
            "resource_plan": serialized,
        },
        jobs=[
            {
                "uuid": job_uuid,
                "workflow_node_uuid": node_uuid,
                "topological_index": index,
                "executor_kind": "device_action",
                "execution_policy": {},
                "execution_timeout_seconds": 0,
                "param": {},
            }
            for index, (node_uuid, job_uuid) in enumerate(zip(node_uuids, job_uuids))
        ],
    )
    # ``submit_frozen`` 当前不暴露优先级；测试只依赖先提交 owner 的稳定公平序。
    del priority
    return aggregate, job_uuids, serialized


def _fences(payload: dict[str, Any]) -> dict[str, int]:
    return {
        str(item["lock_key"]): int(item["fencing_token"])
        for item in payload["fences"]
    }


def test_generic_multi_resource_conflict_never_creates_partial_claim(
    tmp_path: Path,
) -> None:
    """两个命名资源中任一被占用时，等待方不能留下另一资源的 Claim/Fence。"""

    store = InventoryStore(str(tmp_path / "generic-atomic.db"))
    service = InventoryService(store)
    held_key = named_resource_lock_key("shared-station")
    free_key = named_resource_lock_key("independent-tool")

    def request(label: str, keys: tuple[str, ...]) -> DispatchAdmissionRequest:
        return DispatchAdmissionRequest(
            effect_uuid=stable_uuid(f"effect:{label}"),
            task_uuid=stable_uuid(f"task:{label}"),
            job_uuid=stable_uuid(f"job:{label}"),
            attempt=1,
            parameter_hash=f"sha256:{label}",
            expected_change_set={"kind": "no_inventory_change"},
            resources=tuple(
                DispatchResource(lock_key=key, scope="resource") for key in keys
            ),
        )

    try:
        owner = service.station_resources.acquire_dispatch_permit(
            request("generic-atomic-owner", (held_key,))
        )
        waiter = service.station_resources.acquire_dispatch_permit(
            request("generic-atomic-waiter", (free_key, held_key))
        )

        assert owner.acquired
        assert not waiter.acquired
        assert waiter.wait_code == "resource_claimed"
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_claim"
        ) == {"count": 1}
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_lock_lease "
            "WHERE lock_key=?",
            (free_key,),
        ) == {"count": 0}
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_fence_counter "
            "WHERE lock_key=?",
            (free_key,),
        ) == {"count": 0}
    finally:
        store.close()


def test_same_named_mutex_serializes_tasks_while_unrelated_mutex_runs(
    core_runtime: CoreRuntime,
) -> None:
    """同名资源跨 Task 精确互斥，不同名资源不产生伪冲突。"""

    owner, owner_jobs, _ = _submit_named_mutex_task(
        core_runtime,
        task_name="generic-owner",
        mutex_alias="station_mutex",
        device_ids=("reactor-a",),
    )
    waiter, waiter_jobs, _ = _submit_named_mutex_task(
        core_runtime,
        task_name="generic-waiter",
        mutex_alias="station_mutex",
        device_ids=("reactor-b",),
    )
    unrelated, unrelated_jobs, _ = _submit_named_mutex_task(
        core_runtime,
        task_name="generic-unrelated",
        mutex_alias="other_station_mutex",
        device_ids=("robot-a",),
    )

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0],
        unrelated_jobs[0],
    ]
    assert core_runtime.workflow_store.get_job(waiter_jobs[0])["status"] == "pending"
    wait_reason = core_runtime.workflow_store.get_job(waiter_jobs[0])["wait_reason"]
    assert {
        "scope": "resource",
        "lock_key": named_resource_lock_key("station_mutex"),
    } in wait_reason["resources"]

    core_runtime.scheduler.on_job_finished(owner_jobs[0], True, {})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0],
        unrelated_jobs[0],
        waiter_jobs[0],
    ]

    core_runtime.scheduler.on_job_finished(unrelated_jobs[0], True, {})
    core_runtime.scheduler.on_job_finished(waiter_jobs[0], True, {})
    assert core_runtime.workflow_store.get_task(owner["task"]["uuid"])["status"] == "succeeded"
    assert core_runtime.workflow_store.get_task(waiter["task"]["uuid"])["status"] == "succeeded"
    assert core_runtime.workflow_store.get_task(unrelated["task"]["uuid"])["status"] == "succeeded"


def test_three_job_named_mutex_scope_keeps_continuity_with_new_predispatch_per_job(
    core_runtime: CoreRuntime,
) -> None:
    """根命名锁贯穿三个 Job，但每个 Job 都重新取得自己的 Claim 与 Fence。"""

    owner, owner_jobs, plan = _submit_named_mutex_task(
        core_runtime,
        task_name="generic-three-job-owner",
        mutex_alias="station_mutex",
        device_ids=("robot-a", "reactor-a", "robot-a"),
    )
    waiter, waiter_jobs, _ = _submit_named_mutex_task(
        core_runtime,
        task_name="generic-three-job-waiter",
        mutex_alias="station_mutex",
        device_ids=("warehouse-a",),
    )
    generic_key = named_resource_lock_key("station_mutex")
    assert next(
        resource["canonical_key"]
        for resource in plan["resources"]
        if resource["alias"] == "station_mutex"
    ) == generic_key
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0]
    ]

    payloads = [core_runtime.dispatcher.dispatched[0]]
    for index in range(2):
        core_runtime.scheduler.on_job_finished(owner_jobs[index], True, {"step": index + 1})
        assert core_runtime.dispatcher.dispatched[-1]["job_id"] == owner_jobs[index + 1]
        assert core_runtime.workflow_store.get_job(waiter_jobs[0])["status"] == "pending"
        payloads.append(core_runtime.dispatcher.dispatched[-1])

    claim_uuids = [str(payload["claim_uuid"]) for payload in payloads]
    generic_tokens = [_fences(payload)[generic_key] for payload in payloads]
    assert len(set(claim_uuids)) == 3
    assert generic_tokens == sorted(generic_tokens)
    assert len(set(generic_tokens)) == 3
    assert all(generic_key in _fences(payload) for payload in payloads)

    core_runtime.scheduler.on_job_finished(owner_jobs[2], True, {"step": 3})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == waiter_jobs[0]
    core_runtime.scheduler.on_job_finished(waiter_jobs[0], True, {})
    assert core_runtime.workflow_store.get_task(owner["task"]["uuid"])["status"] == "succeeded"
    assert core_runtime.workflow_store.get_task(waiter["task"]["uuid"])["status"] == "succeeded"


def test_legacy_workflow_lock_scope_checks_migrate_in_place(tmp_path: Path) -> None:
    """旧 Workflow DB 原地扩 scope，不丢 Lease/Waiter、索引、触发器或外键。"""

    if BasicConfig.database_type == "postgresql":
        pytest.skip("该测试通过 SQLite sqlite_schema 模拟旧库结构")

    database = tmp_path / "legacy-workflow.db"
    store = WorkflowStore(database, persist_workflow_definitions=False)
    aggregate = persist_task(
        store,
        task_name="legacy-generic-workflow",
        devices=("reactor-a",),
        device_material_uuids=("00000000-0000-4000-8000-000000000021",),
    )
    task_uuid = aggregate["uuid"]
    job_uuid = stable_uuid("job:legacy-generic-workflow:0")
    now = "2026-09-07T00:00:00Z"
    lease_uuid = "00000000-0000-4000-8000-000000000022"
    waiter_uuid = "00000000-0000-4000-8000-000000000023"
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO execution_lock_lease("
            "uuid,create_time,update_time,deleted_at,description,meta_data,"
            "workflow_task_uuid,workflow_node_job_uuid,lock_key,scope,"
            "material_uuid,site_uuid,state,acquired_at,released_at"
            ") VALUES(?,?,?,NULL,NULL,'{}',?,?,?,?,NULL,NULL,'reserved',?,NULL)",
            (
                lease_uuid,
                now,
                now,
                task_uuid,
                job_uuid,
                "/devices/legacy-reactor",
                "device",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO execution_lock_waiter("
            "uuid,create_time,update_time,deleted_at,description,meta_data,"
            "workflow_task_uuid,workflow_node_job_uuid,lock_key,scope,"
            "material_uuid,site_uuid,state,enqueued_at,released_at"
            ") VALUES(?,?,?,NULL,NULL,'{}',?,?,?,?,NULL,NULL,'waiting',?,NULL)",
            (
                waiter_uuid,
                now,
                now,
                task_uuid,
                job_uuid,
                "/devices/legacy-waiter",
                "device",
                now,
            ),
        )
    store.close()
    _downgrade_scope_check(database, ("execution_lock_lease", "execution_lock_waiter"))

    reopened = WorkflowStore(database, persist_workflow_definitions=False)
    generic_key = named_resource_lock_key("legacy_station_mutex")
    try:
        with reopened.transaction() as connection:
            assert connection.execute(
                "SELECT uuid FROM execution_lock_lease WHERE uuid=?", (lease_uuid,)
            ).fetchone()["uuid"] == lease_uuid
            assert connection.execute(
                "SELECT uuid FROM execution_lock_waiter WHERE uuid=?", (waiter_uuid,)
            ).fetchone()["uuid"] == waiter_uuid
            for table in ("execution_lock_lease", "execution_lock_waiter"):
                sql = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()["sql"]
                assert _NEW_SCOPE_CHECK in sql
            connection.execute(
                "INSERT INTO execution_lock_lease("
                "uuid,create_time,update_time,deleted_at,description,meta_data,"
                "workflow_task_uuid,workflow_node_job_uuid,lock_key,scope,"
                "material_uuid,site_uuid,state,acquired_at,released_at"
                ") VALUES(?,?,?,NULL,NULL,'{}',?,?,?,?,NULL,NULL,'reserved',?,NULL)",
                (
                    "00000000-0000-4000-8000-000000000024",
                    now,
                    now,
                    task_uuid,
                    job_uuid,
                    generic_key,
                    "resource",
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO execution_lock_waiter("
                "uuid,create_time,update_time,deleted_at,description,meta_data,"
                "workflow_task_uuid,workflow_node_job_uuid,lock_key,scope,"
                "material_uuid,site_uuid,state,enqueued_at,released_at"
                ") VALUES(?,?,?,NULL,NULL,'{}',?,?,?,?,NULL,NULL,'waiting',?,NULL)",
                (
                    "00000000-0000-4000-8000-000000000025",
                    now,
                    now,
                    task_uuid,
                    job_uuid,
                    generic_key,
                    "resource",
                    now,
                ),
            )
            schema_objects = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('index','trigger')"
                ).fetchall()
            }
            assert {
                "ux_execution_lock_lease_active_key",
                "ux_execution_lock_lease_active_job_key",
                "ix_execution_lock_lease_job_state",
                "ux_execution_lock_waiter_active_job_key",
                "ix_execution_lock_waiter_fairness",
                "trg_release_inactive_execution_lock_waiters",
            } <= schema_objects
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()

    # 第二次打开证明迁移幂等且新 scope 仍可被 SQLite 解析。
    idempotent = WorkflowStore(database, persist_workflow_definitions=False)
    idempotent.close()


def test_legacy_inventory_lock_scope_check_migrates_in_place(tmp_path: Path) -> None:
    """旧 Inventory v13 DB 原地升级到当前版本并保留既有 Claim/Lease。"""

    if BasicConfig.database_type == "postgresql":
        pytest.skip("该测试通过 SQLite sqlite_schema 模拟旧库结构")

    database = tmp_path / "legacy-inventory.db"
    store = InventoryStore(str(database))
    now = "2026-09-07T00:00:00Z"
    claim_uuid = "00000000-0000-4000-8000-000000000031"
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO station_execution_claim("
            "claim_uuid,effect_uuid,task_uuid,job_uuid,attempt,parameter_hash,"
            "expected_change_set,resource_keys,state,acquired_at,committed_at,"
            "released_at,update_time"
            ") VALUES(?,?,?,?,1,'sha256:legacy','{}','[]','prepared',?,NULL,NULL,?)",
            (
                claim_uuid,
                "00000000-0000-4000-8000-000000000032",
                "00000000-0000-4000-8000-000000000033",
                "00000000-0000-4000-8000-000000000034",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO station_execution_lock_lease("
            "claim_uuid,lock_key,scope,material_uuid,site_uuid,fencing_token,"
            "state,acquired_at,released_at,update_time"
            ") VALUES(?,?,'device',NULL,NULL,1,'prepared',?,NULL,?)",
            (claim_uuid, "/devices/legacy-reactor", now, now),
        )
    store.close()
    _downgrade_scope_check(database, ("station_execution_lock_lease",))
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version = 13")
        connection.commit()
    finally:
        connection.close()

    reopened = InventoryStore(str(database))
    generic_key = named_resource_lock_key("legacy_inventory_station_mutex")
    try:
        assert reopened.query_one("PRAGMA user_version") == {
            "user_version": SCHEMA_VERSION
        }
        assert reopened.query_one(
            "SELECT claim_uuid,lock_key FROM station_execution_lock_lease "
            "WHERE claim_uuid=?",
            (claim_uuid,),
        ) == {"claim_uuid": claim_uuid, "lock_key": "/devices/legacy-reactor"}
        with reopened.transaction() as db:
            db.execute(
                "INSERT INTO station_execution_lock_lease("
                "claim_uuid,lock_key,scope,material_uuid,site_uuid,fencing_token,"
                "state,acquired_at,released_at,update_time"
                ") VALUES(?,?,'resource',NULL,NULL,2,'prepared',?,NULL,?)",
                (claim_uuid, generic_key, now, now),
            )
            sql = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' "
                "AND name='station_execution_lock_lease'"
            ).fetchone()["sql"]
            assert _NEW_SCOPE_CHECK in sql
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()

    idempotent = InventoryStore(str(database))
    idempotent.close()
