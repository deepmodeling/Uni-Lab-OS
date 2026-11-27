"""中心冻结合同预检应提前拒绝明确的共享物料输出，且保持零占用。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.f05_material_source_http_fixture import SOURCE_HANDLE_UUID, SOURCE_NODE_UUID
from tests.app.test_f05_material_source_http_e2e import (
    _apply_workflow_graph,
    _create_material,
    _create_resource_template,
    runtime,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.published_contract import PublishedWorkflowContractStore


def published_source(runtime, policy="shared_source", *, return_input=False, automatic=False, many=False):
    template = _create_resource_template(runtime.client, resource_id="preflight.sample", display_name="样品", registry_type="material")
    device_template = _create_resource_template(runtime.client, resource_id="preflight.device", display_name="设备", registry_type="device")
    local = _create_material(runtime.client, resource_template_uuid=template, barcode="PREFLIGHT-SAMPLE", name="样品")
    if automatic:
        # 自动选料对照必须有真实源库位与容器；无库位不再仅靠输出 deferred 放行。
        site = str(uuid4())
        with runtime.inventory.store.transaction() as connection:
            connection.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name) VALUES (?,'2026-01-01','2026-01-01',?,'source')", (site, local))
        _create_material(runtime.client, resource_template_uuid=template, barcode="PREFLIGHT-OCCUPANT", name="源容器", site_uuid=site)
    device = _create_material(runtime.client, resource_template_uuid=device_template, barcode="PREFLIGHT-DEVICE", name="设备")
    workflow = _apply_workflow_graph(runtime, material_resource_template_uuid=template,
        device_resource_template_uuid=device_template, material_uuid=local,
        device_material_uuid=device, mode="existing", custody_policy=policy, automatic=automatic)
    graph = runtime.client.get(f"/api/v1/workflows/{workflow}/graph").json()["data"]
    metadata = deepcopy(graph["workflow"]["meta_data"])
    metadata["unilab"] = {
        "input_contract": {"version": 1, "parameters": []},
        "output_contract": {"version": 1, "outputs": [{"name": "sample", "schema": {"$slot": "ResourceSlot"}}]},
        "output_bindings": {"sample": {"kind": "node_output", "workflow_node_uuid": SOURCE_NODE_UUID, "source_handle_uuid": SOURCE_HANDLE_UUID}},
    }
    metadata["center_material_ports"] = [{"name": "sample", "direction": "output", "value_path": "$.sample",
        "local_value_kind": "material_ref", "cardinality": "one", "resource_template_uuid": template}]
    if return_input:
        metadata["unilab"]["input_contract"]["parameters"] = [{"name": "sample", "schema": {"type": "string"}, "required": True}]
        metadata["unilab"]["output_contract"]["outputs"][0]["schema"] = {"type": "string"}
        metadata["unilab"]["output_bindings"]["sample"] = {"kind": "workflow_input", "parameter": "sample"}
        metadata["center_material_ports"][0]["local_value_kind"] = "material_uuid"
        if many:
            schema = {"type": "array", "items": {"type": "string"}}
            metadata["unilab"]["input_contract"]["parameters"][0]["schema"] = schema
            metadata["unilab"]["output_contract"]["outputs"][0]["schema"] = schema
            metadata["center_material_ports"][0]["cardinality"] = "many"
    source = next(node for node in graph["nodes"] if node["uuid"] == SOURCE_NODE_UUID)
    graph = runtime.workflow_store.save_graph(workflow, revision=graph["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(source)], edges=[], workflow_meta_data=metadata)
    contract = PublishedWorkflowContractStore(runtime.workflow_store).publish(graph=graph, expected_revision=graph["workflow"]["revision"])
    return workflow, local, contract


def facts(runtime):
    return tuple(runtime.workflow_store.count_rows(table) for table in ("workflow_task", "workflow_node_job")) + tuple(
        runtime.inventory.store.query_all(f"SELECT * FROM {table}") for table in (
            "inventory_material_source_binding", "inventory_reservation", "inventory_ledger", "sync_outbox"))


def report(runtime, workflow, contract, *, input_value=None, input_ready=True):
    response = runtime.client.post("/api/v1/workflows/run-preflight", json={"items": [{
        "workflow_uuid": workflow, "revision_fingerprint": contract["source_hash"],
        "input": input_value or {}, "input_ready": input_ready,
    }]})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    return response.json()["data"]


def test_frozen_preflight_rejects_shared_material_output_without_reserving(runtime):
    workflow, local, contract = published_source(runtime)
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is False, result
    check = next(c for c in result["items"][0]["checks"] if c["type"] == "material_output")
    assert check["code"] == "invalid_material_output_custody"
    assert check["blocking"] is True and "共享" in check["message"]
    assert check["details"]["material_port"] == "sample"
    assert check["details"]["local_material_uuid"] == local
    assert facts(runtime) == before
    assert runtime.dispatcher.dispatched == []


def test_shared_source_returned_through_workflow_input_is_still_blocked(runtime):
    workflow, local, contract = published_source(runtime, return_input=True)
    before = facts(runtime)
    result = report(runtime, workflow, contract, input_value={"sample": local})
    assert result["can_run"] is False, result
    assert any(c["code"] == "invalid_material_output_custody" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_shared_source_in_many_output_is_checked(runtime):
    workflow, local, contract = published_source(runtime, return_input=True, many=True)
    result = report(runtime, workflow, contract, input_value={"sample": [local]})
    assert result["can_run"] is False, result
    assert any(c["code"] == "invalid_material_output_custody" for c in result["items"][0]["checks"])


def test_known_shared_output_is_checked_even_when_upstream_input_is_pending(runtime):
    workflow, _, contract = published_source(runtime, automatic=True)
    before = facts(runtime)
    result = report(runtime, workflow, contract, input_ready=False)
    assert result["can_run"] is False, result
    assert any(c["code"] == "invalid_material_output_custody" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


@pytest.mark.parametrize("automatic", [False, True])
def test_exclusive_output_preflight_does_not_acquire_task_ownership(runtime, automatic):
    workflow, _, contract = published_source(runtime, "task_exclusive", automatic=automatic)
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is True, result
    assert any(c["type"] == "material_output" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_station_native_preflight_without_center_fingerprint_keeps_existing_rules(runtime):
    workflow, _, _ = published_source(runtime)
    result = runtime.client.post(f"/api/v1/workflows/{workflow}/run-preflight", json={"input": {}}).json()["data"]
    assert result["can_run"] is True, result


@pytest.mark.parametrize("policy", ["shared_source", "task_exclusive"])
def test_known_output_current_foreign_usage_is_blocked_without_reserving(runtime, policy):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest

    workflow, local, contract = published_source(runtime, "task_exclusive")
    template = runtime.inventory.store.query_one("SELECT resource_template_uuid FROM material WHERE uuid=?", (local,))["resource_template_uuid"]
    runtime.inventory.admit_material_sources(str(uuid4()), [MaterialSourceAdmissionRequest(
        node_id=str(uuid4()), resource_template_uuid=template, custody_policy=policy,
        requirement=MaterialRequirement(template_id=template, instance_uuid=local))])
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is False, result
    assert any(c["type"] == "material_output" and c["code"] == "material_output_unavailable"
               for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_shared_child_blocks_output_carrier_preflight(runtime):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest

    workflow, carrier, contract = published_source(runtime, "task_exclusive")
    template = runtime.inventory.store.query_one("SELECT resource_template_uuid FROM material WHERE uuid=?", (carrier,))["resource_template_uuid"]
    child = _create_material(runtime.client, resource_template_uuid=template, barcode="PREFLIGHT-CHILD", name="内容物", parent_uuid=carrier)
    runtime.inventory.admit_material_sources(str(uuid4()), [MaterialSourceAdmissionRequest(
        node_id=str(uuid4()), resource_template_uuid=template, custody_policy="shared_source",
        requirement=MaterialRequirement(template_id=template, instance_uuid=child))])
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is False, result
    assert any(c["code"] == "material_output_unavailable" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_preflight_checks_original_published_custody_and_not_latest_graph(runtime):
    workflow, _, old = published_source(runtime)
    graph = runtime.workflow_store.get_graph(workflow)
    source = deepcopy(graph["nodes"][0])
    source["param"]["custody_policy"] = "task_exclusive"
    latest = runtime.workflow_store.save_graph(workflow, revision=graph["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(source)], edges=[])
    newest = PublishedWorkflowContractStore(runtime.workflow_store).publish(graph=latest, expected_revision=latest["workflow"]["revision"])
    assert report(runtime, workflow, old)["can_run"] is False
    assert report(runtime, workflow, newest)["can_run"] is True


def test_unresolved_shared_source_is_rejected_by_policy_before_selecting_inventory(runtime):
    workflow, _, contract = published_source(runtime, automatic=True)
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is False, result
    assert any(c["code"] == "invalid_material_output_custody" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_unmarked_output_does_not_add_center_material_restriction(runtime):
    workflow, _, _ = published_source(runtime)
    graph = runtime.workflow_store.get_graph(workflow)
    metadata = deepcopy(graph["workflow"]["meta_data"])
    metadata.pop("center_material_ports")
    graph = runtime.workflow_store.save_graph(workflow, revision=graph["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(node) for node in graph["nodes"]], edges=[], workflow_meta_data=metadata)
    contract = PublishedWorkflowContractStore(runtime.workflow_store).publish(graph=graph, expected_revision=graph["workflow"]["revision"])
    result = report(runtime, workflow, contract)
    assert result["can_run"] is True, result
    assert not any(c["type"] == "material_output" for c in result["items"][0]["checks"])


def test_plain_inventory_reservation_also_blocks_known_output(runtime):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement

    workflow, local, contract = published_source(runtime, "task_exclusive")
    template = runtime.inventory.store.query_one("SELECT resource_template_uuid FROM material WHERE uuid=?", (local,))["resource_template_uuid"]
    runtime.inventory.reserve_workflow(str(uuid4()), {str(uuid4()): [MaterialRequirement(template_id=template, instance_uuid=local)]})
    before = facts(runtime)
    result = report(runtime, workflow, contract)
    assert result["can_run"] is False, result
    assert any(c["code"] == "material_output_unavailable" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_many_output_checks_every_known_shared_material(runtime):
    workflow, local, _ = published_source(runtime, return_input=True)
    graph = runtime.workflow_store.get_graph(workflow)
    metadata = deepcopy(graph["workflow"]["meta_data"])
    schema = {"type": "array", "items": {"type": "string"}}
    metadata["unilab"]["input_contract"]["parameters"][0]["schema"] = schema
    metadata["unilab"]["output_contract"]["outputs"][0]["schema"] = schema
    metadata["center_material_ports"][0]["cardinality"] = "many"
    graph = runtime.workflow_store.save_graph(workflow, revision=graph["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(node) for node in graph["nodes"]], edges=[], workflow_meta_data=metadata)
    contract = PublishedWorkflowContractStore(runtime.workflow_store).publish(graph=graph, expected_revision=graph["workflow"]["revision"])
    before = facts(runtime)
    result = report(runtime, workflow, contract, input_value={"sample": [local]})
    assert result["can_run"] is False, result
    assert any(c["code"] == "invalid_material_output_custody" for c in result["items"][0]["checks"])
    assert facts(runtime) == before


def test_device_generated_output_does_not_inherit_unrelated_shared_source_policy():
    from tests.workflow.test_f05_material_source_execution_responsibility import _source_only_graph
    from tests.workflow.test_qg01_implicit_workflow_material_output import (
        _compile,
        _source_with_implicit_material_output,
    )
    from unilabos.workflow.material_output_preflight import material_output_checks

    compiled = _compile(_source_with_implicit_material_output())
    assert compiled.valid, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    other = _source_only_graph()
    other["nodes"][0]["param"]["custody_policy"] = "shared_source"
    for field in ("nodes", "node_templates", "handle_templates"):
        graph[field].extend(deepcopy(other[field]))
    graph["workflow"]["meta_data"]["center_material_ports"] = [{
        "name": "report", "direction": "output", "value_path": "$.report",
        "local_value_kind": "material_uuid", "cardinality": "one", "resource_template_uuid": str(uuid4()),
    }]
    checks = material_output_checks(graph)
    assert len(checks) == 1
    assert checks[0]["code"] == "material_output_checked_at_runtime"
    assert checks[0]["status"] == "deferred" and checks[0]["blocking"] is False
