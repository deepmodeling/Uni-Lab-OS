"""上游 Material 作为运行时 material_source mount 的集成合同。"""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from unilabos.app.scheduler.material_source_resolution import (
    MaterialSourceResolutionCoordinator,
    MaterialSourceResolutionError,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _handle
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    MATERIAL_SOURCE_TEMPLATE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_TEMPLATE_UUID,
)
from .test_f05_material_source_authoring_roundtrip import _goal_material_engine
from .test_material_io_contract import (
    LOAD_UUID,
    SITE_UUID,
    _material_io_catalog,
)
from .test_material_io_contract import (
    MATERIAL_SOURCE as IO_MATERIAL_SOURCE,
)
from .test_runtime_material_replenishment import BACKUP, REFILL
from .test_structured_condition_authoring import CONDITION_NODE_UUID, _condition_template

MOUNT_HANDLE_UUID = "40000000-0000-4000-8000-000000000019"
SUFFICIENT_HANDLE_UUID = "40000000-0000-4000-8000-000000000020"
READY_TARGET_UUID = "40000000-0000-4000-8000-000000000021"
READY_SOURCE_UUID = "40000000-0000-4000-8000-000000000022"
DIRECT_ACTION_UUID = "20000000-0000-4000-8000-000000000011"
CHILD_SOURCE_UUID = "20000000-0000-4000-8000-000000000014"
ACTUAL_MOUNT_UUID = "50000000-0000-4000-8000-000000000099"
SELECTED_MATERIAL_UUID = "50000000-0000-4000-8000-000000000098"


def _engine() -> WorkflowAuthoringEngine:
    base = _goal_material_engine()._catalog
    material_io = _material_io_catalog()
    templates = [action.detached_template() for action in base.actions]
    templates.extend(action.detached_template() for action in material_io.actions)
    templates.append(_condition_template())
    handles = [
        handle
        for catalog in (base, material_io)
        for action in catalog.actions
        for handle in action.detached_handles()
    ]
    handles.extend(
        [
            _handle(
                MOUNT_HANDLE_UUID,
                node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
                key="mount",
                io_type="target",
                value_type="ResourceSlot",
                required=False,
                data_source="goal",
            ),
            _handle(
                SUFFICIENT_HANDLE_UUID,
                node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
                key="sufficient",
                io_type="source",
                value_type="boolean",
            ),
            _handle(
                READY_TARGET_UUID,
                node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
            _handle(
                READY_SOURCE_UUID,
                node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ]
    )
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            templates,
            handles,
            resource_template_symbols={
                "lab.resources:plate_96": PLATE_TEMPLATE_UUID,
            },
        )
    )


def _source(*, dynamic_mount: bool = True) -> str:
    backup_mount = "loaded.material" if dynamic_mount else (
        f'resource_ref("{MOUNT_MATERIAL_UUID}")'
    )
    return f'''from lab.devices import Reactor
from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialCustodyPolicy, MaterialFlowRole, device, material_io,
    material_source, resource_ref, workflow, workflow_output,
)

reactor: Reactor = device()

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Dynamic mount")
def dynamic_mount_workflow():
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load", resource_template=plate_96,
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"), site="{SITE_UUID}",
        material=None, disposition=None, scope=None, timeout_seconds=45,
    )
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    plate = material_source(
        resource_template=plate_96, mode="existing",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"), material_uuid=None,
        site=None, slot_range=None, flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
        custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        supports_replenishment=True,
    )
    # unilab:node_uuid={CONDITION_NODE_UUID}
    if plate.sufficient:
        # unilab:node_uuid={DIRECT_ACTION_UUID}
        direct = reactor.process_plate(plate=plate)
    else:
        # unilab:node_uuid={BACKUP}
        backup = material_source(
            resource_template=plate_96, mode="existing",
            mount={backup_mount}, material_uuid=None, site=None, slot_range=None,
            flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
            custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        )
        # unilab:node_uuid={REFILL}
        refilled = reactor.process_plate(plate=backup)
    return workflow_output()
'''


def _compiled(*, dynamic_mount: bool = True, engine=None):
    return (engine or _engine()).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source(dynamic_mount=dynamic_mount),
        source_uri="memory://dynamic_mount.py",
        applied_graph=_applied_graph(),
    )


