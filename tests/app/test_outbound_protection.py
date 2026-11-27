"""任务独占向交接的无空档移交。"""

import json

import pytest

from tests.app.test_station_handoff import package, prepared, setup, uid
from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.domain import (
    MaterialClaimedByOtherTask,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService


def admission(s, task, policy="task_exclusive", local=None):
    local = local or s["carrier"]["uuid"]
    InventoryService(s["stores"]["source"]).admit_material_sources(
        task,
        [
            MaterialSourceAdmissionRequest(
                node_id=uid(),
                resource_template_uuid=s["templates"]["source"]["test.carrier"],
                custody_policy=policy,
                requirement=MaterialRequirement(
                    template_id=s["templates"]["source"]["test.carrier"], instance_uuid=local
                ),
            )
        ],
    )


def test_frozen_transfer_endpoint_preserves_unrelated_custody(setup):
    """同一仓库的独立库位可执行，整仓公共修改和外国占用库位仍受保护。"""
    from dataclasses import replace

    from unilabos.app.scheduler.inventory.dispatch_admission import DispatchAdmissionRequest, DispatchResource
    from unilabos.app.scheduler.resource_lock import material_lock_key, site_lock_key

    s, task_a, task_b, other_site = setup, uid(), uid(), uid()
    store = s["stores"]["source"]
    owner = store.query_one("SELECT material_uuid FROM site WHERE uuid=?", (s["sites"]["source"],))["material_uuid"]
    with store.transaction() as c:
        c.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name) VALUES (?,'2026-01-01','2026-01-01',?,'独立库位')",
                  (other_site, owner))
    admission(s, task_a)
    request = DispatchAdmissionRequest(effect_uuid=uid(), task_uuid=task_b, job_uuid=uid(), attempt=1,
        parameter_hash="hash", expected_change_set={"kind": "no_inventory_change"}, resources=(
            DispatchResource(material_lock_key(owner), "material", owner),
            DispatchResource(site_lock_key(owner, other_site), "material_site", owner, other_site),
        ))
    authority = InventoryService(store).station_resources
    assert authority.acquire_dispatch_permit(request).wait_code == "task_material_claimed"
    occupied = s["sites"]["source"]
    wrong_site = replace(request, resources=(request.resources[0],
        DispatchResource(site_lock_key(owner, occupied), "material_site", owner, occupied)),
        custody_endpoint_site_uuids=(occupied,))
    assert authority.acquire_dispatch_permit(wrong_site).wait_code == "task_material_claimed"
    with store.transaction() as c, pytest.raises(InventoryMutationConflict):
        assert_inventory_mutation_unclaimed(c, material_uuids=[owner])
    decision = authority.acquire_dispatch_permit(replace(request, custody_endpoint_site_uuids=(other_site,)))
    assert decision.acquired, decision.wait_message
    assert store.query_one("SELECT status FROM inventory_material_source_binding WHERE workflow_id=?", (task_a,))["status"] == "active"


def test_outbound_requires_actual_task_exclusive_admission(setup):
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task = setup, uid()
    admission(s, task, "shared_source")
    with pytest.raises(ValueError, match="独占"):
        protect_outputs(
            s["stores"]["source"],
            invocation_uuid=uid(),
            task_uuid=task,
            outputs=[
                {
                    "local_material_uuid": s["carrier"]["uuid"],
                    "resource_template_uuid": s["templates"]["source"]["test.carrier"],
                }
            ],
        )


def test_outbound_protection_survives_task_release_then_transfers_to_handoff(setup):
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task, invocation = setup, uid(), uid()
    admission(s, task)
    protect_outputs(
        s["stores"]["source"],
        invocation_uuid=invocation,
        task_uuid=task,
        outputs=[
            {
                "local_material_uuid": s["carrier"]["uuid"],
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            }
        ],
    )
    InventoryService(s["stores"]["source"]).release_workflow(task, reason="workflow_succeeded")
    with s["stores"]["source"].transaction() as c:
        with pytest.raises(InventoryMutationConflict):
            assert_inventory_mutation_unclaimed(c, material_uuids=[s["carrier"]["uuid"]])
    s["payload"]["items"][0]["source_material_refs"][0]["local_revision"] = s["stores"]["source"].query_one(
        "SELECT aggregate_version FROM material WHERE uuid=?", (s["carrier"]["uuid"],)
    )["aggregate_version"]
    prepared(s)
    s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"])
    rows = s["stores"]["source"].query_all("SELECT * FROM inventory_reservation WHERE workflow_id=?", (invocation,))
    assert rows and all(r["status"] == "released" for r in rows)
    assert s["stores"]["source"].query_one("SELECT 1 FROM station_handoff_resource WHERE active=1")


