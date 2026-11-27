"""Material I/O 内建人工操作的运行时闭环测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, build_core_runtime, stable_uuid
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.workflow.resource_lock_plan import (
    RESOURCE_PLAN_CAPABILITY,
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)
from unilabos.workflow.service import WorkflowService


def _install_load_target(
    runtime: CoreRuntime,
    name: str,
    *,
    category: list[str] | None = None,
) -> tuple[str, str, str]:
    backend = BackendResourceService(runtime.inventory_store)
    template_uuid = backend.sync_resource_templates(
        [
            {
                "id": f"runtime-container-{name}",
                "display_name": "Runtime container",
                "registry_type": "resource",
                "available_sites": [],
                "category": category or [],
            }
        ]
    )["templates"][0]["uuid"]
    mount_template_uuid = backend.sync_resource_templates(
        [
            {
                "id": f"runtime-mount-{name}",
                "display_name": "Runtime mount",
                "registry_type": "resource",
                "available_sites": [],
            }
        ]
    )["templates"][0]["uuid"]
    mount_uuid = backend.create_material(
        {
            "resource_template_uuid": mount_template_uuid,
            "name": f"Mount {name}",
        }
    )["uuid"]
    site_uuid = stable_uuid(f"material-io-site:{name}")
    now = "2026-09-18T00:00:00.000Z"
    with runtime.inventory_store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,name,sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,NULL,NULL,'{}',?,'SOURCE',0,?,NULL,0,0,0,0,0,0)
            """,
            (
                site_uuid,
                now,
                now,
                mount_uuid,
                json.dumps([template_uuid]),
            ),
        )
    return template_uuid, mount_uuid, site_uuid


def _submit_material_io(
    runtime: CoreRuntime,
    *,
    task_name: str,
    param: dict[str, Any],
    timeout_seconds: int = 3600,
) -> tuple[dict[str, Any], str]:
    node_uuid = stable_uuid(f"material-io-node:{task_name}")
    job_uuid = stable_uuid(f"material-io-job:{task_name}")
    frozen_param = {**param, "timeout_seconds": timeout_seconds}
    resource_plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {
                "workflow_uuid": stable_uuid(f"material-io-workflow:{task_name}"),
                "nodes": [{"uuid": node_uuid}],
            }
        ),
        {},
    )
    aggregate = runtime.submit_frozen(
        task_name=task_name,
        execution_plan={
            "version": 1,
            "capabilities": [RESOURCE_PLAN_CAPABILITY],
            "run_mode": "normal",
            "target_node_uuid": None,
            "resource_plan": serialize_resource_plan(resource_plan),
            "nodes": [
                {
                    "uuid": node_uuid,
                    "kind": "material_io",
                    "action_name": "material_io",
                    "action_type": "material_io",
                    "param": frozen_param,
                    "manual_confirmation": {
                        "timeout_seconds": timeout_seconds
                    },
                    "resource_plan_id": resource_plan.plan_id,
                    "resource_interval_ids": [],
                    "resource_acquire_set_id": "",
                }
            ],
            "handles": [],
            "edges": [],
        },
        jobs=[
            {
                "uuid": job_uuid,
                "workflow_node_uuid": node_uuid,
                "topological_index": 0,
                "executor_kind": "material_io",
                "execution_policy": {},
                "execution_timeout_seconds": 0,
                "param": frozen_param,
            }
        ],
    )
    return aggregate, job_uuid


def _load_param(
    template_uuid: str,
    mount_uuid: str,
    site_uuid: str,
) -> dict[str, Any]:
    return {
        "mode": "load",
        "resource_template_uuid": template_uuid,
        "mount": {"uuid": mount_uuid},
        "site": "SOURCE",
        "site_uuid": site_uuid,
        "material": None,
        "disposition": None,
        "scope": None,
        "timeout_seconds": 3600,
    }


def test_load_by_site_name_when_site_uuid_key_is_present(
    core_runtime: CoreRuntime,
) -> None:
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "site-name"
    )
    param = _load_param(template_uuid, mount_uuid, site_uuid)
    param["site"] = "SOURCE"
    param["site_uuid"] = None
    _aggregate, job_uuid = _submit_material_io(
        core_runtime,
        task_name="material-io-load-by-name",
        param=param,
    )
    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")
    job = next(item for item in approved["jobs"] if item["uuid"] == job_uuid)
    material_uuid = job["return_info"]["material"]["uuid"]
    occupied = core_runtime.inventory_store.query_one(
        "SELECT uuid FROM site WHERE occupied_material_uuid=?",
        (material_uuid,),
    )
    assert occupied["uuid"] == site_uuid

    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "site-name-and-uuid"
    )
    matched = _load_param(template_uuid, mount_uuid, site_uuid)
    matched["site"] = "source"
    matched["site_uuid"] = site_uuid
    _aggregate, matched_job = _submit_material_io(
        core_runtime,
        task_name="material-io-load-name-and-uuid",
        param=matched,
    )
    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(matched_job, action="approve")
    matched_material = next(
        item for item in approved["jobs"] if item["uuid"] == matched_job
    )["return_info"]["material"]["uuid"]
    occupied = core_runtime.inventory_store.query_one(
        "SELECT uuid FROM site WHERE occupied_material_uuid=?",
        (matched_material,),
    )
    assert occupied["uuid"] == site_uuid


