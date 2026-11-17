"""调度权威的注册表目录只来自 Host 上报：上报前为空并明确报错，上报后供各消费方使用。

权威进程（有 Registry Authority）读生效条目，不读本进程的 ``lab_registry``；Host 进程（没有
Registry Authority）读本地扫描。覆盖 ``registry/catalog.py`` 与它的消费方：出库资源类目录 /
按资源类实例化、图登记的设备模板 Site、模板实例化的动作类型、工作流编译与模板实例化的
「注册表尚未上报」。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import ok_data
from unilabos.protocol.base import canonical_hash
from unilabos.registry.registry import lab_registry
from unilabos.server.services.runtime.registry import RegistryService, set_registry_service

TIP_RACK = {
    "id": "tip_rack_300",
    "registry_type": "resource",
    "display_name": "300ul 枪头盒",
    "class": {
        "module": "pylabrobot.resources.opentrons.tip_racks:opentrons_96_tiprack_300ul",
        "type": "pylabrobot",
        "action_value_mappings": {},
    },
    "handles": [],
}
SITES = [{"id": "slot-1", "name": "slot-1", "size": {"width": 1, "height": 1, "depth": 1}}]
PUMP = {
    "id": "pump",
    "registry_type": "device",
    "display_name": "泵",
    "available_sites": SITES,
    "class": {
        "module": "lab.pump:Pump",
        "type": "python",
        "action_value_mappings": {"dose": {"type": "UniLabJsonCommand", "goal": {}, "handles": {
            "input": [{"handler_key": "liquid_in"}], "output": [{"handler_key": "liquid_out"}],
        }}},
    },
    "handles": [],
}


@pytest.fixture()
def authority(tmp_path, monkeypatch):
    """本进程是调度权威：Registry Authority 已装配，本地扫描里另有一个权威不该看到的条目。"""

    monkeypatch.setitem(lab_registry.resource_type_registry, "scanned_only", {
        "class": {"module": "pylabrobot.resources:Plate", "type": "pylabrobot"}, "display_name": "本地扫描",
    })
    monkeypatch.setitem(lab_registry.device_type_registry, "scanned_device", {
        "class": {"module": "x:Y", "type": "python", "action_value_mappings": {}}, "available_sites": SITES,
    })
    service = RegistryService(tmp_path / "runtime.db", machine_id="authority-machine")
    set_registry_service(service)
    try:
        yield service
    finally:
        set_registry_service(None)
        service.close()


def _report(service: RegistryService, *definitions: dict[str, Any]) -> None:
    service.report_entries([
        {"id": item["id"], "content_sha256": canonical_hash(item), "payload": item} for item in definitions
    ])


def test_catalog_is_empty_before_the_first_report_and_ignores_the_local_scan(authority) -> None:
    from unilabos.registry import catalog

    assert catalog.catalog_reported() is False
    assert catalog.device_entries() == {} and catalog.resource_entries() == {}
    assert catalog.device_entry("scanned_device") is None
    with pytest.raises(catalog.RegistryNotReported, match="注册表尚未上报"):
        catalog.require_reported()

    _report(authority, PUMP, TIP_RACK)

    assert catalog.catalog_reported() is True
    assert set(catalog.device_entries()) == {"pump"} and set(catalog.resource_entries()) == {"tip_rack_300"}
    assert catalog.device_entry("pump")["class"]["module"] == "lab.pump:Pump"
    assert catalog.device_entry("tip_rack_300") is None  # 类型不符


def test_host_process_reads_its_local_scan(monkeypatch) -> None:
    from unilabos.registry import catalog

    set_registry_service(None)
    monkeypatch.setitem(lab_registry.device_type_registry, "scanned_device", {"class": {"module": "x:Y"}})
    assert catalog.catalog_reported() is True
    assert "scanned_device" in catalog.device_entries()
    assert catalog.device_entry("scanned_device")["class"]["module"] == "x:Y"


def test_registry_classes_and_instantiation_follow_the_reported_catalog(authority, tmp_path) -> None:
    from unilabos.protocol.materials import MaterialInstantiate
    from unilabos.server.api.materials import install_materials_api
    from unilabos.server.services.materials import MaterialsService
    from unilabos.server.services.materials.inventory_commands import instantiate_tree
    from unilabos.server.services.materials.core import MaterialValidationError

    materials = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, materials)
    try:
        with TestClient(app) as client:
            assert ok_data(client.get("/api/v1/materials/registry-classes")) == []
            with pytest.raises(MaterialValidationError, match="注册表尚未上报"):
                instantiate_tree(MaterialInstantiate(registry_class="tip_rack_300", name="tips"))

            _report(authority, TIP_RACK)

            assert ok_data(client.get("/api/v1/materials/registry-classes")) == [
                {"registry_class": "tip_rack_300", "display_name": "300ul 枪头盒"}
            ]
            tree = instantiate_tree(MaterialInstantiate(registry_class="tip_rack_300", name="tips"))
            assert tree.nodes[0].identity.name == "tips"
            # 已上报但没有这个资源类：照旧是「不可实例化」，不是「尚未上报」
            with pytest.raises(MaterialValidationError, match="不可实例化"):
                instantiate_tree(MaterialInstantiate(registry_class="scanned_only", name="plate"))
    finally:
        materials.close()


def test_material_io_load_instantiates_from_the_reported_catalog(authority, tmp_path) -> None:
    """调度权威上的人工装入：注册表同步的模板以资源条目 id 为名，按 Host 上报的生效条目实例化；
    上报前、或只有本地扫描里有的资源类，挂到设备上都被拒绝。"""

    from tests.server.test_material_io_inventory import _ensure_template, _param, device_deck
    from unilabos.client.materials.core import LocalMaterialsClient
    from unilabos.resources.adapters.registry_materials import register_resource_definitions
    from unilabos.server.services.materials import MaterialsService
    from unilabos.server.services.materials.core import MaterialValidationError
    from unilabos.server.services.materials.material_io import MaterialIO, material_io_command_id

    materials = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(materials)
    try:
        register_resource_definitions([TIP_RACK], gateway)
        _ensure_template(materials, "scanned-only-template", "scanned_only")
        mount, _sites = device_deck(materials)
        material_io = MaterialIO(materials)
        with pytest.raises(MaterialValidationError, match="注册表尚未上报"):
            material_io.plan(_param("load", mount, site="A1", resource_template="tip_rack_300"),
                             task_uuid="task", job_uuid="j0")

        _report(authority, TIP_RACK)
        with pytest.raises(MaterialValidationError, match="registry 资源类不可实例化: 'scanned_only'"):
            material_io.plan(_param("load", mount, site="A1", resource_template="scanned_only"),
                             task_uuid="task", job_uuid="j1")
        plan = material_io.check(_param("load", mount, site="A1", resource_template="tip_rack_300"),
                                 task_uuid="task", job_uuid="j2")
        response = gateway.inventory_command({"command_id": material_io_command_id("j2"), "type": "material.io",
                                              "payload": plan, "actor": "scheduler"})
        assert response["status"] == "completed", response
        assert len(materials.get_tree(response["result"]["material_uuid"]).nodes) == 1 + 96 + 96
    finally:
        materials.close()


def test_graph_site_templates_and_template_action_types_use_reported_entries(authority) -> None:
    from unilabos.registry.workflows import _action_type_from_registry
    from unilabos.resources.adapters.device_site import registry_device_site_templates
    from unilabos.server.services.runtime.workflow.convert_from_json import get_action_handles

    assert registry_device_site_templates() == {}
    assert _action_type_from_registry("pump", "dose") == ""

    _report(authority, PUMP)

    assert registry_device_site_templates() == {"pump": SITES}
    assert _action_type_from_registry("pump", "dose") == "UniLabJsonCommand"
    assert get_action_handles("pump", "dose") == {"source": ["liquid_in"], "target": ["liquid_out"]}


def test_workflow_compile_says_registry_not_reported(authority) -> None:
    from unilabos.registry.workflows import DeviceCatalog
    from unilabos.server.services.runtime.workflow.authoring_compiler import WorkflowAuthoringCompiler

    catalog = DeviceCatalog()
    catalog.add("pump-1", "pump", str(uuid4()))
    compiler = WorkflowAuthoringCompiler(catalog_provider=lambda: catalog, registry_fingerprint=lambda: "r")
    workflow_uuid = str(uuid4())
    source = (
        "from unilabos.registry.workflows import workflow\n\n\n"
        "@workflow(\"加样\")\n"
        "def flow(ctx):\n"
        "    ctx.run(\"pump-1/dose\", {}, name=\"加样\")\n"
    )
    applied = {"workflow": {"uuid": workflow_uuid, "name": "加样", "meta_data": {}},
               "nodes": [], "edges": [], "handle_templates": []}

    def compile_once() -> dict[str, Any]:
        return compiler.compile(workflow_uuid=workflow_uuid, workflow_revision=1, python_source=source,
                                source_uri="package://local/flow.py", applied_graph=applied)

    failed = compile_once()
    assert failed["graph"] is None
    assert [item["code"] for item in failed["diagnostics"]] == ["registry_not_reported"]

    _report(authority, PUMP)

    compiled = compile_once()
    assert compiled["diagnostics"] == []
    assert compiled["graph"]["nodes"][0]["action_type"] == "UniLabJsonCommand"


def test_template_instantiation_says_registry_not_reported(authority, tmp_path) -> None:
    from unilabos.server.api.runtime.workflow import create_workflow_app
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    service = WorkflowService(tmp_path / "workflow.db")
    try:
        client = TestClient(create_workflow_app(service))
        body = {"template_uuid": str(uuid4()), "bindings": {}}
        response = client.post("/api/v1/workflows/from-template", json=body).json()
        assert response["error"]["code"] == "registry_not_reported"

        _report(authority, PUMP)

        response = client.post("/api/v1/workflows/from-template", json=body).json()
        assert response["code"] != 0 and response["error"].get("code") != "registry_not_reported"
    finally:
        service.close()


def test_structural_node_templates_exist_before_any_report() -> None:
    from unilabos.server.services.runtime.registry import template_uuid
    from unilabos.server.services.runtime.workflow.node_templates import project_registry_templates

    templates, handles = project_registry_templates([], resource_template_uuid_of=template_uuid)

    assert {item["name"] for item in templates} >= {"condition", "loop", "group", "material_io"}
    assert {item["resource_template_uuid"] for item in templates} == {template_uuid("host_node")}
    assert handles  # 条件 / 循环 / 人工物料带 ready 连接点