def test_full_tree_foreign_claim_blocks_outbound(setup):
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task = setup, uid()
    child = BackendResourceService(s["stores"]["source"]).create_material(
        {
            "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            "name": "sample",
            "parent_uuid": s["carrier"]["uuid"],
        }
    )
    admission(s, uid(), "shared_source", child["uuid"])
    with pytest.raises(MaterialClaimedByOtherTask):
        admission(s, task)
    with pytest.raises(ValueError):
        protect_outputs(
            s["stores"]["source"],
            invocation_uuid=uid(),
            task_uuid=task,
            outputs=[
                {
                    "local_material_uuid": s["carrier"]["uuid"],
                    "resource_template_uuid": s["templates"]["source"]["test.carrier"],
                }
            ],
        )


def test_inventory_release_guard_runs_before_any_reservation_is_released(setup):
    s, task = setup, uid()
    admission(s, task)
    inventory = InventoryService(s["stores"]["source"])
    inventory.set_workflow_release_guard(lambda _: False)
    result = inventory.release_workflow(task, reason="workflow_succeeded")
    assert result["status"] == "held"
    assert s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND status='active'", (task,)
    )


def test_produced_independent_output_acquires_real_exclusive_source(setup):
    from unilabos.app.scheduler.inventory.outbound import admit_produced_outputs, protect_outputs

    s, task, invocation = setup, uid(), uid()
    outputs = [
        {
            "local_material_uuid": s["carrier"]["uuid"],
            "resource_template_uuid": s["templates"]["source"]["test.carrier"],
        }
    ]
    inventory = InventoryService(s["stores"]["source"])
    admit_produced_outputs(inventory, task_uuid=task, outputs=outputs)
    assert s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND material_uuid=? AND custody_policy='task_exclusive' AND status='active'",
        (task, s["carrier"]["uuid"]),
    )
    protect_outputs(s["stores"]["source"], invocation_uuid=invocation, task_uuid=task, outputs=outputs)


def test_production_hook_cannot_promote_shared_source_to_outbound(setup):
    from unilabos.app.scheduler.inventory.outbound import admit_produced_outputs

    s, task = setup, uid()
    admission(s, task, "shared_source")
    with pytest.raises(ValueError, match="共享"):
        admit_produced_outputs(
            InventoryService(s["stores"]["source"]),
            task_uuid=task,
            outputs=[
                {
                    "local_material_uuid": s["carrier"]["uuid"],
                    "resource_template_uuid": s["templates"]["source"]["test.carrier"],
                }
            ],
        )


def test_outbound_protection_blocks_new_task_source_admission(setup):
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task = setup, uid()
    admission(s, task)
    protect_outputs(
        s["stores"]["source"],
        invocation_uuid=uid(),
        task_uuid=task,
        outputs=[
            {
                "local_material_uuid": s["carrier"]["uuid"],
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            }
        ],
    )
    InventoryService(s["stores"]["source"]).release_workflow(task, reason="workflow_succeeded")
    with pytest.raises(MaterialClaimedByOtherTask):
        admission(s, uid())


def test_output_template_conflict_is_checked_before_protection(setup):
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task = setup, uid()
    admission(s, task)
    with pytest.raises(ValueError, match="模板"):
        protect_outputs(
            s["stores"]["source"],
            invocation_uuid=uid(),
            task_uuid=task,
            outputs=[{"local_material_uuid": s["carrier"]["uuid"], "resource_template_uuid": uid()}],
        )
    assert not s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_reservation WHERE node_id LIKE 'center-outbound:%'"
    )