def test_dynamic_mount_authoring_graph_source_and_plan_round_trip() -> None:
    engine = _engine()
    compiled = _compiled(engine=engine)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    backup = next(node for node in compiled.graph["nodes"] if node["uuid"] == BACKUP)
    assert backup["param"]["mount"] is None
    mount_edge = next(
        edge
        for edge in compiled.graph["edges"]
        if edge["target_node_uuid"] == BACKUP
        and edge.get("target_handle_uuid") == MOUNT_HANDLE_UUID
    )
    assert mount_edge["source_node_uuid"] == LOAD_UUID
    assert mount_edge["source_handle_uuid"] == IO_MATERIAL_SOURCE
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="memory://dynamic_mount.py",
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics

    plan, _ = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    planned = next(node for node in plan["nodes"] if node["uuid"] == BACKUP)
    assert "prebind_child_for_locks" not in planned
    assert planned["runtime_material_source"] is True
    assert planned["runtime_mount_binding"] == {
        "source_node_uuid": LOAD_UUID,
        "source_data_key": "material",
    }


def test_dynamic_mount_is_rejected_from_task_preadmission() -> None:
    compiled = _compiled()
    graph = deepcopy(compiled.graph)
    graph["nodes"] = [
        node
        for node in graph["nodes"]
        if node["uuid"] in {LOAD_UUID, BACKUP}
    ]
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if edge["source_node_uuid"] in {LOAD_UUID, BACKUP}
        and edge["target_node_uuid"] in {LOAD_UUID, BACKUP}
    ]
    next(node for node in graph["nodes"] if node["uuid"] == BACKUP)[
        "parent_uuid"
    ] = None
    with pytest.raises(
        ExecutionPlanBuildError,
        match="动态 mount",
    ):
        ExecutionPlanBuilder().build(
            graph, run_mode="normal", target_node_uuid=None
        )


def test_runtime_coordinator_uses_actual_mount_in_single_admission() -> None:
    compiled = _compiled()
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    for job in jobs:
        if job["workflow_node_uuid"] == LOAD_UUID:
            job["return_info"] = {"material": {"uuid": ACTUAL_MOUNT_UUID}}

    class Inventory:
        calls = []

        def admit_task_materials(self, task_uuid, requests, quantities, **options):
            self.calls.append((task_uuid, requests, quantities))
            assert requests[0].requirement.mount_uuid == ACTUAL_MOUNT_UUID
            return {
                "allocations": {BACKUP: [SELECTED_MATERIAL_UUID]},
                "allocation_sites": {BACKUP: {}},
            }

    class Projection:
        binding = None

        def validate_material_source_context(self, task_uuid, plan):
            assert "center_material_source_context" not in plan

        def project_material_source_admission(self, task_uuid, bindings, **options):
            self.binding = bindings

    inventory = Inventory()
    projection = Projection()
    result = MaterialSourceResolutionCoordinator(
        inventory=inventory,
        projection=projection,
    ).reconcile(
        {"uuid": str(uuid4()), "execution_plan": plan},
        jobs,
        runtime_node_uuid=BACKUP,
    )
    assert result.status == "admitted"
    assert len(inventory.calls) == 1
    assert projection.binding[BACKUP]["uuid"] == SELECTED_MATERIAL_UUID


def test_runtime_coordinator_reads_device_action_return_value_envelope() -> None:
    compiled = _compiled()
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    for job in jobs:
        if job["workflow_node_uuid"] == LOAD_UUID:
            job["return_info"] = {
                "error": "",
                "result": {
                    "return_info": json.dumps(
                        {
                            "return_value": {
                                "material": {"uuid": ACTUAL_MOUNT_UUID},
                                "present": True,
                            }
                        }
                    ),
                    "success": True,
                },
                "return_value": {
                    "material": {"uuid": ACTUAL_MOUNT_UUID},
                    "present": True,
                },
                "suc": True,
                "suc_type": "normal",
            }

    class Inventory:
        calls = []

        def admit_task_materials(self, task_uuid, requests, quantities, **options):
            self.calls.append((task_uuid, requests, quantities))
            assert requests[0].requirement.mount_uuid == ACTUAL_MOUNT_UUID
            return {
                "allocations": {BACKUP: [SELECTED_MATERIAL_UUID]},
                "allocation_sites": {BACKUP: {}},
            }

    class Projection:
        binding = None

        def validate_material_source_context(self, task_uuid, plan):
            assert "center_material_source_context" not in plan

        def project_material_source_admission(self, task_uuid, bindings, **options):
            self.binding = bindings

    result = MaterialSourceResolutionCoordinator(
        inventory=Inventory(),
        projection=Projection(),
    ).reconcile(
        {"uuid": str(uuid4()), "execution_plan": plan},
        jobs,
        runtime_node_uuid=BACKUP,
    )
    assert result.status == "admitted"