def test_load_approve_executes_locally_and_never_dispatches(
    core_runtime: CoreRuntime,
) -> None:
    def reject_device_resolver(*_args: Any) -> Any:
        raise AssertionError("material_io 不得解析在线设备")

    core_runtime.scheduler._device_target_resolver = reject_device_resolver
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "approve"
    )
    aggregate, job_uuid = _submit_material_io(
        core_runtime,
        task_name="material-io-load",
        param=_load_param(template_uuid, mount_uuid, site_uuid),
    )

    waiting = aggregate["jobs"][0]
    assert waiting["status"] == "running"
    assert waiting["executor_kind"] == "material_io"
    assert waiting["manual_confirmation"]["status"] == "pending"
    assert core_runtime.dispatcher.dispatched == []

    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")

    job = next(item for item in approved["jobs"] if item["uuid"] == job_uuid)
    material_uuid = job["return_info"]["material"]["uuid"]
    assert approved["task"]["status"] == "succeeded"
    assert job["status"] == "succeeded"
    assert job["executor_kind"] == "material_io"
    assert core_runtime.dispatcher.dispatched == []
    assert core_runtime.inventory_store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (site_uuid,),
    ) == {"occupied_material_uuid": material_uuid}
    assert core_runtime.inventory_store.query_all(
        "SELECT DISTINCT state FROM station_execution_claim"
    ) == [{"state": "released"}]
    replay = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")
    assert replay["jobs"][0]["return_info"] == job["return_info"]
    assert core_runtime.inventory_store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    ) == {"count": 1}


def test_unload_approve_and_reject_or_timeout_do_not_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "unload"
    )
    backend = BackendResourceService(core_runtime.inventory_store)
    loaded = backend.material_io_load(
        {
            "template_uuid": template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": site_uuid,
            "name": "Unload me",
            "workflow_id": "setup",
            "node_id": "load",
            "attempt": 1,
        }
    )
    material_uuid = loaded["material_uuid"]
    unload_param = {
        "mode": "unload",
        "resource_template_uuid": None,
        "mount": {"uuid": mount_uuid},
        "site": None,
        "material": {"uuid": material_uuid},
        "disposition": "detach",
        "scope": "material_only",
        "timeout_seconds": 3600,
    }
    _aggregate, job_uuid = _submit_material_io(
        core_runtime,
        task_name="material-io-unload",
        param=unload_param,
    )
    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")
    assert approved["task"]["status"] == "succeeded"
    assert approved["jobs"][0]["return_info"]["material"] == {
        "uuid": material_uuid
    }
    assert core_runtime.inventory_store.query_one(
        "SELECT parent_uuid FROM material WHERE uuid=?", (material_uuid,)
    ) == {"parent_uuid": None}
    assert core_runtime.dispatcher.dispatched == []

    reject_template, reject_mount, reject_site = _install_load_target(
        core_runtime, "reject"
    )
    rejected, reject_job = _submit_material_io(
        core_runtime,
        task_name="material-io-reject",
        param=_load_param(reject_template, reject_mount, reject_site),
    )
    assert rejected["jobs"][0]["status"] == "running"
    result = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(reject_job, action="reject")
    assert result["task"]["status"] == "canceled"
    assert core_runtime.inventory_store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (reject_site,)
    ) == {"occupied_material_uuid": None}

    timeout_template, timeout_mount, timeout_site = _install_load_target(
        core_runtime, "timeout"
    )
    _submit_material_io(
        core_runtime,
        task_name="material-io-timeout",
        param=_load_param(timeout_template, timeout_mount, timeout_site),
        timeout_seconds=5,
    )
    core_runtime.restart_scheduler()
    core_runtime.clock.advance(5)
    assert core_runtime.inventory_store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (timeout_site,)
    ) == {"occupied_material_uuid": None}
    assert core_runtime.dispatcher.dispatched == []


def test_same_site_claim_blocks_concurrent_material_io_until_release(
    core_runtime: CoreRuntime,
) -> None:
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "concurrent"
    )
    first, first_job = _submit_material_io(
        core_runtime,
        task_name="material-io-first",
        param=_load_param(template_uuid, mount_uuid, site_uuid),
    )
    second, second_job = _submit_material_io(
        core_runtime,
        task_name="material-io-second",
        param=_load_param(template_uuid, mount_uuid, site_uuid),
    )
    assert first["jobs"][0]["manual_confirmation"]["status"] == "pending"
    assert second["jobs"][0]["status"] == "pending"
    assert "manual_confirmation" not in second["jobs"][0]

    WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(first_job, action="reject")
    core_runtime.bridge.reschedule()
    waiting = core_runtime.workflow_store.get_job(second_job)
    assert waiting["status"] == "running"


