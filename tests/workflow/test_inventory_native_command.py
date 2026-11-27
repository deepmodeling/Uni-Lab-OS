"""原生库存节点经真实创作、计划、调度和持久桥执行，不派发虚拟设备。"""
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import prepare_task_input
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

from .test_authoring_engine import WORKFLOW_UUID, _compile, _handle, _template

DEVICE = "60000000-0000-4000-8000-000000000001"
MEASURE = "20000000-0000-4000-8000-000000000001"
READ = "20000000-0000-4000-8000-000000000002"
COUNT = "20000000-0000-4000-8000-000000000003"
TEMPLATE = "30000000-0000-4000-8000-000000000001"
SOURCE = f'''from typing import TypedDict
from lab.devices import Reactor
from unilabos.workflow.authoring import device, service, workflow, workflow_output
from unilabos.workflow.native_commands import InventoryCommands

probe: Reactor = device("{DEVICE}")
stock: InventoryCommands = service()

class Result(TypedDict):
    changed: bool

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="板仓盘点")
def stocktake() -> Result:
    # unilab:node_uuid={READ}
    before = stock.read_count(lot_id="plates", mount_uuid="magazine")
    # unilab:node_uuid={MEASURE}
    measured = probe.measure()
    # unilab:node_uuid={COUNT}
    checked = stock.reconcile_count(lot_id="plates", mount_uuid="magazine", measured_count=measured.count, expected_version=before.version)
    return workflow_output(changed=checked.changed)
'''


def engine():
    projection = RegistryTemplateProjection.in_memory(
        authority_id="test", resource_template_identity_resolver=lambda key: str(uuid5(NAMESPACE_URL, key))
    )
    catalog = projection.refresh(RegistryTemplateSnapshot(
        fingerprint="test", resource_templates=(), device_templates=({
            "id": "host_node", "class": {"module": "lab:Host"},
        },),
    ))
    probe, handles = _template(TEMPLATE, name="measure", handles=[
        _handle(str(uuid5(NAMESPACE_URL, "probe.count")), node_template_uuid=TEMPLATE, key="count", io_type="source", value_type="integer"),
        *[_handle(str(uuid5(NAMESPACE_URL, "probe.ready." + direction)), node_template_uuid=TEMPLATE, key="ready", io_type=direction, value_type="any", data_source="dependency") for direction in ("source", "target")],
    ])
    probe["node_type"] = "ILab"
    probe["schema"] = {"type": "object", "properties": {}}
    probe["meta_data"]["unilab"] = {"action_contract_schema": {"type": "object", "properties": {"goal": probe["schema"]}, "required": ["goal"]}}
    return WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
        [*[action.detached_template() for action in catalog.actions], probe],
        [*[handle for action in catalog.actions for handle in action.detached_handles()], *handles],
    ))


def test_service_compiles_without_device_binding_and_roundtrips():
    compiler = engine()
    result = _compile(compiler, SOURCE)
    assert result.valid, result.diagnostics
    nodes = {n["uuid"]: n for n in result.graph["nodes"]}
    for identity in (READ, COUNT):
        assert nodes[identity]["type"] == "workflow_command"
        assert not nodes[identity].get("material_uuid")
    repeated = _compile(compiler, result.normalized_python_source, graph=result.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == result.graph
    plan, jobs = ExecutionPlanBuilder().build(result.graph, run_mode="normal", target_node_uuid=None)
    spec = WorkflowSpecCompiler().compile({"uuid": str(uuid4()), "execution_plan": plan, "input": {}}, jobs)
    assert [n.device_id for n in spec.nodes if n.executor_kind == "workflow_command"] == ["", ""]


@pytest.mark.parametrize("scenario", ["normal", "reserved", "stale_version", "literal", "crash_same_process", "crash_new_process"])
def test_service_runtime_gates_inventory_and_never_dispatches_a_device(tmp_path, scenario):
    source = SOURCE.replace("measured_count=measured.count", "measured_count=8") if scenario == "literal" else SOURCE
    authored = _compile(engine(), source)
    assert authored.valid, authored.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(authored.graph, run_mode="normal", target_node_uuid=None)
    store = WorkflowStore(tmp_path / "workflow.db")
    inventory = InventoryService(InventoryStore(tmp_path / "inventory.db"))
    with inventory.store.transaction() as conn:
        conn.execute("INSERT INTO material(uuid,create_time,update_time,resource_template_uuid,class,type,barcode,name) VALUES (?, '2026-09-13T00:00:00Z', '2026-09-13T00:00:00Z', ?, 'device', 'device', '', '测量仪')", (DEVICE, TEMPLATE))
    inventory.register_instance(template_id="container", edge_uuid="magazine")
    inventory.inbound_lot("plate", 10, unit="piece", lot_id="plates", warehouse_zone_id="magazine")
    if scenario == "reserved":
        inventory.reserve_workflow("other", {"n": [MaterialRequirement(lot_id="plates", quantity=1)]})
    store.create_workflow(workflow_uuid=WORKFLOW_UUID, name="盘点", tags=[], description=None, meta_data={})
    task_uuid = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID, task_uuid=task_uuid, run_mode="normal", target_node_uuid=None,
        description=None, meta_data={}, plan_builder=lambda _: prepare_task_input(
            graph=authored.graph, execution_plan=plan, jobs=jobs, raw_input={},
        ),
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(task_uuid))
        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["action"] == "measure"
        if scenario == "stale_version":
            inventory.reserve_workflow("intervening", {"n": [MaterialRequirement(lot_id="plates", quantity=1)]})
        if scenario.startswith("crash"):
            original = bridge._projection.project_local_control_evaluation
            def crash_projection(**kwargs):
                if "measured_count" in (kwargs.get("local_command_result") or {}):
                    raise RuntimeError("模拟库存已提交但结果未投影时进程中断")
                return original(**kwargs)
            bridge._projection.project_local_control_evaluation = crash_projection
            with pytest.raises(RuntimeError, match="模拟库存"):
                scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {"count": 8})
            assert inventory.store.get_lot("plates")["quantity_total"] == 8
            bridge.close()
            scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
            bridge = TaskSchedulerBridge(store, scheduler=scheduler)
            bridge.recover_active_tasks(execution_process_restarted=scenario == "crash_new_process")
        else:
            scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {"count": 8})
        assert len(dispatcher.dispatched) == 1
        task = store.get_task(task_uuid)
        settled = next(j for j in store.list_jobs(task_uuid) if j["workflow_node_uuid"] == COUNT)
        if scenario == "normal" or scenario.startswith("crash"):
            assert task["status"] == "succeeded", (task, store.list_jobs(task_uuid))
            assert task["output"]["changed"] is True
            assert settled["status"] == "succeeded"
            assert inventory.store.get_lot("plates")["quantity_total"] == 8
            scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {"count": 8})
            assert inventory.store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger WHERE op_type='lot.measurement_reconciled'")["n"] == 1
        else:
            assert task["status"] != "succeeded", task
            assert settled["status"] != "succeeded", settled
            assert inventory.store.get_lot("plates")["quantity_total"] == 10
    finally:
        bridge.close()
        inventory.store.close()
        store.close()