def test_runtime_dynamic_output_admission_excludes_plain_input_uuid(setup):
    from unilabos.app.scheduler.inventory.outbound import admit_dynamic_job_outputs

    s, task, node, handle = setup, uid(), uid(), uid()
    template = s["templates"]["source"]["test.carrier"]
    port = {
        "name": "output",
        "direction": "output",
        "value_path": "$.result",
        "local_value_kind": "material_uuid",
        "cardinality": "one",
        "resource_template_uuid": template,
    }
    task_row = {
        "uuid": task,
        "create_time": "2020-01-01T00:00:00Z",
        "input": {"plain": s["carrier"]["uuid"]},
        "workflow_snapshot": {
            "workflow": {
                "meta_data": {
                    "unilab": {
                        "material_ports": [port],
                        "input_contract": {"version": 1, "parameters": []},
                        "output_contract": {
                            "version": 1,
                            "outputs": [{"name": "result", "schema": {"type": "string"}}],
                        },
                    }
                }
            }
        },
        "execution_plan": {
            "nodes": [
                {
                    "kind": "workflow_output",
                    "output_bindings": {
                        "result": {"kind": "node_output", "workflow_node_uuid": node, "source_handle_uuid": handle}
                    },
                }
            ],
            "handles": [{"node_uuid": node, "template_handle_uuid": handle, "data_key": "result"}],
        },
    }
    job = {"uuid": uid(), "workflow_node_uuid": node, "executor_kind": "device_action"}
    inventory = InventoryService(s["stores"]["source"])
    admit_dynamic_job_outputs(inventory, task=task_row, job=job, return_info={"result": s["carrier"]["uuid"]})
    assert not s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=?", (task,)
    )
    task_row["input"] = {}
    admit_dynamic_job_outputs(inventory, task=task_row, job=job, return_info={"result": s["carrier"]["uuid"]})
    assert not s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND custody_policy='task_exclusive'",
        (task,),
    )
    store = s["stores"]["source"]

    def production_event(*, event="material.load", owner=task, producer=job["uuid"]):
        with store.transaction() as c:
            store.append_material_ledger(
                c,
                material_uuid=s["carrier"]["uuid"],
                name="producer output",
                event_type=event,
                quantity_delta=1,
                workflow_task_uuid=owner,
                workflow_node_job_uuid=producer,
            )

    production_event(owner=uid())
    production_event(producer=uid())
    production_event(event="current_substance.created")
    admit_dynamic_job_outputs(inventory, task=task_row, job=job, return_info={"result": s["carrier"]["uuid"]})
    assert not store.query_one("SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=?", (task,))
    production_event()
    admit_dynamic_job_outputs(inventory, task=task_row, job=job, return_info={"result": s["carrier"]["uuid"]})
    assert store.query_one(
        "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND custody_policy='task_exclusive'",
        (task,),
    )


def test_cancel_outbound_settlement_requires_original_versions(setup):
    from unilabos.app.scheduler.inventory.outbound import protect_outputs, settle_canceled_outbound

    s, task, invocation = setup, uid(), uid()
    admission(s, task)
    protect_outputs(
        s["stores"]["source"],
        invocation_uuid=invocation,
        task_uuid=task,
        outputs=[
            {
                "local_material_uuid": s["carrier"]["uuid"],
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            }
        ],
    )
    InventoryService(s["stores"]["source"]).release_workflow(task, reason="workflow_succeeded")
    with s["stores"]["source"].transaction() as c:
        c.execute("UPDATE material SET aggregate_version=aggregate_version+1 WHERE uuid=?", (s["carrier"]["uuid"],))
    with s["stores"]["source"].transaction() as c:
        with pytest.raises(ValueError, match="版本|状态"):
            settle_canceled_outbound(c, invocation_uuid=invocation, task_uuid=task, evidence_refs=["verified:source"])
    assert s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_reservation WHERE workflow_id=? AND status='active'", (invocation,)
    )


