"""命名库位组、完整库存 Claim 与物料转运结算的核心验收。"""

from __future__ import annotations

import json
from typing import Any

from tests.scheduler_core.conftest import CoreRuntime, persist_task, stable_uuid
from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def material_reference_schema() -> dict[str, Any]:
    """返回会进入执行锁解析的冻结物料引用 Schema。"""

    return {
        "type": "object",
        "x-unilabos-material-lock": True,
        "properties": {"uuid": {"type": "string", "format": "uuid"}},
        "required": ["uuid"],
        "additionalProperties": False,
    }


def transfer_schema() -> dict[str, Any]:
    """返回同时容纳来源注入与目标选择结果的转运动作 Schema。"""

    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "resource": material_reference_schema(),
                    "mount_resource": material_reference_schema(),
                    "source_owner": material_reference_schema(),
                    "source_site_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "default": "",
                    },
                    "source_site": {"type": "string", "default": ""},
                    "site_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "default": "",
                    },
                    "site": {"type": "string", "default": ""},
                },
                "required": ["resource", "mount_resource"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }


def transfer_contract() -> dict[str, Any]:
    """返回不依赖固定参数名猜测的冻结资源合同。"""

    return {
        "version": 1,
        "transfer": {
            "material_param": "resource",
            "source_owner_param": "source_owner",
            "source_site_uuid_param": "source_site_uuid",
            "source_site_name_param": "source_site",
            "target_owner_param": "mount_resource",
            "target_site_uuid_param": "site_uuid",
            "target_site_name_param": "site",
            "gripper_site_role": "robot.gripper",
        },
    }


def transfer_task(
    runtime: CoreRuntime,
    *,
    task_name: str,
    executor: str,
    material_uuid: str,
    target_owner_uuid: str,
    site_uuids: list[str],
    named_group: bool,
) -> tuple[dict[str, Any], str]:
    """持久化并提交一项显式库位或命名组转运。"""

    node_uuid = stable_uuid(f"transfer-node:{task_name}")
    job_uuid = stable_uuid(f"transfer-job:{task_name}")
    param: dict[str, Any] = {
        "resource": {"uuid": material_uuid},
        "mount_resource": {"uuid": target_owner_uuid},
    }
    execution_policy: dict[str, Any] = {}
    if named_group:
        execution_policy = {
            "target_site_group": list(site_uuids),
            "target_site_selection": {
                "version": 1,
                "owner_material_uuid": target_owner_uuid,
                "group_key": "process_input",
                "requested_reference": "",
                "strategy": "sort_order",
                "site_uuids": list(site_uuids),
                "fingerprint": "sha256:scheduler-core-sites",
            },
        }
    else:
        param["site_uuid"] = site_uuids[0]

    node = {
        "uuid": node_uuid,
        "parent_uuid": None,
        "kind": "material_transfer",
        "device_id": executor,
        "material_uuid": runtime.device_materials[executor],
        "action_name": "transfer_resource",
        "action_type": "goal",
        "param": param,
        "param_schema": transfer_schema(),
        "execution_policy": execution_policy,
        "action_resource_contract": transfer_contract(),
        "material_requirements": [],
    }
    job = {
        "uuid": job_uuid,
        "workflow_node_uuid": node_uuid,
        "topological_index": 0,
        "executor_kind": "material_transfer",
        "execution_policy": execution_policy,
        "execution_timeout_seconds": 0,
        "param": {},
    }
    aggregate = runtime.submit_frozen(
        task_name=task_name,
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [node],
            "handles": [],
            "edges": [],
        },
        jobs=[job],
    )
    return aggregate, job_uuid