def test_static_mount_behavior_does_not_gain_runtime_binding() -> None:
    compiled = _compiled(dynamic_mount=False)
    assert compiled.valid, compiled.diagnostics
    plan, _ = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    backup = next(node for node in plan["nodes"] if node["uuid"] == BACKUP)
    assert backup["param"]["mount"] == {"uuid": MOUNT_MATERIAL_UUID}
    assert "runtime_mount_binding" not in backup


def _nested_lookup_source() -> str:
    return f'''from lab.devices import Reactor
from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialCustodyPolicy, MaterialFlowRole, device, material_source,
    resource_ref, workflow, workflow_output,
)

reactor: Reactor = device()

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Nested lookup")
def nested_lookup_workflow():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    box = material_source(
        resource_template=plate_96, mode="existing",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"), material_uuid=None,
        site=None, slot_range=None, flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
        custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        supports_replenishment=True,
    )
    # unilab:node_uuid={CONDITION_NODE_UUID}
    if box.sufficient:
        # unilab:node_uuid={CHILD_SOURCE_UUID}
        tip = material_source(
            resource_template=plate_96, mode="existing",
            mount=box.material, material_uuid=None, site=None, slot_range=None,
            flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
            custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        )
        # unilab:node_uuid={DIRECT_ACTION_UUID}
        used = reactor.process_plate(plate=tip)
    else:
        # unilab:node_uuid={BACKUP}
        fallback = material_source(
            resource_template=plate_96, mode="existing",
            mount=resource_ref("{MOUNT_MATERIAL_UUID}"), material_uuid=None,
            site=None, slot_range=None, flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
            custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        )
        # unilab:node_uuid={REFILL}
        missing = reactor.process_plate(plate=fallback)
    return workflow_output()
'''


