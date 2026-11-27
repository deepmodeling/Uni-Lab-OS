"""工作流定义增量编辑公共合同。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import _template
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.models import (
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore


def _client(tmp_path, *, with_import: bool = False, material_resolver=None):
    """创建隔离的 Local 工作流定义服务。"""

    store = WorkflowStore(tmp_path / "workflow_definition_edit.db")
    if not with_import:
        return TestClient(create_workflow_app(WorkflowService(store))), store
    selected_root = tmp_path / "domain"
    (selected_root / "demo_domain").mkdir(parents=True)
    selected_root.joinpath("package.yaml").write_text(
        "package:\n  name: demo_domain\nworkflows: []\n",
        encoding="utf-8",
    )
    plan = discover_editable_sources((selected_root,))
    template, handles = _template(
        "30000000-0000-4000-8000-000000000090",
        name="noop",
        handles=[],
    )
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([template], handles)
    )
    with store.transaction() as connection:
        store._ensure_authoring_catalog_projection(
            connection,
            node_templates=[template],
            handle_templates=handles,
            authority_id=engine.template_catalog_fingerprint,
            now="2026-08-28T00:00:00+00:00",
        )
    service = WorkflowService(
        store,
        compiler=engine,
        source_target=DomainWorkflowSourceTarget.from_discovery_plan(plan),
        material_resolver=material_resolver,
    )
    service.replace_discovered_source_authorizations(plan)
    return TestClient(create_workflow_app(service)), store


def test_source_backed_node_projects_fixed_material_binding(tmp_path) -> None:
    """增量创作必须把已选设备物料可信投影到源码和应用图。"""

    material_uuid = "51000000-0000-4000-8000-000000000001"
    client, store = _client(
        tmp_path,
        with_import=True,
        material_resolver=lambda identity: {
            "uuid": identity,
            "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        } if identity == material_uuid else None,
    )
    workflow = _workflow(client, "固定设备操作")
    response = client.post(
        f"/api/v1/workflows/{workflow['uuid']}/nodes",
        json={
            "workflow_node_template_uuid": "30000000-0000-4000-8000-000000000090",
            "material_uuid": material_uuid,
            "name": "noop",
            "description": "固定设备动作",
            "param": {},
            "meta_data": {"unilab": {"executor_binding": {"mode": "fixed", "device_id": "untrusted"}}},
        },
    )

    assert response.status_code == 201, response.json()
    node = response.json()["data"]
    assert node["material_uuid"] == material_uuid
    assert node["meta_data"]["unilab"]["executor_binding"] == {
        "mode": "fixed",
        "device_id": material_uuid,
    }
    source_path = next((tmp_path / "domain" / "demo_domain").rglob("workflow_*.py"))
    assert f"device('{material_uuid}')" in source_path.read_text(encoding="utf-8")
    store.close()


def _workflow(client: TestClient, name: str = "增量编辑") -> dict:
    """创建一份空工作流并返回公共投影。"""

    return client.post(
        "/api/v1/workflows",
        json={"name": name, "tags": [], "meta_data": {}},
    ).json()["data"]


def _compute_node(name: str) -> dict:
    """返回不依赖设备模板的计算节点创建参数。"""

    return {
        "name": name,
        "type": "compute",
        "pose": {"x": 10, "y": 20},
        "param": {"expression": "1 + 1"},
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {},
    }


def test_node_crud_duplicate_and_page_contract(tmp_path) -> None:
    """节点增删改查和复制必须共享整图修订并使用 Backend 分页形状。"""

    client, store = _client(tmp_path)
    workflow = _workflow(client)
    workflow_uuid = workflow["uuid"]

    created_response = client.post(
        f"/api/v1/workflows/{workflow_uuid}/nodes",
        json=_compute_node("计算 A"),
    )
    assert created_response.status_code == 201
    created = created_response.json()["data"]
    assert created["workflow_uuid"] == workflow_uuid
    assert created["name"] == "计算 A"

    page = client.get(
        f"/api/v1/workflows/{workflow_uuid}/nodes",
        params={"page": 1, "page_size": 1},
    ).json()["data"]
    assert set(page) == {"items", "total", "page", "page_size"}
    assert page["total"] == 1
    assert page["items"][0]["uuid"] == created["uuid"]

    detail = client.get(
        f"/api/v1/workflow-nodes/{created['uuid']}"
    ).json()["data"]
    assert detail["uuid"] == created["uuid"]

    patched = client.patch(
        f"/api/v1/workflow-nodes/{created['uuid']}",
        json={"name": "计算 A（已修改）", "description": "用于校验"},
    ).json()["data"]
    assert patched["name"] == "计算 A（已修改）"
    assert patched["description"] == "用于校验"

    duplicate_response = client.post(
        f"/api/v1/workflow-nodes/{created['uuid']}/duplicate",
        json={},
    )
    assert duplicate_response.status_code == 201
    duplicate = duplicate_response.json()["data"]
    assert duplicate["uuid"] != created["uuid"]
    assert duplicate["name"] == "计算 A（已修改） copy"

    deleted = client.post(
        f"/api/v1/workflows/{workflow_uuid}/batch-delete",
        json={"node_uuids": [duplicate["uuid"]], "edge_uuids": []},
    ).json()["data"]
    assert [node["uuid"] for node in deleted["nodes"]] == [created["uuid"]]

    response = client.delete(f"/api/v1/workflow-nodes/{created['uuid']}")
    assert response.status_code == 200
    assert response.json() == {"code": 0}
    assert client.get(
        f"/api/v1/workflows/{workflow_uuid}/graph"
    ).json()["data"]["nodes"] == []
    assert store.get_workflow(workflow_uuid)["revision"] == 6
    store.close()


def test_duplicate_workflow_remaps_parent_identity_and_is_independent(tmp_path) -> None:
    """复制工作流须重建节点身份和父子引用，后续编辑不能影响来源图。"""

    client, store = _client(tmp_path)
    source = _workflow(client, "来源流程")
    source_uuid = source["uuid"]
    parent = client.post(
        f"/api/v1/workflows/{source_uuid}/nodes",
        json=_compute_node("父节点"),
    ).json()["data"]
    child_body = _compute_node("子节点")
    child_body["parent_uuid"] = parent["uuid"]
    child = client.post(
        f"/api/v1/workflows/{source_uuid}/nodes",
        json=child_body,
    ).json()["data"]
    source_graph = store.get_graph(source_uuid)
    store.save_graph(
        source_uuid,
        revision=3,
        nodes=[WorkflowNodeWrite.model_validate(node) for node in source_graph["nodes"]],
        edges=[],
        inventory_requirements=[
            WorkflowInventoryRequirementWrite(
                uuid="79000000-0000-4000-8000-000000000001",
                consume_node_uuid=child["uuid"],
                requirement_key="copied-liquid",
                target_type="current_substance",
                required_quantity=1,
                quantity_unit="mL",
                meta_data={
                    "unilab": {"material_source_node_uuid": parent["uuid"]}
                },
            )
        ],
    )

    copied_response = client.post(
        f"/api/v1/workflows/{source_uuid}/duplicate",
        json={"name": "复制流程"},
    )
    assert copied_response.status_code == 201
    copied = copied_response.json()["data"]
    assert copied["workflow"]["uuid"] != source_uuid
    assert copied["workflow"]["name"] == "复制流程"
    assert len(copied["nodes"]) == 2
    copied_parent = next(node for node in copied["nodes"] if node["name"] == "父节点")
    copied_child = next(node for node in copied["nodes"] if node["name"] == "子节点")
    assert copied_parent["uuid"] != parent["uuid"]
    assert copied_child["uuid"] != child["uuid"]
    assert copied_child["parent_uuid"] == copied_parent["uuid"]
    assert len(copied["inventory_requirements"]) == 1
    copied_requirement = copied["inventory_requirements"][0]
    assert copied_requirement["uuid"] != "79000000-0000-4000-8000-000000000001"
    assert copied_requirement["consume_node_uuid"] == copied_child["uuid"]
    assert copied_requirement["meta_data"]["unilab"][
        "material_source_node_uuid"
    ] == copied_parent["uuid"]

    client.patch(
        f"/api/v1/workflow-nodes/{copied_child['uuid']}",
        json={"name": "复制图内子节点"},
    )
    source_graph = client.get(
        f"/api/v1/workflows/{source_uuid}/graph"
    ).json()["data"]
    assert next(node for node in source_graph["nodes"] if node["uuid"] == child["uuid"])[
        "name"
    ] == "子节点"
    store.close()


def test_batch_delete_rejects_unknown_identity_without_partial_write(tmp_path) -> None:
    """批量删除含未知身份时必须关闭式失败并保留整图。"""

    client, store = _client(tmp_path)
    workflow = _workflow(client)
    workflow_uuid = workflow["uuid"]
    node = client.post(
        f"/api/v1/workflows/{workflow_uuid}/nodes",
        json=_compute_node("保留节点"),
    ).json()["data"]

    response = client.post(
        f"/api/v1/workflows/{workflow_uuid}/batch-delete",
        json={
            "node_uuids": ["ffffffff-ffff-4fff-8fff-ffffffffffff"],
            "edge_uuids": [],
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 1000
    graph = client.get(
        f"/api/v1/workflows/{workflow_uuid}/graph"
    ).json()["data"]
    assert [item["uuid"] for item in graph["nodes"]] == [node["uuid"]]
    store.close()


def test_legacy_import_accepts_wrapped_payload_and_rebuilds_identities(tmp_path) -> None:
    """旧版导入须原子创建首版图，并且不能沿用来源节点身份。"""

    client, store = _client(tmp_path, with_import=True)
    old_node_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    response = client.post(
        "/api/v1/workflows/import",
        json={
            "data": {
                "workflow_name": "旧版流程",
                "tags": ["legacy"],
                "meta_data": {},
                "nodes": [
                    {
                        "uuid": old_node_uuid,
                        "workflow_node_template_uuid": (
                            "30000000-0000-4000-8000-000000000090"
                        ),
                        **_compute_node("旧计算节点"),
                        "description": "旧版节点说明",
                    }
                ],
                "edges": [],
                "inventory_requirements": [],
            }
        },
    )
    assert response.status_code == 201
    imported = response.json()["data"]
    assert imported["workflow"]["name"] == "旧版流程"
    assert imported["workflow"]["revision"] == 1
    assert imported["nodes"][0]["uuid"] != old_node_uuid
    assert imported["nodes"][0]["name"] == "旧计算节点"
    assert store.count_rows("workflow") == 1
    store.close()


def test_legacy_import_explains_json_field_and_reference_errors(tmp_path) -> None:
    """旧版 JSON 导入失败时应指出缺少字段、字段位置或模板引用问题。"""

    client, store = _client(tmp_path, with_import=True)
    try:
        missing_nodes = client.post(
            "/api/v1/workflows/import",
            json={"name": "缺少节点的流程"},
        )
        assert missing_nodes.status_code == 200
        assert missing_nodes.json()["code"] == 1000
        assert "nodes 必须是至少包含一个节点的数组" in missing_nodes.json()[
            "error"
        ]["msg"]

        malformed_json = client.post(
            "/api/v1/workflows/import",
            content=b'{"name":',
            headers={"Content-Type": "application/json"},
        )
        assert malformed_json.status_code == 200
        assert malformed_json.json()["code"] == 1000
        assert malformed_json.json()["error"]["msg"] == (
            "JSON 工作流导入失败：请求体不是合法 JSON，请检查引号、括号、逗号和字段类型"
        )

        invalid_root = client.post(
            "/api/v1/workflows/import",
            json=[{"name": "不能是数组"}],
        )
        assert invalid_root.status_code == 200
        assert invalid_root.json()["code"] == 1000
        assert invalid_root.json()["error"]["msg"] == (
            "JSON 工作流导入失败：请求体必须是 JSON 对象"
        )

        invalid_nodes_type = client.post(
            "/api/v1/workflows/import",
            json={"name": "节点类型错误的流程", "nodes": "不是数组"},
        )
        assert invalid_nodes_type.status_code == 200
        assert invalid_nodes_type.json()["code"] == 1000
        assert invalid_nodes_type.json()["error"]["msg"] == (
            "JSON 工作流导入失败：字段 nodes 必须是数组"
        )

        invalid_node_uuid = client.post(
            "/api/v1/workflows/import",
            json={
                "name": "节点身份错误的流程",
                "nodes": [{"uuid": "not-a-uuid", "type": "compute"}],
                "edges": [],
            },
        )
        assert invalid_node_uuid.status_code == 200
        assert invalid_node_uuid.json()["code"] == 1000
        assert "nodes[0].uuid" in invalid_node_uuid.json()["error"]["msg"]

        missing_template = client.post(
            "/api/v1/workflows/import",
            json={
                "name": "模板不存在的流程",
                "nodes": [
                    {
                        "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "workflow_node_template_uuid": (
                            "ffffffff-ffff-4fff-8fff-ffffffffffff"
                        ),
                    }
                ],
                "edges": [],
            },
        )
        assert missing_template.status_code == 200
        assert missing_template.json()["code"] == 3002
        assert "nodes[0] 引用的工作流节点模板不存在" in missing_template.json()[
            "error"
        ]["msg"]
        assert "ffffffff-ffff-4fff-8fff-ffffffffffff" in missing_template.json()[
            "error"
        ]["msg"]
        assert store.count_rows("workflow") == 0
    finally:
        client.close()
        store.close()