def test_named_site_group_uses_next_candidate_and_settles_both_tasks(
    core_runtime: CoreRuntime,
) -> None:
    """首选 Site 被另一 Task 的 Claim 占用时选择组内下一 Site。"""

    backend = BackendResourceService(core_runtime.inventory_store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "scheduler-core.site-owner",
                "display_name": "调度核心虚拟库位父资源",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "scheduler-core.transfer-item",
                "display_name": "调度核心虚拟转运物料",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {item["name"]: item["uuid"] for item in templates}
    target_owner = backend.create_material(
        {
            "resource_template_uuid": template_by_name["scheduler-core.site-owner"],
            "barcode": "CORE-TARGET",
            "name": "虚拟目标托盘",
        }
    )
    source_owners = [
        backend.create_material(
            {
                "resource_template_uuid": template_by_name["scheduler-core.site-owner"],
                "barcode": f"CORE-SOURCE-{index}",
                "name": f"虚拟来源托盘 {index}",
            }
        )
        for index in range(2)
    ]
    materials = [
        backend.create_material(
            {
                "resource_template_uuid": template_by_name[
                    "scheduler-core.transfer-item"
                ],
                "barcode": f"CORE-ITEM-{index}",
                "name": f"虚拟转运物料 {index}",
            }
        )
        for index in range(2)
    ]
    target_sites = [stable_uuid("target-site:a"), stable_uuid("target-site:b")]
    source_sites = [stable_uuid("source-site:a"), stable_uuid("source-site:b")]
    gripper_sites = [stable_uuid("gripper:robot-a"), stable_uuid("gripper:reactor-a")]
    now = "2027-01-15T08:00:00Z"
    item_template_uuid = template_by_name["scheduler-core.transfer-item"]
    with core_runtime.inventory_store.transaction() as connection:
        for index, site_uuid in enumerate(target_sites):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,?,?,?,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    now,
                    now,
                    json.dumps({"unilab": {"site_groups": ["process_input"]}}),
                    target_owner["uuid"],
                    f"TARGET-{index + 1}",
                    index,
                    json.dumps([item_template_uuid]),
                ),
            )
        for index, site_uuid in enumerate(source_sites):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    now,
                    now,
                    "{}",
                    source_owners[index]["uuid"],
                    f"SOURCE-{index + 1}",
                    index,
                    json.dumps([item_template_uuid]),
                    materials[index]["uuid"],
                ),
            )
        for site_uuid, executor in zip(
            gripper_sites,
            ("robot-a", "reactor-a"),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,?,?,0,'[]',NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    now,
                    now,
                    json.dumps({"unilab": {"resource_role": "robot.gripper"}}),
                    core_runtime.device_materials[executor],
                    "GRIPPER",
                ),
            )

    first_task, first_job = transfer_task(
        core_runtime,
        task_name="site-first",
        executor="robot-a",
        material_uuid=materials[0]["uuid"],
        target_owner_uuid=target_owner["uuid"],
        site_uuids=[target_sites[0]],
        named_group=False,
    )
    second_task, second_job = transfer_task(
        core_runtime,
        task_name="site-fallback",
        executor="reactor-a",
        material_uuid=materials[1]["uuid"],
        target_owner_uuid=target_owner["uuid"],
        site_uuids=target_sites,
        named_group=True,
    )

    assert first_task["task"]["status"] == "running"
    assert second_task["task"]["status"] == "running"
    assert [payload["job_id"] for payload in core_runtime.dispatcher.dispatched] == [
        first_job,
        second_job,
    ]
    assert (
        core_runtime.dispatcher.dispatched[1]["action_args"]["site_uuid"]
        == (target_sites[1])
    )
    assert core_runtime.dispatcher.dispatched[1]["action_args"]["site"] == "TARGET-2"
    second_payload = core_runtime.dispatcher.dispatched[1]
    persisted_second = core_runtime.workflow_store.get_job(second_job)
    persisted_claim = TaskRuntimeProjection(
        core_runtime.workflow_store
    ).get_execution_claim(second_job)
    assert persisted_claim is not None
    assert second_payload["claim_uuid"] == persisted_claim["claim_uuid"]
    assert second_payload["fences"] == persisted_claim["fences"]
    assert second_payload["effect_uuid"] == persisted_second["dispatch_effect_uuid"]
    assert second_payload["fences"]
    assert {fence["lock_key"] for fence in second_payload["fences"]} >= {
        f"material/{materials[1]['uuid']}/exclusive",
        (f"material/{target_owner['uuid']}/site/{target_sites[1]}/exclusive"),
    }

    succeeded = CommittedJobOutcome(
        outcome="succeeded",
        return_info={},
        error_info=[],
        unknown_command_ids=[],
    )
    core_runtime.scheduler.on_job_outcome(first_job, succeeded)
    core_runtime.scheduler.on_job_outcome(second_job, succeeded)

    occupied = core_runtime.inventory_store.query_all(
        "SELECT uuid,occupied_material_uuid FROM site WHERE uuid IN (?,?) "
        "ORDER BY sort_order",
        tuple(target_sites),
    )
    assert occupied == [
        {"uuid": target_sites[0], "occupied_material_uuid": materials[0]["uuid"]},
        {"uuid": target_sites[1], "occupied_material_uuid": materials[1]["uuid"]},
    ]
    assert [
        row["state"]
        for row in core_runtime.inventory_store.query_all(
            "SELECT state FROM station_execution_claim ORDER BY job_uuid"
        )
    ] == ["released", "released"]