def test_workflow_handoff_cannot_bypass_original_output_protection(setup):
    s = setup
    prepared(s)
    s["payload"].update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="node_output")
    with pytest.raises(ValueError, match="产物|独占"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"])
    assert s["stores"]["source"].query_one("SELECT 1 FROM station_handoff_resource WHERE active=1") is None


def test_station_stock_transfer_freezes_without_workflow_output(setup):
    """本站库存直接交接，仍在同一事务冻结完整载体，不能被其他动作领取。"""
    from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER

    s = setup
    s["payload"].update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="station_material")
    item = s["payload"]["items"][0]
    item["source_snapshot"] = s["auth"]["source"].export_source_snapshot(
        container_uuid=item["container_uuid"], source_material_refs=item["source_material_refs"]
    )
    s["plans"][item["handoff_item_uuid"]]["adapter"] = {
        "kind": ADAPTER, "site_names": {s["carrier"]["uuid"]: {}}, "reagent_info_uuids": {},
    }
    prepared(s)
    event = s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"])
    assert event["payload"]["kind"] == "released"
    assert s["auth"]["source"].freeze_source(
        package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"]
    ) == event
    changed = {**s["payload"], "transfer_source_kind": "node_output", "revision": 3}
    with pytest.raises(ValueError, match="固定身份"):
        s["auth"]["source"].apply_center_snapshot(package(changed))
    assert s["stores"]["source"].query_one("SELECT 1 FROM inventory_reservation WHERE node_id LIKE 'center-outbound:%'") is None
    with pytest.raises(InventoryMutationConflict), s["stores"]["source"].transaction() as c:
        assert_inventory_mutation_unclaimed(c, material_uuids=[s["carrier"]["uuid"]])


