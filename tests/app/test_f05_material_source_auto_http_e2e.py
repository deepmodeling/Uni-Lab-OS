"""自动物料来源（MaterialSource）的公开 HTTP 准入重试合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.app.test_f05_material_source_http_e2e import (
    HOLDER_NODE_UUID,
    HOLDER_TASK_UUID,
    _apply_workflow_graph,
    _create_material,
    _create_resource_template,
    _Runtime,
)
from tests.app.test_f05_material_source_http_e2e import runtime as _shared_runtime
from unilabos.app.scheduler.inventory.domain import MaterialRequirement

SITE_A = "11000000-0000-4000-8000-000000000001"
SITE_B = "11000000-0000-4000-8000-000000000002"


@pytest.fixture()
def automatic_runtime(tmp_path: Path):
    """复用固定来源公开 HTTP 夹具的完整本地运行时装配。"""

    yield from _shared_runtime.__wrapped__(tmp_path)


def test_slot_range_rejects_empty_then_waits_for_reserved_material_on_same_task_and_jobs(
    automatic_runtime: _Runtime,
) -> None:
    """空范围不建任务；补料但被占用时等待，释放后沿原 Task/Jobs 自动分配。"""

    runtime = automatic_runtime
    warehouse_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.auto-warehouse",
        display_name="自动分配仓库",
        registry_type="resource",
    )
    material_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.auto-plate",
        display_name="自动分配孔板",
        registry_type="material",
    )
    device_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.auto-reactor",
        display_name="自动分配反应器",
        registry_type="device",
    )
    mount_uuid = _create_material(
        runtime.client,
        resource_template_uuid=warehouse_template_uuid,
        barcode="AUTO-WAREHOUSE-1",
        name="自动分配一号仓库",
    )
    device_uuid = _create_material(
        runtime.client,
        resource_template_uuid=device_template_uuid,
        barcode="AUTO-REACTOR-1",
        name="自动分配反应器",
    )
    with runtime.inventory.store.transaction() as connection:
        for site_uuid, name, order in (
            (SITE_A, "A1", 10),
            (SITE_B, "B1", 5),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-06T00:00:00Z",
                    "2026-08-06T00:00:00Z",
                    mount_uuid,
                    name,
                    order,
                    json.dumps([material_template_uuid]),
                ),
            )
    workflow_uuid = _apply_workflow_graph(
        runtime,
        material_resource_template_uuid=material_template_uuid,
        device_resource_template_uuid=device_template_uuid,
        material_uuid=mount_uuid,
        device_material_uuid=device_uuid,
        mode="existing",
        automatic=True,
        slot_uuids=[SITE_A, SITE_B],
    )

    request = {"workflow_uuid": workflow_uuid, "run_mode": "normal", "meta_data": {}}
    rejected = runtime.client.post(
        "/api/v1/workflow-tasks",
        json=request,
    )
    assert rejected.status_code == 200
    assert rejected.json()["code"] == 3003
    assert rejected.json()["error"]["code"] == "preflight_failed"
    assert rejected.json()["error"]["details"]["code"] == "material_source_empty"
    assert runtime.workflow_store.count_rows("workflow_task") == 0
    assert runtime.workflow_store.count_rows("workflow_node_job") == 0
    assert runtime.dispatcher.dispatched == []

    selected_uuid = _create_material(
        runtime.client,
        resource_template_uuid=material_template_uuid,
        parent_uuid=mount_uuid,
        site_uuid=SITE_B,
        barcode="AUTO-PLATE-1",
        name="自动分配孔板一号",
    )
    runtime.inventory.reserve_workflow(
        HOLDER_TASK_UUID,
        {HOLDER_NODE_UUID: [MaterialRequirement(instance_uuid=selected_uuid)]},
    )

    created = runtime.client.post("/api/v1/workflow-tasks", json=request)
    assert created.status_code == 201, created.json()
    assert created.json()["code"] == 0
    task_uuid = str(created.json()["data"]["uuid"])
    jobs_before = runtime.client.get(
        f"/api/v1/workflow-tasks/{task_uuid}/jobs"
    ).json()["data"]
    task_before = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()[
        "data"
    ]
    source_plan = task_before["execution_plan"]["nodes"][0]
    assert created.json()["data"]["status"] == "pending"
    assert [job["status"] for job in jobs_before] == ["pending", "pending"]
    assert source_plan["param"]["material_uuid"] is None
    assert source_plan["material_requirements"] == [
        {
            "template_id": material_template_uuid,
            "mount_uuid": mount_uuid,
            "site_uuid": "",
            "slot_uuids": [SITE_A, SITE_B],
        }
    ]
    assert runtime.dispatcher.dispatched == []

    runtime.inventory.release_workflow(HOLDER_TASK_UUID, reason="fixture_release")
    rescheduled = runtime.client.post("/api/v1/reschedule")
    jobs_after = runtime.client.get(
        f"/api/v1/workflow-tasks/{task_uuid}/jobs"
    ).json()["data"]

    assert rescheduled.status_code == 200
    assert [job["uuid"] for job in jobs_after] == [
        job["uuid"] for job in jobs_before
    ]
    assert [job["status"] for job in jobs_after] == ["succeeded", "running"]
    assert jobs_after[0]["return_info"]["material"]["uuid"] == selected_uuid
    assert jobs_after[1]["param"] == {"plate": {"uuid": selected_uuid}}
    assert runtime.dispatcher.dispatched[0]["action_args"] == {
        "plate": {"uuid": selected_uuid}
    }