def test_missing_virtual_stock_waits_then_reuses_the_same_task_and_job(
    core_runtime: CoreRuntime,
) -> None:
    """库存补齐前不派发，补齐后原身份预留、执行并消费。"""

    task_name = "inventory-retry"
    task_uuid = stable_uuid(f"task:{task_name}")
    job_uuid = stable_uuid(f"job:{task_name}:0")
    stock_template = "scheduler-core-stock"
    lot_uuid = "scheduler-core-lot"
    task = persist_task(
        core_runtime.workflow_store,
        task_name=task_name,
        devices=["reactor-a"],
        device_material_uuids=[core_runtime.device_materials["reactor-a"]],
        material_requirements_by_node=[
            [{"template_id": stock_template, "quantity": 2.0, "unit": "mL"}]
        ],
    )

    blocked = core_runtime.bridge.submit(task)

    assert blocked["task"]["uuid"] == task_uuid
    assert blocked["task"]["status"] == "pending"
    assert core_runtime.dispatcher.dispatched == []
    assert (
        core_runtime.inventory_store.query_all("SELECT * FROM inventory_reservation")
        == []
    )

    core_runtime.inventory.inbound_lot(
        template_id=stock_template,
        quantity=2.0,
        unit="mL",
        lot_id=lot_uuid,
    )
    retried = core_runtime.bridge.retry_admission(task_uuid)

    assert retried["task"]["uuid"] == task_uuid
    assert [job["uuid"] for job in retried["jobs"]] == [job_uuid]
    assert [payload["job_id"] for payload in core_runtime.dispatcher.dispatched] == [
        job_uuid
    ]
    assert core_runtime.inventory_store.query_all(
        "SELECT workflow_id,node_id,status FROM inventory_reservation"
    ) == [
        {
            "workflow_id": task_uuid,
            "node_id": stable_uuid(f"node:{task_name}:0"),
            "status": "active",
        }
    ]

    core_runtime.scheduler.on_job_finished(job_uuid, True, {})

    assert core_runtime.inventory_store.query_one(
        "SELECT status FROM inventory_reservation WHERE workflow_id=?",
        (task_uuid,),
    ) == {"status": "consumed"}
    assert core_runtime.inventory_store.query_one(
        "SELECT quantity_total,quantity_available,quantity_reserved "
        "FROM inventory_lot WHERE lot_id=?",
        (lot_uuid,),
    ) == {
        "quantity_total": 0.0,
        "quantity_available": 0.0,
        "quantity_reserved": 0.0,
    }