def test_effect_commit_replays_after_scheduler_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_core_runtime(tmp_path)
    try:
        template_uuid, mount_uuid, site_uuid = _install_load_target(
            runtime, "restart"
        )
        _aggregate, job_uuid = _submit_material_io(
            runtime,
            task_name="material-io-restart",
            param=_load_param(template_uuid, mount_uuid, site_uuid),
        )
        original_finished = runtime.scheduler.on_job_finished

        def fail_after_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("injected post-effect crash")

        monkeypatch.setattr(runtime.scheduler, "on_job_finished", fail_after_effect)
        with pytest.raises(RuntimeError, match="post-effect"):
            runtime.bridge.decide_manual_confirmation(job_uuid, action="approve")
        assert runtime.inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
        ) == {"count": 1}

        monkeypatch.setattr(runtime.scheduler, "on_job_finished", original_finished)
        runtime.restart_scheduler()

        job = runtime.workflow_store.get_job(job_uuid)
        assert job["status"] == "succeeded"
        assert job["return_info"]["material"]["uuid"]
        assert runtime.inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
        ) == {"count": 1}
        assert runtime.dispatcher.dispatched == []
    finally:
        runtime.close()


def test_load_approve_can_create_reagent_on_container(
    core_runtime: CoreRuntime,
) -> None:
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "reagent", category=["container"]
    )
    param = _load_param(template_uuid, mount_uuid, site_uuid)
    param["reagent"] = {
        "name": "乙醇",
        "cas": "64-17-5",
        "quantity": 12,
        "quantity_unit": "mL",
    }
    _aggregate, job_uuid = _submit_material_io(
        core_runtime,
        task_name="material-io-load-reagent",
        param=param,
    )
    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")
    job = next(item for item in approved["jobs"] if item["uuid"] == job_uuid)
    assert approved["task"]["status"] == "succeeded"
    assert job["return_info"]["reagent_info_created"] is True
    assert job["return_info"]["reagent"]["quantity"] == 12
    material_uuid = job["return_info"]["material"]["uuid"]
    assert core_runtime.inventory_store.query_one(
        "SELECT quantity,quantity_unit FROM reagent WHERE material_uuid=?",
        (material_uuid,),
    ) == {"quantity": 12, "quantity_unit": "mL"}
    assert core_runtime.inventory_store.query_one(
        "SELECT name,cas FROM reagent_info WHERE uuid=?",
        (job["return_info"]["reagent_info"]["uuid"],),
    ) == {"name": "乙醇", "cas": "64-17-5"}


def test_refill_approve_increments_reagent_without_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    template_uuid, mount_uuid, site_uuid = _install_load_target(
        core_runtime, "refill", category=["container"]
    )
    backend = BackendResourceService(core_runtime.inventory_store)
    loaded = backend.material_io_load(
        {
            "template_uuid": template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": site_uuid,
            "name": "Refill me",
            "workflow_id": "setup",
            "node_id": "load",
            "attempt": 1,
            "reagent": {
                "name": "乙醇",
                "cas": "64-17-5",
                "quantity": 10,
                "quantity_unit": "mL",
                "container_capacity": {"max_volume_ul": 100000},
            },
        }
    )
    refill_param = {
        "mode": "refill",
        "resource_template_uuid": None,
        "mount": {"uuid": mount_uuid},
        "site": None,
        "material": {"uuid": loaded["material_uuid"]},
        "disposition": None,
        "scope": None,
        "timeout_seconds": 3600,
        "reagent": {"quantity": 15, "quantity_unit": "mL"},
    }
    _aggregate, job_uuid = _submit_material_io(
        core_runtime,
        task_name="material-io-refill",
        param=refill_param,
    )
    approved = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    ).decide_manual_confirmation(job_uuid, action="approve")
    job = next(item for item in approved["jobs"] if item["uuid"] == job_uuid)
    assert approved["task"]["status"] == "succeeded"
    assert job["status"] == "succeeded"
    assert job["return_info"]["quantity"] == 25
    assert job["return_info"]["quantity_added"] == 15
    assert job["return_info"]["reagent"]["uuid"] == loaded["reagent"]["uuid"]
    assert core_runtime.dispatcher.dispatched == []
    assert core_runtime.inventory_store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (site_uuid,),
    ) == {"occupied_material_uuid": loaded["material_uuid"]}
    assert core_runtime.inventory_store.query_one(
        "SELECT quantity FROM reagent WHERE uuid=?",
        (loaded["reagent"]["uuid"],),
    ) == {"quantity": 25}
