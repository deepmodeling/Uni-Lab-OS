"""物料参数按库位绑定：``{"site_uuid": ...}`` 在建任务 / 运行预检时解析成占着该库位的物料并冻结。

节点参数、工作流入口参数（ResourceSlot）与单点动作参数都可以这样写；参数写了 ``AllowedResourceTemplates`` 时核对
占用物料的模板。库位不存在、库位为空、模板不符是预检错误（``material_site_invalid``，``reason`` 分别为
``site_not_found`` / ``site_empty`` / ``template_mismatch``）。解析结果冻结进任务：之后物料被挪走，任务仍用同一件
物料；之后它就是普通物料参数（默认共享物料锁）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.server.test_scheduler_inventory_authority import _material, _mutation
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteCreate,
)
from unilabos.protocol.runtime.site_references import material_at_site
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowError

SAMPLE_SYMBOL = "lab.samples:Tube"
_LOCK = {"type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"}}, "required": ["uuid"],
         "x-unilabos-material-lock": True}


def _schema(*, symbols: tuple[str, ...] = ()) -> dict:
    return {
        "x-unilabos-action-contract": {
            "version": 2, "resource_template_symbols": {"goal": {"sample": list(symbols)} if symbols else {}}},
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "sample": _LOCK,
                    "batch": {"type": ["array", "null"], "items": _LOCK, "default": None},
                },
                "required": ["sample"],
            },
            "result": {},
        },
    }


def _template(stack: Stack, name: str, module_name: str | None = None) -> None:
    stack.materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
        template_uuid=f"{name}-template", name=name, display_name=name, resource_type="container",
        class_name="Container", module_name=module_name))


def _rack(stack: Stack) -> tuple[str, dict[str, str]]:
    """一个三位样品架：A、B 位各有一支管，C 位空着。"""

    _template(stack, "rack")
    _template(stack, "tube", SAMPLE_SYMBOL)

    def identity(name: str, template: str) -> MaterialIdentityWrite:
        return MaterialIdentityWrite(resource_id=f"{name}-{uuid4()}", name=f"{name}-{uuid4()}", template_name=template,
                                     resource_type="container", class_name="Container")

    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="rack", identity=identity("rack", "rack"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A", occupied_client_ref="tube-a"),
            SiteCreate(template_name="slot", site_index=1, label="B", occupied_client_ref="tube-b"),
            SiteCreate(template_name="slot", site_index=2, label="C")]),
        MaterialNodeCreate(client_ref="tube-a", parent_client_ref="rack", identity=identity("tube-a", "tube")),
        MaterialNodeCreate(client_ref="tube-b", parent_client_ref="rack", identity=identity("tube-b", "tube")),
    ])).data
    rack = tree.client_ref_map["rack"]
    sites = {site.label: site.site_uuid for site in stack.materials.get_material(rack).sites}
    return rack, {**sites, "tube-a": tree.client_ref_map["tube-a"], "tube-b": tree.client_ref_map["tube-b"]}


def _node(param: dict) -> dict:
    return {"uuid": str(uuid4()), "name": "处理样品", "type": "device_action", "material_uuid": str(uuid4()),
            "action_name": "process", "meta_data": {"target_device_id": "processor"}, "param": param}


def _workflow(stack: Stack, nodes: list[dict], *, io: dict | None = None, schema: dict | None = None) -> dict:
    stack.workflow.configure_preflight(materials=stack.client, endpoints=lambda: [],
                                       action_schema_resolver=lambda *_: schema or _schema())
    payload = {"workflow_uuid": str(uuid4()), "name": "按库位取样", "description": "", "tags": [], "edges": [],
               "nodes": nodes}
    if io is not None:
        payload["io"] = io
    return upsert_workflow(stack.workflow, payload)


def _create(stack: Stack, record: dict, input_value: dict | None = None) -> dict:
    return stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value=input_value or {}, description=None, meta_data={})


def test_node_param_bound_by_site_is_frozen_to_the_occupant_and_locked(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = None
    try:
        _rack_uuid, refs = _rack(stack)
        node = _node({"sample": material_at_site(refs["A"]), "batch": [{"site_uuid": refs["B"]}]})
        record = _workflow(stack, [node])
        task = _create(stack, record)
        [planned] = task["execution_plan"]["nodes"]
        assert planned["param"]["sample"] == {"uuid": refs["tube-a"]}
        assert planned["param"]["batch"] == [{"uuid": refs["tube-b"]}]
        assert {(item["parameter"], item["site_uuid"], item["material_uuid"])
                for item in task["execution_plan"]["material_site_bindings"]} == {
            ("sample", refs["A"], refs["tube-a"]), ("batch", refs["B"], refs["tube-b"])}
        [job] = stack.workflow.list_workflow_node_jobs(task["uuid"])
        assert job["param"]["sample"] == {"uuid": refs["tube-a"]}

        # 建任务之后管子被挪走、另一支管子放进 A 位：任务仍用冻结的那一支
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=refs["tube-a"], destination_site_uuid=refs["C"]))
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=refs["tube-b"], destination_site_uuid=refs["A"]))
        scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client,
                                     materials_need_lock_resolver=lambda *_: ["sample", "batch"])
        stack.workflow.set_task_submitter(scheduler.submit)
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        payload = executor.dispatched[0]
        assert payload["action_args"]["sample"] == {"uuid": refs["tube-a"]}
        # 之后就是普通物料参数：默认共享物料锁锁住冻结的物料
        held = [request for request in scheduler.resource_snapshot().requests if request.owner_uuid == payload["job_id"]]
        assert held and refs["tube-a"] in str(held[0].identifiers)
        executor.finish(0, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        if scheduler is not None:
            scheduler.stop()
        stack.close()


def test_resource_slot_workflow_input_bound_by_site_freezes_the_task_input(tmp_path) -> None:
    stack = Stack(tmp_path)
    try:
        _rack_uuid, refs = _rack(stack)
        node = _node({"sample": {"kind": "workflow_input", "parameter": "sample"}})
        io = {"input_contract": {"version": 1, "parameters": [
            {"name": "sample", "schema": {"$slot": "ResourceSlot"}, "required": True}]},
              "output_contract": {"version": 1, "outputs": [
                  {"name": "sample", "schema": {"$slot": "ResourceSlot"}, "implicit": True}]},
              "output_bindings": {"sample": {"kind": "workflow_input", "parameter": "sample"}}}
        record = _workflow(stack, [node], io=io, schema=_schema(symbols=(SAMPLE_SYMBOL,)))
        task = _create(stack, record, {"sample": {"site_uuid": refs["B"]}})
        assert task["input"] == {"sample": {"uuid": refs["tube-b"]}}
        [planned] = task["execution_plan"]["nodes"]
        assert planned["param"]["sample"] == {"uuid": refs["tube-b"]}
        # 暂停改参会按 entry_param_template 重新代入：模板里仍是入口参数引用，代入的是冻结的输入
        assert planned["entry_param_template"]["sample"] == {"kind": "workflow_input", "parameter": "sample"}
    finally:
        stack.close()


@pytest.mark.parametrize("case", ["unknown", "empty", "template"])
def test_unknown_empty_or_wrong_type_site_is_a_preflight_error(tmp_path, case: str) -> None:
    stack = Stack(tmp_path)
    try:
        _rack_uuid, refs = _rack(stack)
        site = {"unknown": str(uuid4()), "empty": refs["C"], "template": refs["A"]}[case]
        symbols = ("lab.samples:Vial",) if case == "template" else ()
        record = _workflow(stack, [_node({"sample": {"site_uuid": site}})], schema=_schema(symbols=symbols))
        reason = {"unknown": "site_not_found", "empty": "site_empty", "template": "template_mismatch"}[case]

        with pytest.raises(WorkflowError) as caught:
            _create(stack, record)
        assert caught.value.code == "material_site_invalid"
        assert caught.value.details["reason"] == reason
        assert caught.value.details["parameter"] == "sample" and caught.value.details["site_uuid"] == site
        assert site in caught.value.message and "sample" in caught.value.message
        assert stack.workflow.list_tasks(page=1, page_size=10)["items"] == []

        client = TestClient(create_workflow_app(stack.workflow))
        response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": record["uuid"]}).json()
        assert response["code"] == 1000 and response["error"]["code"] == "material_site_invalid"
        assert response["error"]["details"]["reason"] == reason
        # 运行预检走同一条解析路径
        report = client.post(f"/api/v1/workflows/{record['uuid']}/run-preflight", json={}).json()["data"]
        assert report["status"] == "invalid"
        [blocked] = [item for item in report["checks"] if item["blocking"]]
        assert blocked["code"] == "invalid_material_site" and blocked["details"]["reason"] == reason
    finally:
        stack.close()


def test_preflight_reports_the_bound_material_without_writes(tmp_path) -> None:
    stack = Stack(tmp_path)
    try:
        _rack_uuid, refs = _rack(stack)
        record = _workflow(stack, [_node({"sample": {"site_uuid": refs["A"]}})], schema=_schema(symbols=(SAMPLE_SYMBOL,)))
        report = stack.workflow.preflight_workflow(workflow_uuid=record["uuid"], run_mode="normal",
                                                   target_node_uuid=None, input_value={})
        [bound] = [item for item in report["checks"] if item["code"] == "material_sites_bound"]
        assert bound["details"]["bindings"][0]["material_uuid"] == refs["tube-a"]
        assert not any(item["code"] == "invalid_action_parameters" for item in report["checks"])
        assert stack.workflow.list_tasks(page=1, page_size=10)["items"] == []
    finally:
        stack.close()


def test_ad_hoc_device_action_param_bound_by_site(tmp_path) -> None:
    stack = Stack(tmp_path)
    try:
        _rack_uuid, refs = _rack(stack)
        stack.workflow.configure_preflight(materials=stack.client, action_schema_resolver=lambda *_: _schema())
        task = stack.workflow.create_ad_hoc_device_action_task(
            device_id="processor", action_name="process", param={"sample": {"site_uuid": refs["A"]}},
            description=None, meta_data={})
        [job] = stack.workflow.list_workflow_node_jobs(task["uuid"])
        assert job["param"]["sample"] == {"uuid": refs["tube-a"]}
        with pytest.raises(WorkflowError) as caught:
            stack.workflow.create_ad_hoc_device_action_task(
                device_id="processor", action_name="process", param={"sample": {"site_uuid": refs["C"]}},
                description=None, meta_data={})
        assert caught.value.details["reason"] == "site_empty"
    finally:
        stack.close()


def test_http_materials_gateway_reads_the_same_site_facts(tmp_path, monkeypatch) -> None:
    """调度权威与物料权威分开部署时经 ``POST /inventory/site-admission`` 读同一份库位事实。"""

    from fastapi import FastAPI

    from tests.server.test_v1_inventory_api import _TestClientOpener
    from unilabos.client.materials import HTTPMaterialsClient
    from unilabos.server.api.materials import install_materials_api

    stack = Stack(tmp_path)
    try:
        _rack_uuid, refs = _rack(stack)
        app = FastAPI()
        install_materials_api(app, stack.materials)
        with TestClient(app) as client:
            monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(client))
            gateway = HTTPMaterialsClient("http://testserver")
            payload = {"site_uuids": [refs["A"], refs["C"], "missing"]}
            facts = gateway.site_admission_facts(payload)
            assert facts == stack.client.site_admission_facts(payload)
            assert facts["sites"][refs["A"]]["occupant"]["material_uuid"] == refs["tube-a"]
            assert facts["sites"][refs["A"]]["occupant"]["module_name"] == SAMPLE_SYMBOL
            assert facts["sites"][refs["C"]]["occupant"] is None
            assert facts["sites"]["missing"] == {"site_uuid": "missing", "exists": False}
    finally:
        stack.close()


def test_resource_slot_input_schema_accepts_the_site_form() -> None:
    from unilabos.server.services.runtime.workflow.value_schema import WorkflowValueSchemaError, validate_value

    site = str(uuid4())
    assert validate_value({"$slot": "ResourceSlot"}, {"site_uuid": site.upper()}) == {"site_uuid": site}
    with pytest.raises(WorkflowValueSchemaError):
        validate_value({"$slot": "ResourceSlot"}, {"site_uuid": "A1"})


def _typed_carrier(stack: Stack) -> tuple[dict[str, str], dict[str, str]]:
    """一个两位载架：A 位是深孔板，B 位是试剂槽；模板 uuid 由物料权威登记（工作流输入的允许模板按 uuid 核对）。"""

    templates = {name: str(uuid4()) for name in ("deep_well_plate", "reagent_trough")}
    for name, identity in templates.items():
        stack.materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
            template_uuid=identity, name=name, display_name=name, resource_type="container", class_name="Container"))
    _template(stack, "carrier")

    def identity(name: str, template: str) -> MaterialIdentityWrite:
        return MaterialIdentityWrite(resource_id=f"{name}-{uuid4()}", name=f"{name}-{uuid4()}", template_name=template,
                                     resource_type="container", class_name="Container")

    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="carrier", identity=identity("carrier", "carrier"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A", occupied_client_ref="plate"),
            SiteCreate(template_name="slot", site_index=1, label="B", occupied_client_ref="trough")]),
        MaterialNodeCreate(client_ref="plate", parent_client_ref="carrier", identity=identity("plate", "deep_well_plate")),
        MaterialNodeCreate(client_ref="trough", parent_client_ref="carrier", identity=identity("trough", "reagent_trough")),
    ])).data
    carrier = tree.client_ref_map["carrier"]
    sites = {site.label: site.site_uuid for site in stack.materials.get_material(carrier).sites}
    return templates, {**sites, "plate": tree.client_ref_map["plate"], "trough": tree.client_ref_map["trough"]}


def _allowlisted_input(template_uuids: list[str]) -> dict:
    slot = {"$slot": "ResourceSlot", "allowed_resource_template_uuids": template_uuids}
    return {"input_contract": {"version": 1, "parameters": [{"name": "plate_1", "schema": slot, "required": True}]},
            "output_contract": {"version": 1, "outputs": [{"name": "plate_1", "schema": slot, "implicit": True}]},
            "output_bindings": {"plate_1": {"kind": "workflow_input", "parameter": "plate_1"}}}


@pytest.mark.parametrize("form", ["site", "uuid"])
def test_workflow_input_outside_its_allowed_templates_is_rejected_at_preflight_and_submission(tmp_path, form) -> None:
    """``@workflow(allowed_templates=...)`` 写进输入 schema 的允许模板：按库位绑定与直接给 ``{"uuid"}`` 都核对。"""

    stack = Stack(tmp_path)
    try:
        templates, refs = _typed_carrier(stack)
        node = _node({"sample": {"kind": "workflow_input", "parameter": "plate_1"}})
        record = _workflow(stack, [node], io=_allowlisted_input([templates["deep_well_plate"]]))
        wrong = {"plate_1": {"site_uuid": refs["B"]} if form == "site" else {"uuid": refs["trough"]}}
        right = {"plate_1": {"site_uuid": refs["A"]} if form == "site" else {"uuid": refs["plate"]}}
        code = "material_site_invalid" if form == "site" else "material_template_mismatch"

        with pytest.raises(WorkflowError) as caught:
            _create(stack, record, wrong)
        assert caught.value.code == code
        assert caught.value.details["reason"] == "template_mismatch" and caught.value.details["parameter"] == "plate_1"
        assert "plate_1" in caught.value.message and "reagent_trough" in caught.value.message
        assert stack.workflow.list_tasks(page=1, page_size=10)["items"] == []

        client = TestClient(create_workflow_app(stack.workflow))
        response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": record["uuid"], "input": wrong}).json()
        assert response["code"] == 1000 and response["error"]["code"] == code
        report = client.post(f"/api/v1/workflows/{record['uuid']}/run-preflight", json={"input": wrong}).json()["data"]
        assert report["status"] == "invalid"
        [blocked] = [item for item in report["checks"] if item["blocking"]]
        assert blocked["code"] == ("invalid_material_site" if form == "site" else "invalid_material_template")
        assert blocked["details"]["reason"] == "template_mismatch"

        ok = client.post(f"/api/v1/workflows/{record['uuid']}/run-preflight", json={"input": right}).json()["data"]
        # 这里没有在线设备（device_action_unavailable 照常阻塞），只看物料这一项
        assert not [item for item in ok["checks"] if item["blocking"] and item["type"] == "resources"]
        task = _create(stack, record, right)
        assert task["input"] == {"plate_1": {"uuid": refs["plate"]}}

        # 工作流详情的输入 schema 带着前端筛选要用的允许模板
        detail = client.get(f"/api/v1/workflows/{record['uuid']}").json()["data"]
        assert templates["deep_well_plate"] in str(detail) and "allowed_resource_template_uuids" in str(detail)
    finally:
        stack.close()
