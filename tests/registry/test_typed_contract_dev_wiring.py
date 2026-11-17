"""严格合同显式接入 dev，旧驱动兼容且合同约束进入真正执行路径。"""
import logging
from pathlib import Path
from uuid import uuid4

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.registry.typed_contract import TypedActionValueError, validate_action_values
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.errors import StoreConflict


def typed_action(tmp_path: Path):
    path = tmp_path / "driver.py"
    path.write_text('''
from typing import Annotated, TypedDict
from pydantic import Field
from unilabos.registry.decorators import action, device
from unilabos.registry.annotations import MaterialLock
from unilabos.registry.placeholder_type import ResourceSlot

class Result(TypedDict):
    quantity: float

@device(id="typed_test")
class Driver:
    @action(contract="typed")
    def dispense(self, source: ResourceSlot, reference: Annotated[ResourceSlot, MaterialLock(free=True)],
                 volume: Annotated[float, Field(ge=0)] = 1.0) -> Result:
        raise NotImplementedError

    @action()
    def legacy(self, anything):
        return anything
''')
    devices, _, _ = _parse_file(path, tmp_path)
    return Registry()._build_device_entry_from_ast("typed_test", devices[0])["class"]["action_value_mappings"]


def test_registry_keeps_required_fields_defaults_named_results_and_material_locks(tmp_path, monkeypatch, caplog):
    from unilabos.registry import material_locks

    monkeypatch.setattr(material_locks, "_warned_definitions", set())
    caplog.set_level(logging.WARNING, logger="unilabos.registry.material_locks")
    actions = typed_action(tmp_path)
    typed = actions["dispense"]
    # 已废弃的 MaterialLock(free=True) 照常解析但不再免锁：每个物料入参都加锁，并告警一次
    assert typed["materials_need_lock"] == ["source", "reference"]
    assert typed["schema"]["properties"]["goal"]["properties"]["reference"]["x-unilabos-material-lock"] is True
    (warning,) = [record.getMessage() for record in caplog.records if record.name == "unilabos.registry.material_locks"]
    assert "driver.Driver.dispense" in warning and "reference: MaterialLock(free=True)" in warning
    assert typed["goal_default"] == {"volume": 1.0}
    assert set(typed["schema"]["properties"]["goal"]["required"]) == {"source", "reference"}
    assert typed["placeholder_keys"] == {"source": "unilabos_resources", "reference": "unilabos_resources"}
    assert "x-unilabos-action-contract" not in actions["legacy"]["schema"]
    source = {"uuid": str(uuid4())}
    assert validate_action_values(typed["schema"], {"source": source, "reference": source})["volume"] == 1
    for values in ({"source": source}, {"source": source, "reference": source, "volume": -1}):
        with pytest.raises(TypedActionValueError):
            validate_action_values(typed["schema"], values)
    with pytest.raises(TypedActionValueError):
        validate_action_values(typed["schema"], {"quantity": float("inf")}, section="result")


def test_actual_plan_rejects_static_invalid_values_but_defers_upstream_values(tmp_path):
    action = typed_action(tmp_path)["dispense"]
    node_uuid, template_uuid = str(uuid4()), str(uuid4())
    source = {"uuid": str(uuid4())}
    graph = {"workflow": {"uuid": str(uuid4()), "revision": 1}, "nodes": [{
        "uuid": node_uuid, "workflow_node_template_uuid": template_uuid, "type": "device_action", "disabled": False,
        "param": {"source": source, "reference": source, "volume": -1}, "execution_policy": {}, "meta_data": {},
    }], "edges": [], "node_templates": [{"uuid": template_uuid, "node_type": "device_action", "schema": action["schema"]}], "handle_templates": []}
    service = WorkflowService(":memory:")
    try:
        with pytest.raises(StoreConflict, match="合同校验"):
            service._build_execution_plan(graph, run_mode="normal", target_node_uuid=None)
        graph["nodes"][0]["param"]["volume"] = 2
        plan, jobs = service._build_execution_plan(graph, run_mode="normal", target_node_uuid=None)
        assert jobs[0]["param"]["volume"] == 2
        assert plan["nodes"][0]["param_schema"] == action["schema"]
    finally:
        service.close()


def test_plan_decodes_template_schema_stored_as_json_text(tmp_path):
    """节点模板行里的 schema 是 JSON 文本（注册表投影的旧合同动作都是这样）。

    执行计划原样带文本时，调度器派发前的物料核对对它调 ``.get`` 直接失败，旧合同设备
    （内置虚拟设备的全部动作）一进工作流就跑不了；计划里要是字典。
    """

    import json

    from unilabos.server.backend.scheduler.material_sanity import material_references

    action = typed_action(tmp_path)["legacy"]
    node_uuid, template_uuid = str(uuid4()), str(uuid4())
    graph = {"workflow": {"uuid": str(uuid4()), "revision": 1}, "nodes": [{
        "uuid": node_uuid, "workflow_node_template_uuid": template_uuid, "type": "device_action", "disabled": False,
        "param": {"anything": 1}, "execution_policy": {}, "meta_data": {"target_device_id": "dev"},
    }], "edges": [], "node_templates": [
        {"uuid": template_uuid, "node_type": "device_action", "schema": json.dumps(action["schema"])}
    ], "handle_templates": []}
    service = WorkflowService(":memory:")
    try:
        plan, _jobs = service._build_execution_plan(graph, run_mode="normal", target_node_uuid=None)
        schema = plan["nodes"][0]["param_schema"]
        assert schema == action["schema"]
        assert material_references(schema, {"anything": 1}, [], []) == {}
    finally:
        service.close()


def test_nodes_without_template_freeze_registered_contract(tmp_path):
    action = typed_action(tmp_path)["dispense"]
    service = WorkflowService(":memory:")
    try:
        service.configure_preflight(action_schema_resolver=lambda device, name: action["schema"])
        workflow = service.create_workflow(name="无模板动作", tags=[], description=None, meta_data={})
        reference = {"uuid": str(uuid4())}
        service.save_graph(workflow["uuid"], revision=1, nodes=[{
            "uuid": str(uuid4()), "name": "加液", "type": "device_action", "material_uuid": str(uuid4()),
            "action_name": "dispense", "param": {"source": reference, "reference": reference}, "pose": {"x": 0, "y": 0},
        }], edges=[])
        task = service.create_workflow_task(workflow_uuid=workflow["uuid"], run_mode="normal", target_node_uuid=None, input_value={}, description=None, meta_data={})
        node = task["execution_plan"]["nodes"][0]
        assert node["param_schema"] == action["schema"]
        assert node["param"]["volume"] == 1.0
    finally:
        service.close()


def test_inventory_material_allocation_matches_strict_resource_reference(tmp_path):
    from unilabos.protocol.materials import InventoryAllocation
    from unilabos.server.backend.scheduler.service import allocation_arguments

    schema = typed_action(tmp_path)["dispense"]["schema"]
    identity = str(uuid4())
    items = [InventoryAllocation(key=key, kind="material", template_uuid="plate", material_uuid=identity)
             for key in ("source", "reference")]
    args = allocation_arguments(items, action_schema=schema)
    assert args == {"source": {"uuid": identity}, "reference": {"uuid": identity}}
    assert validate_action_values(schema, args)["volume"] == 1.0