def test_station_stock_transfer_cannot_steal_pending_workflow_output(setup):
    """来源声明不能把尚未正式补报的上游产物变成可自由搬运的本站库存。"""
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task, invocation = setup, uid(), uid()
    admission(s, task)
    protect_outputs(s["stores"]["source"], invocation_uuid=invocation, task_uuid=task, outputs=[{
        "local_material_uuid": s["carrier"]["uuid"],
        "resource_template_uuid": s["templates"]["source"]["test.carrier"],
    }])
    InventoryService(s["stores"]["source"]).release_workflow(task, reason="workflow_succeeded")
    s["payload"]["items"][0]["source_material_refs"][0]["local_revision"] = s["stores"]["source"].query_one(
        "SELECT aggregate_version FROM material WHERE uuid=?", (s["carrier"]["uuid"],)
    )["aggregate_version"]
    s["payload"].update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="station_material")
    prepared(s)
    with pytest.raises(ValueError, match="正式补报"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"])
    assert s["stores"]["source"].query_one("SELECT 1 FROM station_handoff_resource WHERE active=1") is None
    assert s["stores"]["source"].query_one(
        "SELECT 1 FROM inventory_reservation WHERE workflow_id=? AND status='active'", (invocation,)
    )


def child_material(s):
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService

    return BackendResourceService(s["stores"]["source"]).create_material(
        {
            "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            "name": "nested sample",
            "parent_uuid": s["carrier"]["uuid"],
        }
    )["uuid"]


def test_exclusive_carrier_admission_rejects_foreign_shared_descendant(setup):
    s = setup
    child = child_material(s)
    admission(s, uid(), "shared_source", child)
    with pytest.raises(MaterialClaimedByOtherTask):
        admission(s, uid())


def test_exclusive_carrier_blocks_later_foreign_child_admission_and_action(setup):
    from tests.app.test_station_material_inputs import permit

    s = setup
    child = child_material(s)
    owner = uid()
    admission(s, owner)
    with pytest.raises(MaterialClaimedByOtherTask):
        admission(s, uid(), "shared_source", child)
    with s["stores"]["source"].transaction() as c:
        assert not permit(c, uid(), child).acquired
        assert permit(c, owner, child).acquired


def test_exclusive_carrier_blocks_public_descendant_mutation(setup):
    s = setup
    child = child_material(s)
    admission(s, uid())
    with s["stores"]["source"].transaction() as c:
        with pytest.raises(InventoryMutationConflict):
            assert_inventory_mutation_unclaimed(c, material_uuids=[child])


@pytest.mark.parametrize("operation", ["reserve", "release", "consume", "quarantine"])
def test_generic_reservation_operations_cannot_change_outbound_namespace(setup, operation):
    from unilabos.app.scheduler.inventory.domain import CommandRejected
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task, invocation = setup, uid(), uid()
    admission(s, task)
    protect_outputs(
        s["stores"]["source"],
        invocation_uuid=invocation,
        task_uuid=task,
        outputs=[
            {
                "local_material_uuid": s["carrier"]["uuid"],
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            }
        ],
    )
    inventory = InventoryService(s["stores"]["source"])
    node = "center-outbound:" + s["carrier"]["uuid"]
    with pytest.raises(CommandRejected):
        if operation == "reserve":
            inventory.reserve_workflow(invocation, {node: []})
        else:
            getattr(inventory, operation + "_reservation")(invocation, node)
    assert (
        s["stores"]["source"].query_one("SELECT status FROM inventory_reservation WHERE workflow_id=?", (invocation,))[
            "status"
        ]
        == "active"
    )


def test_command_release_cannot_remove_outbound_protection(setup):
    from unilabos.app.scheduler.inventory.commands import execute_command
    from unilabos.app.scheduler.inventory.outbound import protect_outputs

    s, task, invocation = setup, uid(), uid()
    admission(s, task)
    protect_outputs(
        s["stores"]["source"],
        invocation_uuid=invocation,
        task_uuid=task,
        outputs=[
            {
                "local_material_uuid": s["carrier"]["uuid"],
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            }
        ],
    )
    response = execute_command(
        InventoryService(s["stores"]["source"]),
        {
            "command_id": uid(),
            "type": "inventory.release",
            "payload": {"workflow_id": invocation, "node_id": "center-outbound:" + s["carrier"]["uuid"]},
        },
    )
    assert response["status"] == "rejected"
    assert "出站产物保护" in str(response["error"])
    assert (
        s["stores"]["source"].query_one("SELECT status FROM inventory_reservation WHERE workflow_id=?", (invocation,))[
            "status"
        ]
        == "active"
    )


def test_exclusive_source_retains_original_descendant_after_owner_rearrangement(setup):
    from tests.app.test_station_material_inputs import permit

    s = setup
    child = child_material(s)
    owner = uid()
    admission(s, owner)
    # 模拟原任务合法动作已经结算后的新父链；原来源保管不能因此凭空失去内容保护。
    with s["stores"]["source"].transaction() as c:
        c.execute("UPDATE material SET parent_uuid=NULL WHERE uuid=?", (child,))
    with pytest.raises(MaterialClaimedByOtherTask):
        admission(s, uid(), "shared_source", child)
    with s["stores"]["source"].transaction() as c:
        assert not permit(c, uid(), child).acquired
        with pytest.raises(InventoryMutationConflict):
            assert_inventory_mutation_unclaimed(c, material_uuids=[child])


@pytest.mark.parametrize('reservation_state', ['active', 'released'])
def test_cancel_settlement_cannot_release_detached_unsafe_original_content(setup, reservation_state):
    from unilabos.app.center_client.material_settlement import _assert_safe_release
    s=setup
    child=child_material(s)
    owner=uid()
    admission(s,owner)
    with s['stores']['source'].transaction() as c:
        c.execute("UPDATE inventory_reservation SET status=? WHERE workflow_id=?",(reservation_state,owner))
        c.execute("UPDATE material SET parent_uuid=NULL,inventory_status='handoff_quarantined' WHERE uuid=?",(child,))
        with pytest.raises(ValueError, match='物料仍持于设备|位置待核实'):
            _assert_safe_release(c,s['auth']['source'],uid(),owner)


def test_inventory_release_guard_retains_known_and_soft_deleted_task(setup,tmp_path):
    from tests.workflow.test_f05_task_scheduler_bridge import _seed_task
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge
    s=setup
    store=WorkflowStore(tmp_path/'release-guard-workflow.db')
    inventory=InventoryService(s['stores']['source'])
    scheduler=EdgeScheduler(dispatcher=RecordingDispatcher(),inventory=inventory)
    bridge=TaskSchedulerBridge(store,scheduler=scheduler)
    try:
        task=_seed_task(store,with_material=False)
        admission(s,task['uuid'])
        assert inventory.release_workflow(task['uuid'])['status']=='held'
        with store.transaction() as c:
            c.execute("UPDATE workflow_task SET deleted_at='2026-10-30T00:00:00Z' WHERE uuid=?",(task['uuid'],))
        assert inventory.release_workflow(task['uuid'])['status']=='held'
        assert s['stores']['source'].query_one("SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND status='active'",(task['uuid'],))
    finally:
        bridge.close()
        store.close()
