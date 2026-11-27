"""material_io 创作、冻结计划与规格编译合同。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_graph import _output_resource_template_symbol
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.authoring_material import MaterialSourceDeclaration
from unilabos.workflow.authoring_material_io import MaterialIOBinding, MaterialIODeclaration
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.resource_lock_plan import (
    RESOURCE_PLAN_CAPABILITY,
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _handle

LOAD_UUID = "21000000-0000-4000-8000-000000000001"
UNLOAD_UUID = "21000000-0000-4000-8000-000000000002"
REFILL_UUID = "21000000-0000-4000-8000-000000000003"
TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"
LOAD_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000001"
MOUNT_UUID = "51000000-0000-4000-8000-000000000001"
SITE_UUID = "51000000-0000-4000-8000-000000000002"
FIXED_MATERIAL_UUID = "51000000-0000-4000-8000-000000000003"
MATERIAL_TARGET = "41000000-0000-4000-8000-000000000001"
MATERIAL_SOURCE = "41000000-0000-4000-8000-000000000002"
READY_TARGET = "41000000-0000-4000-8000-000000000003"
READY_SOURCE = "41000000-0000-4000-8000-000000000004"


def _material_io_catalog() -> AuthoringCatalogSnapshot:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "resource_template_uuid": {"type": ["string", "null"]},
            "mount": {"type": "object"},
            "site": {"type": ["string", "null"]},
            "material": {"type": ["object", "null"]},
            "disposition": {"type": ["string", "null"]},
            "scope": {"type": ["string", "null"]},
            "timeout_seconds": {"type": "integer"},
        },
    }
    template = {
        "uuid": TEMPLATE_UUID,
        "resource_template_uuid": "30000000-0000-4000-8000-000000000099",
        "name": "material_io",
        "display_name": "Material IO",
        "class": "unilabos.workflow.authoring:material_io",
        "description": "经人工确认装卸物料。",
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": schema,
        "type": "material_io",
        "node_type": "material_io",
        "meta_data": {
            "unilab": {
                "executor_kind": "material_io",
                "builtin_manual_confirmation": True,
            }
        },
    }
    handles = [
        _handle(
            MATERIAL_TARGET,
            node_template_uuid=TEMPLATE_UUID,
            key="material",
            io_type="target",
            value_type="ResourceSlot",
            required=False,
            data_source="goal",
        ),
        _handle(
            MATERIAL_SOURCE,
            node_template_uuid=TEMPLATE_UUID,
            key="material",
            io_type="source",
            value_type="ResourceSlot",
        ),
        _handle(
            READY_TARGET,
            node_template_uuid=TEMPLATE_UUID,
            key="ready",
            io_type="target",
            value_type="any",
            data_source="dependency",
        ),
        _handle(
            READY_SOURCE,
            node_template_uuid=TEMPLATE_UUID,
            key="ready",
            io_type="source",
            value_type="any",
            data_source="dependency",
        ),
    ]
    return AuthoringCatalogSnapshot.from_entities(
        [template],
        handles,
        resource_template_symbols={"lab.resources:plate_96": LOAD_TEMPLATE_UUID},
    )


def _source(*, site: str = repr(SITE_UUID), timeout: int = 45) -> str:
    return f'''from lab.resources import plate_96
from unilabos.workflow.authoring import material_io, resource_ref, workflow, workflow_output


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Material IO")
def material_io_workflow(*, target_site: str = "{SITE_UUID}"):
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load",
        resource_template=plate_96,
        mount=resource_ref("{MOUNT_UUID}"),
        site={site},
        material=None,
        disposition=None,
        scope=None,
        timeout_seconds={timeout},
    )
    # unilab:node_uuid={UNLOAD_UUID}
    unloaded = material_io(
        mode="unload",
        resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"),
        site=target_site,
        material=loaded,
        disposition="detach",
        scope="material_with_contents",
        timeout_seconds={timeout},
    )
    return workflow_output()
'''


def _compile(source: str):
    return WorkflowAuthoringEngine(catalog=_material_io_catalog()).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="memory://material_io.py",
        applied_graph=_applied_graph(),
    )


def _production_material_io_engine() -> WorkflowAuthoringEngine:
    template, handles = RegistryTemplateProjection._compile_material_io(
        resource_template_uuid="30000000-0000-4000-8000-000000000099",
        resource_name="host_node",
        resource_display_name="Host Node",
    )
    # 持久模板目录通过后端读取模型发布 JSON 文本 Schema。
    template["schema"] = json.dumps(
        template["schema"],
        ensure_ascii=False,
        sort_keys=True,
    )
    catalog = AuthoringCatalogSnapshot.from_entities(
        [template],
        handles,
        resource_template_symbols={"lab.resources:plate_96": LOAD_TEMPLATE_UUID},
    )
    return WorkflowAuthoringEngine(catalog=catalog)


def _compile_with_production_template(source: str, *, applied_graph=None):
    return _production_material_io_engine().compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="memory://material_io_production.py",
        applied_graph=applied_graph or _applied_graph(),
    )


def test_material_io_authoring_round_trip() -> None:
    compiled = _compile(_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    load, unload = compiled.graph["nodes"]
    assert load["param"] == {
        "mode": "load",
        "resource_template_uuid": LOAD_TEMPLATE_UUID,
        "mount": {"uuid": MOUNT_UUID},
        "site": SITE_UUID,
        "material": None,
        "disposition": None,
        "scope": None,
        "timeout_seconds": 45,
    }
    assert unload["param"]["mode"] == "unload"
    assert unload["param"]["site"] is None
    assert unload["manual_confirmation"] == {"timeout_seconds": 45}
    assert len(compiled.graph["edges"]) == 1
    assert "material=loaded" in compiled.normalized_python_source

    repeated = _compile(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


def test_load_output_template_guarantee_cannot_outlive_its_frozen_load_parameters() -> None:
    from copy import deepcopy

    from unilabos.workflow.workflow_io import WorkflowIOValidationError, validate_workflow_graph_io

    compiled = _compile_with_production_template(_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    node = next(n for n in graph["nodes"] if n["param"].get("mode") == "load")
    node["param"]["resource_template_uuid"] = FIXED_MATERIAL_UUID
    with pytest.raises(WorkflowIOValidationError, match="装载输出模板保证"):
        validate_workflow_graph_io(graph)


def test_scalar_io_binds_operation_material_and_site_inputs_and_round_trips() -> None:
    source = f'''from typing import TypedDict
from lab.resources import plate_96
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import material_io, resource_ref, workflow

class Result(TypedDict):
    plate: ResourceSlot

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Manual handoff", workflow_type="experiment_operation")
def handoff(*, sample: ResourceSlot, destination: str) -> Result:
    # unilab:node_uuid={UNLOAD_UUID}
    removed = material_io(mode="unload", resource_template=None, mount=resource_ref("{MOUNT_UUID}"), site="A1", material=sample, disposition="detach", scope="material_with_contents", timeout_seconds=45)
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(mode="load", resource_template=plate_96, mount=resource_ref("{MOUNT_UUID}"), site=destination, material=None, disposition=None, scope=None, timeout_seconds=45)
    return {{"plate": loaded}}
'''
    compiled = _compile_with_production_template(source)
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    bindings = {
        value["parameter"]
        for node in compiled.graph["nodes"]
        for value in node["meta_data"]["unilab"]["input_bindings"].values()
    }
    assert bindings == {"sample", "destination"}
    output = compiled.graph["workflow"]["meta_data"]["unilab"]["output_contract"]["outputs"][0]
    assert output["schema"]["allowed_resource_template_uuids"] == [LOAD_TEMPLATE_UUID]
    assert "material=sample" in compiled.normalized_python_source
    assert "site=destination" in compiled.normalized_python_source
    repeated = _compile_with_production_template(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


def test_load_site_name_and_optional_site_uuid_round_trip() -> None:
    source = _source().replace(
        f"site={SITE_UUID!r},",
        'site="T12",\n        site_uuid=None,',
        1,
    )
    compiled = _compile_with_production_template(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    load = compiled.graph["nodes"][0]
    assert load["param"]["site"] == "T12"
    assert load["param"]["site_uuid"] is None
    assert "site='T12'" in compiled.normalized_python_source
    assert "site_uuid=None" in compiled.normalized_python_source
    repeated = _compile_with_production_template(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph["nodes"][0]["param"]["site"] == "T12"

    named = source.replace("site_uuid=None", f'site_uuid="{SITE_UUID}"', 1)
    compiled_named = _compile_with_production_template(named)
    assert compiled_named.valid and compiled_named.graph is not None, compiled_named.diagnostics
    assert compiled_named.graph["nodes"][0]["param"]["site_uuid"] == SITE_UUID


def test_unload_and_refill_site_is_name_with_optional_uuid() -> None:
    source = f'''from lab.resources import plate_96
from unilabos.workflow.authoring import material_io, resource_ref, workflow, workflow_output


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Material IO sites")
def material_io_workflow():
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load",
        resource_template=plate_96,
        mount=resource_ref("{MOUNT_UUID}"),
        site="BEAKER1",
        material=None,
        disposition=None,
        scope=None,
        timeout_seconds=45,
    )
    # unilab:node_uuid={UNLOAD_UUID}
    unloaded = material_io(
        mode="unload",
        resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"),
        site="BEAKER1",
        site_uuid="{SITE_UUID}",
        material=loaded,
        disposition="detach",
        scope="material_only",
        timeout_seconds=45,
    )
    # unilab:node_uuid={REFILL_UUID}
    refilled = material_io(
        mode="refill",
        resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"),
        site="BEAKER1",
        site_uuid="{SITE_UUID}",
        material=loaded,
        disposition=None,
        scope=None,
        timeout_seconds=45,
        reagent={{"quantity": 1, "quantity_unit": "mL"}},
    )
    return workflow_output()
'''
    compiled = _compile_with_production_template(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    unload = next(node for node in compiled.graph["nodes"] if node["param"]["mode"] == "unload")
    refill = next(node for node in compiled.graph["nodes"] if node["param"]["mode"] == "refill")
    assert unload["param"]["site"] == "BEAKER1"
    assert unload["param"]["site_uuid"] == SITE_UUID
    assert refill["param"]["site"] == "BEAKER1"
    assert refill["param"]["site_uuid"] == SITE_UUID
    repeated = _compile_with_production_template(
        compiled.normalized_python_source,
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_load_name_and_barcode_round_trip() -> None:
    source = _source().replace(
        "timeout_seconds=45,\n    )",
        'timeout_seconds=45,\n        name="tip",\n        barcode="TIP-BOX-01",\n    )',
        1,
    )
    compiled = _compile_with_production_template(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    load = compiled.graph["nodes"][0]
    assert load["param"]["name"] == "tip"
    assert load["param"]["barcode"] == "TIP-BOX-01"
    assert "barcode='TIP-BOX-01'" in compiled.normalized_python_source
    repeated = _compile_with_production_template(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph["nodes"][0]["param"]["barcode"] == "TIP-BOX-01"


def test_serial_unload_load_with_nullable_material_and_output_passes_public_graph() -> None:
    source = f'''from typing import TypedDict
from lab.resources import plate_96
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import material_io, resource_ref, workflow, workflow_output


class ReplenishmentResult(TypedDict):
    material: ResourceSlot


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Tip replenishment",
    workflow_type="experiment_operation",
)
def replenish() -> ReplenishmentResult:
    # unilab:node_uuid={UNLOAD_UUID}
    unloaded = material_io(
        mode="unload",
        resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"),
        site="{SITE_UUID}",
        material=resource_ref("{FIXED_MATERIAL_UUID}"),
        disposition="soft_delete",
        scope="material_with_contents",
        timeout_seconds=3600,
    )
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load",
        resource_template=plate_96,
        mount=resource_ref("{MOUNT_UUID}"),
        site="{SITE_UUID}",
        material=None,
        disposition=None,
        scope=None,
        timeout_seconds=3600,
    )
    return workflow_output(material=loaded)
'''
    compiled = _compile_with_production_template(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["workflow"]["workflow_type"] == "experiment_operation"
    assert compiled.graph["workflow"]["meta_data"]["unilab"]["output_bindings"][
        "material"
    ]["workflow_node_uuid"] == LOAD_UUID

    repeated = _compile_with_production_template(
        compiled.normalized_python_source,
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_material_io_freezes_executor_without_device_dispatch_contract() -> None:
    compiled = _compile(_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    assert [job["executor_kind"] for job in jobs] == ["material_io", "material_io"]
    assert all("device_id" not in node for node in plan["nodes"])
    assert plan["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 45}
    resource_plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {
                "workflow_uuid": WORKFLOW_UUID,
                "nodes": [{"uuid": node["uuid"]} for node in plan["nodes"]],
            }
        ),
        {},
    )
    plan["resource_plan"] = serialize_resource_plan(resource_plan)
    plan["capabilities"] = [
        *plan.get("capabilities", []),
        RESOURCE_PLAN_CAPABILITY,
    ]
    for node in plan["nodes"]:
        node["resource_plan_id"] = resource_plan.plan_id
        node["resource_interval_ids"] = []
        node["resource_acquire_set_id"] = ""
    task: dict[str, Any] = {
        "uuid": "61000000-0000-4000-8000-000000000001",
        "execution_plan": plan,
    }
    spec = WorkflowSpecCompiler().compile(task, jobs)
    assert [node.executor_kind for node in spec.nodes] == [
        "material_io",
        "material_io",
    ]
    assert all(node.device_id == "scheduler-control" for node in spec.nodes)
    assert all(node.resource_plan_id == resource_plan.plan_id for node in spec.nodes)


def _compile_material_io_with_job_material(
    *,
    mode: str,
    planned_material: dict[str, str] | None,
    job_material: dict[str, str] | None,
) -> dict[str, Any]:
    """编译一个装卸节点，并返回运行参数。

    计划参数里的 material 可以仍是 None；作业参数携带来源准入后的绑定。
    """

    compiled = _compile(_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    resource_plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {
                "workflow_uuid": WORKFLOW_UUID,
                "nodes": [{"uuid": node["uuid"]} for node in plan["nodes"]],
            }
        ),
        {},
    )
    plan["resource_plan"] = serialize_resource_plan(resource_plan)
    plan["capabilities"] = [
        *plan.get("capabilities", []),
        RESOURCE_PLAN_CAPABILITY,
    ]
    target = next(node for node in plan["nodes"] if node["param"]["mode"] == mode)
    for node in plan["nodes"]:
        node["resource_plan_id"] = resource_plan.plan_id
        node["resource_interval_ids"] = []
        node["resource_acquire_set_id"] = ""
    target["param"] = {**target["param"], "material": planned_material}
    job = next(item for item in jobs if item["workflow_node_uuid"] == target["uuid"])
    job["param"] = {**job["param"], "material": job_material}
    task: dict[str, Any] = {
        "uuid": "61000000-0000-4000-8000-000000000001",
        "execution_plan": plan,
    }
    spec = WorkflowSpecCompiler().compile(task, jobs)
    compiled_node = next(node for node in spec.nodes if node.id == target["uuid"])
    return compiled_node.param


def test_refill_output_uses_upstream_material_template() -> None:
    source = MaterialSourceDeclaration(
        node_uuid="21000000-0000-4000-8000-000000000010",
        result_name="bottle",
        title=None,
        description=None,
        resource_template_symbol="lab.resources:bottle",
        mode="existing",
        mount_resource_id=None,
        mount_binding=None,  # type: ignore[arg-type]
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role="consumable",
        custody_policy="task_exclusive",
        source_node=None,  # type: ignore[arg-type]
    )
    refill = MaterialIODeclaration(
        node_uuid=REFILL_UUID,
        result_name="refilled",
        title=None,
        description=None,
        mode="refill",
        resource_template_symbol=None,
        mount_resource_id=MOUNT_UUID,
        site=None,
        site_uuid=None,
        site_uuid_declared=False,
        site_parameter=None,
        material_binding=MaterialIOBinding("node_output", "material", "bottle"),
        disposition=None,
        scope=None,
        timeout_seconds=45,
        reagent={"quantity": 10, "quantity_unit": "mL"},
        load_name=None,
        load_barcode=None,
        source_node=None,  # type: ignore[arg-type]
    )
    nodes = {"bottle": (source, None), "refilled": (refill, None)}
    assert _output_resource_template_symbol(refill, nodes) == "lab.resources:bottle"
    assert _output_resource_template_symbol(source, nodes) == "lab.resources:bottle"


def test_refill_output_port_inherits_upstream_template() -> None:
    from unilabos.workflow.material_graph_validation import _resolved_producer_schema

    bottle = "70000000-0000-4000-8000-000000000001"
    io_template = "70000000-0000-4000-8000-000000000002"
    source_uuid = "21000000-0000-4000-8000-000000000010"
    source_out = "70000000-0000-4000-8000-000000000011"
    refill_in = "70000000-0000-4000-8000-000000000012"
    refill_out = "70000000-0000-4000-8000-000000000013"

    def handle(uuid: str, template: str, io_type: str) -> dict[str, str]:
        return {
            "uuid": uuid,
            "workflow_node_template_uuid": template,
            "handle_key": "material",
            "io_type": io_type,
        }

    handles = {
        source_out: handle(source_out, "src-tpl", "source"),
        refill_in: handle(refill_in, io_template, "target"),
        refill_out: handle(refill_out, io_template, "source"),
    }
    source_param = {"resource_template_uuid": bottle}
    refill_param = {"mode": "refill", "resource_template_uuid": None}
    nodes = {
        source_uuid: {
            "uuid": source_uuid,
            "type": "material_source",
            "param": source_param,
        },
        REFILL_UUID: {
            "uuid": REFILL_UUID,
            "type": "material_io",
            "param": refill_param,
        },
    }
    schema = _resolved_producer_schema(
        node_uuid=REFILL_UUID,
        source_handle_uuid=refill_out,
        nodes=nodes,
        templates={},
        handles=handles,
        effective_params={
            source_uuid: source_param,
            REFILL_UUID: refill_param,
        },
        incoming_by_target={
            (REFILL_UUID, refill_in): {
                "source_node_uuid": source_uuid,
                "source_handle_uuid": source_out,
                "target_node_uuid": REFILL_UUID,
                "target_handle_uuid": refill_in,
            }
        },
        cache={},
        resolving=set(),
    )
    assert schema == {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [bottle],
    }


def test_material_graph_accepts_refill_chain_and_rejects_other_template() -> None:
    """source → refill → refill → 限模板动作应通过；换一个模板则拒绝。"""

    from unilabos.workflow.material_graph_validation import (
        MaterialGraphValidationError,
        validate_material_graph,
    )

    bottle = "70000000-0000-4000-8000-000000000001"
    other = "70000000-0000-4000-8000-000000000009"
    io_template = "70000000-0000-4000-8000-000000000002"
    action_template = "70000000-0000-4000-8000-000000000003"
    source_uuid = "21000000-0000-4000-8000-000000000010"
    first_refill = "21000000-0000-4000-8000-000000000011"
    second_refill = "21000000-0000-4000-8000-000000000012"
    action_uuid = "21000000-0000-4000-8000-000000000013"

    def handle(uuid: str, template: str, io_type: str, *, allow: str | None = None) -> dict:
        unilab: dict = {"value_schema": {"$slot": "ResourceSlot"}}
        if allow is not None:
            unilab["allowed_resource_template_uuids"] = [allow]
        return {
            "uuid": uuid,
            "workflow_node_template_uuid": template,
            "handle_key": "material" if template != action_template else "reagent_bottle",
            "io_type": io_type,
            "type": "ResourceSlot",
            "meta_data": {"unilab": unilab},
        }

    source_out = "h-source-out"
    io_in, io_out = "h-io-in", "h-io-out"
    action_in = "h-action-in"
    handles = {
        source_out: handle(source_out, "src-tpl", "source"),
        io_in: handle(io_in, io_template, "target"),
        io_out: handle(io_out, io_template, "source"),
        action_in: handle(action_in, action_template, "target", allow=bottle),
    }
    source_param = {"mode": "existing", "resource_template_uuid": bottle}
    refill_param = {"mode": "refill", "resource_template_uuid": None}
    nodes = [
        {"uuid": source_uuid, "type": "material_source", "param": source_param},
        {"uuid": first_refill, "type": "material_io", "param": refill_param},
        {"uuid": second_refill, "type": "material_io", "param": refill_param},
        {"uuid": action_uuid, "type": "device_action", "param": {}},
    ]
    edges = [
        {
            "source_node_uuid": source_uuid,
            "source_handle_uuid": source_out,
            "target_node_uuid": first_refill,
            "target_handle_uuid": io_in,
        },
        {
            "source_node_uuid": first_refill,
            "source_handle_uuid": io_out,
            "target_node_uuid": second_refill,
            "target_handle_uuid": io_in,
        },
        {
            "source_node_uuid": second_refill,
            "source_handle_uuid": io_out,
            "target_node_uuid": action_uuid,
            "target_handle_uuid": action_in,
        },
    ]
    params = {
        source_uuid: source_param,
        first_refill: refill_param,
        second_refill: refill_param,
        action_uuid: {},
    }
    validate_material_graph(
        nodes=nodes,
        edges=edges,
        templates={},
        handles=handles,
        effective_params=params,
        validated_workflow_io=None,
    )
    handles[action_in]["meta_data"]["unilab"]["allowed_resource_template_uuids"] = [other]
    with pytest.raises(MaterialGraphValidationError) as rejected:
        validate_material_graph(
            nodes=nodes,
            edges=edges,
            templates={},
            handles=handles,
            effective_params=params,
            validated_workflow_io=None,
        )
    assert rejected.value.code == "material_template_mismatch"


def test_unload_and_refill_keep_job_material_binding() -> None:
    bound = {"uuid": "51000000-0000-4000-8000-000000000099"}
    unload = _compile_material_io_with_job_material(
        mode="unload",
        planned_material=None,
        job_material=bound,
    )
    assert unload["material"] == bound
    assert unload["mode"] == "unload"
    assert unload["timeout_seconds"] == 45

    compiled = _compile(_source())
    assert compiled.valid and compiled.graph is not None
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    resource_plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {
                "workflow_uuid": WORKFLOW_UUID,
                "nodes": [{"uuid": node["uuid"]} for node in plan["nodes"]],
            }
        ),
        {},
    )
    plan["resource_plan"] = serialize_resource_plan(resource_plan)
    plan["capabilities"] = [*plan.get("capabilities", []), RESOURCE_PLAN_CAPABILITY]
    refill = next(node for node in plan["nodes"] if node["param"]["mode"] == "unload")
    for node in plan["nodes"]:
        node["resource_plan_id"] = resource_plan.plan_id
        node["resource_interval_ids"] = []
        node["resource_acquire_set_id"] = ""
    refill["param"] = {
        **refill["param"],
        "mode": "refill",
        "material": None,
        "disposition": None,
        "scope": None,
        "amount": {"quantity": 10, "quantity_unit": "mL"},
    }
    job = next(item for item in jobs if item["workflow_node_uuid"] == refill["uuid"])
    job["param"] = {
        **refill["param"],
        "material": bound,
        "timeout_seconds": 1,
    }
    spec = WorkflowSpecCompiler().compile(
        {"uuid": "61000000-0000-4000-8000-000000000001", "execution_plan": plan},
        jobs,
    )
    compiled_refill = next(node for node in spec.nodes if node.id == refill["uuid"])
    assert compiled_refill.param["material"] == bound
    assert compiled_refill.param["mode"] == "refill"
    assert compiled_refill.param["amount"] == {"quantity": 10, "quantity_unit": "mL"}
    assert compiled_refill.param["timeout_seconds"] == 45
    assert compiled_refill.manual_confirmation == {"timeout_seconds": 45}

    frozen = _compile_material_io_with_job_material(
        mode="unload",
        planned_material={"uuid": FIXED_MATERIAL_UUID},
        job_material=None,
    )
    assert frozen["material"] == {"uuid": FIXED_MATERIAL_UUID}

    loaded = _compile_material_io_with_job_material(
        mode="load",
        planned_material=None,
        job_material=None,
    )
    assert loaded["material"] is None


def test_unload_accepts_static_resource_reference_without_data_edge() -> None:
    source = _source().replace(
        "material=loaded,",
        f'material=resource_ref("{FIXED_MATERIAL_UUID}"),',
    )
    compiled = _compile(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert not any(
        edge["source_handle_uuid"] == MATERIAL_SOURCE
        and edge["target_handle_uuid"] == MATERIAL_TARGET
        for edge in compiled.graph["edges"]
    )
    assert compiled.graph["nodes"][1]["param"]["material"] == {
        "uuid": FIXED_MATERIAL_UUID
    }
    assert f'material=resource_ref("{FIXED_MATERIAL_UUID}")' in (
        compiled.normalized_python_source
    )


def test_load_optional_reagent_round_trip_and_unload_rejects_reagent() -> None:
    source = _source().replace(
        "timeout_seconds=45,\n    )",
        '''timeout_seconds=45,
        reagent={"name": "乙醇", "cas": "64-17-5", "quantity": 25, "quantity_unit": "mL"},
    )''',
        1,
    )
    compiled = _compile(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["nodes"][0]["param"]["reagent"] == {
        "name": "乙醇",
        "cas": "64-17-5",
        "physical_state": "liquid",
        "quantity": 25,
        "quantity_unit": "mL",
    }
    assert "reagent=" in compiled.normalized_python_source
    repeated = _compile(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph

    reuse_source = _source().replace(
        "timeout_seconds=45,\n    )",
        '''timeout_seconds=45,
        reagent={"reagent_info_uuid": "61000000-0000-4000-8000-000000000099", "quantity": 8, "quantity_unit": "mL"},
    )''',
        1,
    )
    reused = _compile(reuse_source)
    assert reused.valid and reused.graph is not None, reused.diagnostics
    assert reused.graph["nodes"][0]["param"]["reagent"] == {
        "reagent_info_uuid": "61000000-0000-4000-8000-000000000099",
        "quantity": 8,
        "quantity_unit": "mL",
    }

    unload_with_reagent = _source().replace(
        'scope="material_with_contents",\n        timeout_seconds=45,',
        'scope="material_with_contents",\n        timeout_seconds=45,\n        reagent={"name": "乙醇", "quantity": 1, "quantity_unit": "mL"},',
    )
    rejected = _compile(unload_with_reagent)
    assert not rejected.valid
    assert rejected.diagnostics[0]["code"] == "invalid_material_io"

    both = _source().replace(
        "timeout_seconds=45,\n    )",
        '''timeout_seconds=45,
        reagent={"reagent_info_uuid": "61000000-0000-4000-8000-000000000099", "name": "乙醇", "quantity": 8, "quantity_unit": "mL"},
    )''',
        1,
    )
    assert not _compile(both).valid


def test_material_io_rejects_invalid_mode_contract_and_timeout() -> None:
    invalid_load = _source().replace(
        "material=None,\n        disposition=None,",
        'material=None,\n        disposition="detach",',
        1,
    )
    result = _compile(invalid_load)
    assert not result.valid
    assert result.diagnostics[0]["code"] == "invalid_material_io"

    result = _compile(_source(timeout=86401))
    assert not result.valid
    assert result.diagnostics[0]["code"] == "invalid_material_io"


def test_refill_authoring_round_trip_and_rejects_identity() -> None:
    source = f'''from lab.resources import plate_96
from unilabos.workflow.authoring import material_io, resource_ref, workflow, workflow_output


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Material IO refill")
def material_io_workflow():
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load",
        resource_template=plate_96,
        mount=resource_ref("{MOUNT_UUID}"),
        site="{SITE_UUID}",
        material=None,
        disposition=None,
        scope=None,
        timeout_seconds=45,
        reagent={{"name": "乙醇", "cas": "64-17-5", "quantity": 10, "quantity_unit": "mL"}},
    )
    # unilab:node_uuid={REFILL_UUID}
    refilled = material_io(
        mode="refill",
        resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"),
        site=None,
        material=loaded,
        disposition=None,
        scope=None,
        timeout_seconds=45,
        reagent={{"quantity": 15, "quantity_unit": "mL"}},
    )
    return workflow_output()
'''
    compiled = _compile(source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    refill = compiled.graph["nodes"][1]
    assert refill["param"] == {
        "mode": "refill",
        "resource_template_uuid": None,
        "mount": {"uuid": MOUNT_UUID},
        "site": None,
        "material": None,
        "disposition": None,
        "scope": None,
        "timeout_seconds": 45,
        "reagent": {"quantity": 15, "quantity_unit": "mL"},
    }
    assert "mode='refill'" in compiled.normalized_python_source
    assert "reagent={'quantity': 15, 'quantity_unit': 'mL'}" in (
        compiled.normalized_python_source
    )
    repeated = _compile(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph

    static = source.replace(
        "material=loaded,",
        f'material=resource_ref("{FIXED_MATERIAL_UUID}"),',
    )
    static_compiled = _compile(static)
    assert static_compiled.valid and static_compiled.graph is not None
    assert static_compiled.graph["nodes"][1]["param"]["material"] == {
        "uuid": FIXED_MATERIAL_UUID
    }

    rejected_identity = _compile(
        source.replace(
            'reagent={"quantity": 15, "quantity_unit": "mL"}',
            'reagent={"name": "乙醇", "quantity": 15, "quantity_unit": "mL"}',
        )
    )
    assert not rejected_identity.valid
    assert rejected_identity.diagnostics[0]["code"] == "invalid_material_io"

    rejected_missing = _compile(
        source.replace(
            '        reagent={"quantity": 15, "quantity_unit": "mL"},\n',
            "",
        )
    )
    assert not rejected_missing.valid
    assert rejected_missing.diagnostics[0]["code"] == "invalid_material_io"

    production = _compile_with_production_template(source)
    assert production.valid and production.graph is not None, production.diagnostics