def test_parent_source_mounts_child_source_via_runtime_binding() -> None:
    compiled = _engine().compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_nested_lookup_source(),
        source_uri="memory://nested_lookup.py",
        applied_graph=_applied_graph(),
    )
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    parent = next(node for node in plan["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID)
    child = next(node for node in plan["nodes"] if node["uuid"] == CHILD_SOURCE_UUID)
    assert "prebind_child_for_locks" not in child
    assert parent.get("material_binding_targets", []) == []
    assert child["runtime_material_source"] is True
    assert child["runtime_mount_binding"] == {
        "source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
        "source_data_key": "material",
    }

    for job in jobs:
        if job["workflow_node_uuid"] == MATERIAL_SOURCE_NODE_UUID:
            job["return_info"] = {
                "material": {"uuid": ACTUAL_MOUNT_UUID},
                "sufficient": True,
            }

    class Inventory:
        calls = []

        def admit_task_materials(self, task_uuid, requests, quantities, **options):
            self.calls.append((task_uuid, requests, quantities))
            assert requests[0].requirement.mount_uuid == ACTUAL_MOUNT_UUID
            return {
                "allocations": {CHILD_SOURCE_UUID: [SELECTED_MATERIAL_UUID]},
                "allocation_sites": {CHILD_SOURCE_UUID: {}},
            }

    class Projection:
        binding = None

        def validate_material_source_context(self, task_uuid, plan):
            assert "center_material_source_context" not in plan

        def project_material_source_admission(self, task_uuid, bindings, **options):
            self.binding = bindings

    inventory = Inventory()
    projection = Projection()
    result = MaterialSourceResolutionCoordinator(
        inventory=inventory,
        projection=projection,
    ).reconcile(
        {"uuid": str(uuid4()), "execution_plan": plan},
        jobs,
        runtime_node_uuid=CHILD_SOURCE_UUID,
    )
    assert result.status == "admitted"
    assert projection.binding[CHILD_SOURCE_UUID]["uuid"] == SELECTED_MATERIAL_UUID


def test_parent_box_source_does_not_claim_child_tip_consumers() -> None:
    """范围选盒后再选枪头时，父级来源不能穿过子级来源争写同一动作参数。"""

    active = {
        "box_source": {},
        "tip_source": {},
        "consume": {},
    }
    kinds = {
        "box_source": "material_source",
        "tip_source": "material_source",
        "consume": "device_action",
    }
    outgoing = {
        "box_source": [("tip_source", "mount")],
        "tip_source": [("consume", "resource"), ("consume", "target_box")],
    }
    order = {"box_source": 0, "tip_source": 1, "consume": 2}
    box_consumers = ExecutionPlanBuilder._device_consumer_bindings(
        "box_source",
        active=active,
        kinds=kinds,
        outgoing=outgoing,
        order=order,
    )
    tip_consumers = ExecutionPlanBuilder._device_consumer_bindings(
        "tip_source",
        active=active,
        kinds=kinds,
        outgoing=outgoing,
        order=order,
    )
    assert box_consumers == []
    assert tip_consumers == [("consume", "resource"), ("consume", "target_box")]


def _required_existing_child_plan():
    """编译不带补料分支的父子来源，供准入行为测试共用。"""
    import textwrap

    source = _nested_lookup_source()
    prefix, rest = source.split(f'    # unilab:node_uuid={CONDITION_NODE_UUID}\n', 1)
    child = rest.split('    if box.sufficient:\n', 1)[1].split('    else:\n', 1)[0]
    source = prefix.replace('supports_replenishment=True', 'supports_replenishment=False')
    source += textwrap.indent(textwrap.dedent(child), '    ') + '    return workflow_output()\n'
    compiled = _engine().compile(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        python_source=source, source_uri='memory://required_child.py', applied_graph=_applied_graph(),
    )
    assert compiled.valid, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    return plan, jobs


def test_required_existing_child_uses_actual_parent_receipt_without_replenishment():
    plan, _jobs = _required_existing_child_plan()
    parent = next(n for n in plan['nodes'] if n['uuid'] == MATERIAL_SOURCE_NODE_UUID)
    child = next(n for n in plan['nodes'] if n['uuid'] == CHILD_SOURCE_UUID)
    assert not parent.get('runtime_material_source')
    assert child['runtime_material_source'] is True
    assert not child['param'].get('supports_replenishment')
    assert child['runtime_mount_binding'] == {
        'source_node_uuid': MATERIAL_SOURCE_NODE_UUID, 'source_data_key': 'material',
    }
    assert child['material_requirements'][0]['template_id'] == PLATE_TEMPLATE_UUID
    assert child['material_requirements'][0]['mount_uuid'] == ''



class _ChildAdmissionProjection:
    def __init__(self, jobs):
        self.jobs = jobs
        self.waiting = set()

    def validate_material_source_context(self, task_uuid, plan):
        pass

    def project_material_source_admission(self, task_uuid, bindings, **options):
        assert options["runtime"] is True
        for job in self.jobs:
            if job["workflow_node_uuid"] in bindings:
                job.update(status="succeeded", return_info={"material": bindings[job["workflow_node_uuid"]]})

    def project_material_source_blocked(self, task_uuid, *, reason, source_node_ids):
        assert reason
        self.waiting.update(source_node_ids)


@pytest.mark.parametrize("guard", [
    "none", "condition", "condition_group", "conditional_parent", "replenishment",
    "pending_parent", "old_parent_attempt", "pending_dependency", "deferred", "repeat",
])
def test_required_child_admission_respects_dependencies_and_control_regions(guard):
    """只有实际前置责任完成的必需来源可在锁规划前准入。"""
    plan, jobs = _required_existing_child_plan()
    child = next(node for node in plan["nodes"] if node["uuid"] == CHILD_SOURCE_UUID)
    parent = next(job for job in jobs if job["workflow_node_uuid"] == MATERIAL_SOURCE_NODE_UUID)
    parent.update(status="succeeded", return_info={"material": {"uuid": ACTUAL_MOUNT_UUID}})
    for job in jobs:
        job.setdefault("status", "pending")
    if guard.startswith("condition"):
        member = MATERIAL_SOURCE_NODE_UUID if guard == "conditional_parent" else CHILD_SOURCE_UUID
        if guard == "condition_group":
            member = str(uuid4())
            child["parent_uuid"] = member
            plan["nodes"].append({"uuid": member, "kind": "group"})
        plan["nodes"].append({"uuid": CONDITION_NODE_UUID, "kind": "condition",
                              "control_region": {"branches": [{"node_uuids": [member]}]}})
    elif guard == "replenishment":
        child["param"]["supports_replenishment"] = True
    elif guard == "pending_parent":
        parent["status"] = "pending"
    elif guard == "old_parent_attempt":
        parent["current_attempt"] = False
    elif guard == "pending_dependency":
        dependency = str(uuid4())
        plan["edges"].append({"source_node_uuid": dependency, "target_node_uuid": CHILD_SOURCE_UUID,
                              "dependency_only": True})
        jobs.append({"workflow_node_uuid": dependency, "status": "pending"})
    elif guard == "deferred":
        child.setdefault("meta_data", {}).setdefault("unilab", {})["operation_preparation"] = {"deferred": True}
    elif guard == "repeat":
        loop = str(uuid4())
        child["parent_uuid"] = loop
        plan["nodes"].append({"uuid": loop, "kind": "repeat_until"})
    calls = []

    class Inventory:
        def admit_task_materials(self, task_uuid, requests, quantities, **options):
            request, = requests
            assert request.requirement.mount_uuid == ACTUAL_MOUNT_UUID
            calls.append(request.node_id)
            return {"allocations": {CHILD_SOURCE_UUID: [SELECTED_MATERIAL_UUID]}}

    coordinator = MaterialSourceResolutionCoordinator(inventory=Inventory(), projection=_ChildAdmissionProjection(jobs))
    task = {"uuid": str(uuid4()), "execution_plan": plan}
    original_jobs = deepcopy(jobs)
    assert coordinator.reconcile_required_children(task, read_jobs=lambda: jobs).status == "admitted"
    if guard == "none":
        assert calls == [CHILD_SOURCE_UUID]
        child_job = next(job for job in jobs if job["workflow_node_uuid"] == CHILD_SOURCE_UUID)
        assert child_job["status"] == "succeeded"
        assert child_job["return_info"]["material"]["uuid"] == SELECTED_MATERIAL_UUID
    else:
        assert not calls
        assert jobs == original_jobs
    coordinator.reconcile_required_children(task, read_jobs=lambda: jobs)
    assert len(calls) == (1 if guard == "none" else 0)
    if guard == "pending_dependency":
        jobs[-1]["status"] = "succeeded"
        coordinator.reconcile_required_children(task, read_jobs=lambda: jobs)
        assert calls == [CHILD_SOURCE_UUID]


def test_required_children_resolve_multiple_levels_and_wait_for_other_tasks():
    """逐层读取真实父回执；其他任务占用时保留待准入状态，释放后继续。"""
    from unilabos.app.scheduler.inventory.domain import MaterialClaimedByOtherTask

    plan, jobs = _required_existing_child_plan()
    parent = next(job for job in jobs if job["workflow_node_uuid"] == MATERIAL_SOURCE_NODE_UUID)
    parent.update(status="succeeded", return_info={"material": {"uuid": ACTUAL_MOUNT_UUID}})
    child = next(node for node in plan["nodes"] if node["uuid"] == CHILD_SOURCE_UUID)
    child_job = next(job for job in jobs if job["workflow_node_uuid"] == CHILD_SOURCE_UUID)
    child_job["status"] = "pending"
    grandchild_id = str(uuid4())
    grandchild = deepcopy(child)
    grandchild.update(uuid=grandchild_id, runtime_mount_binding={
        "source_node_uuid": CHILD_SOURCE_UUID, "source_data_key": "material",
    })
    plan["nodes"].insert(0, grandchild)
    plan["edges"].append({"source_node_uuid": CHILD_SOURCE_UUID, "target_node_uuid": grandchild_id})
    jobs.append(dict(child_job, uuid=str(uuid4()), workflow_node_uuid=grandchild_id))
    calls = []

    class Inventory:
        available = False

        def admit_task_materials(self, task_uuid, requests, quantities, **options):
            request, = requests
            if not self.available:
                raise MaterialClaimedByOtherTask("其他任务正在使用子物料")
            calls.append((request.node_id, request.requirement.mount_uuid))
            return {"allocations": {request.node_id: [SELECTED_MATERIAL_UUID]}}

    inventory = Inventory()
    projection = _ChildAdmissionProjection(jobs)
    coordinator = MaterialSourceResolutionCoordinator(inventory=inventory, projection=projection)
    task = {"uuid": str(uuid4()), "execution_plan": plan}
    assert coordinator.reconcile_required_children(task, read_jobs=lambda: jobs).status == "blocked"
    assert projection.waiting == {CHILD_SOURCE_UUID}
    assert child_job["status"] == "pending"
    inventory.available = True
    assert coordinator.reconcile_required_children(task, read_jobs=lambda: jobs).status == "admitted"
    assert calls == [(CHILD_SOURCE_UUID, ACTUAL_MOUNT_UUID), (grandchild_id, SELECTED_MATERIAL_UUID)]
    coordinator.reconcile_required_children(task, read_jobs=lambda: jobs)
    assert len(calls) == 2
