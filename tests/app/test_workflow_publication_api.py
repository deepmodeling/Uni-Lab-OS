"""工作流不可变发布（PublishedWorkflowContract）的公开接口合同。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.json_codec import decode_json_bytes
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _client(tmp_path):
    """创建隔离的本地工作流公开接口测试客户端。"""

    store = WorkflowStore(tmp_path / "workflow_publication.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), store


class _EmptyTemplateProjection:
    """本地动作目录投影；发布的组合模板不属于该目录。"""

    def snapshot(self) -> AuthoringCatalogSnapshot:
        return AuthoringCatalogSnapshot.from_entities([], [])


def _create_workflow_with_one_node(
    client: TestClient,
    *,
    workflow_type: str = "normal",
) -> tuple[str, int]:
    """创建一张可发布的单节点工作流图并返回身份与修订。

    参数：``client`` 通过公开 HTTP 接口操作本地权威；``workflow_type`` 选择普通
    工作流或实验操作。返回：工作流稳定 UUID 与当前修订。异常：创建或图保存
    失败时由断言暴露，测试不绕过服务公开边界。
    """

    created = client.post(
        "/api/v1/workflows",
        json={
            "name": "样品审核",
            "tags": ["published"],
            "meta_data": {},
            "workflow_type": workflow_type,
        },
    )
    assert created.status_code == 201
    workflow_uuid = created.json()["data"]["uuid"]
    graph = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "name": "人工确认",
                    "type": "manual_confirm",
                    "pose": {"x": 80, "y": 60},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200
    return workflow_uuid, graph.json()["data"]["workflow"]["revision"]


def test_publication_is_idempotent_and_freezes_each_revision(tmp_path) -> None:
    """同一修订重复发布须复用合同，新修订须生成不影响旧版的新合同。

    参数：``tmp_path`` 隔离 SQLite 权威。返回：无。异常：发布身份、版本递增、
    图快照不可变或公开字段偏离 Backend 合同时由断言暴露。
    """

    client, store = _client(tmp_path)
    workflow_uuid, revision = _create_workflow_with_one_node(client)

    first = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    )
    assert first.status_code == 201
    first_contract = first.json()["data"]
    assert first_contract["workflow_uuid"] == workflow_uuid
    assert first_contract["workflow_revision"] == revision
    assert first_contract["version"] == 1
    assert first_contract["node_count"] == 1
    assert first_contract["edge_count"] == 0
    assert first_contract["source_hash"].startswith("sha256:")
    assert first_contract["revision_fingerprint"] == first_contract["source_hash"]
    assert first_contract["contract_digest"].startswith("sha256:")
    assert first_contract["input_contract"] == {"version": 1, "parameters": []}
    assert "inputs" not in first_contract["input_contract"]
    assert "graph_snapshot" not in first_contract

    repeated = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    )
    assert repeated.status_code == 201
    assert repeated.json()["data"]["uuid"] == first_contract["uuid"]

    updated = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": revision,
            "nodes": [
                {
                    "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "name": "人工确认（修订版）",
                    "type": "manual_confirm",
                    "pose": {"x": 120, "y": 60},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert updated.status_code == 200
    next_revision = updated.json()["data"]["workflow"]["revision"]
    second = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": next_revision},
    )
    assert second.status_code == 201
    second_contract = second.json()["data"]
    assert second_contract["version"] == 2
    assert second_contract["uuid"] != first_contract["uuid"]
    assert second_contract["source_hash"] != first_contract["source_hash"]

    stored_first = store._conn.execute(
        "SELECT graph_snapshot FROM published_workflow_contract WHERE uuid = ?",
        (first_contract["uuid"],),
    ).fetchone()
    frozen_graph = decode_json_bytes(stored_first["graph_snapshot"].encode("utf-8"))
    assert frozen_graph["nodes"][0]["name"] == "人工确认"
    store.close()


def test_publication_list_returns_latest_contract_per_workflow(tmp_path) -> None:
    """发行目录只返回每个来源工作流的最新完整版本。"""

    client, store = _client(tmp_path)
    workflow_uuid, revision = _create_workflow_with_one_node(client)
    published = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    ).json()["data"]

    response = client.get(
        "/api/v1/published-workflow-contracts",
        params={"page": 1, "page_size": 20, "keyword": "样品"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "items": [published],
        "has_more": False,
        "page": 1,
        "page_size": 20,
    }
    store.close()


def test_workflow_read_models_expose_source_and_published_status(tmp_path) -> None:
    """工作流列表与详情按当前修订返回源码或已发布状态。

    参数：``tmp_path`` 隔离工作流和发布合同存储。返回：无。异常：公开列表、详情
    或完整图没有随修订在 ``source`` 与 ``published`` 间切换时由断言暴露。
    """

    client, store = _client(tmp_path)
    created = client.post(
        "/api/v1/workflows",
        json={"name": "状态展示", "tags": [], "meta_data": {}},
    )
    assert created.status_code == 201
    workflow_uuid = created.json()["data"]["uuid"]
    assert created.json()["data"]["status"] == "source"
    assert created.json()["data"]["published_revision"] is None
    assert created.json()["data"]["has_unpublished_changes"] is False

    listed_source = client.get("/api/v1/workflows").json()["data"]["items"]
    assert listed_source[0]["status"] == "source"
    assert listed_source[0]["has_unpublished_changes"] is False
    assert client.get(f"/api/v1/workflows/{workflow_uuid}").json()["data"][
        "status"
    ] == ("source")

    graph = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "name": "人工确认",
                    "type": "manual_confirm",
                    "pose": {"x": 80, "y": 60},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200
    assert graph.json()["data"]["workflow"]["status"] == "source"
    revision = graph.json()["data"]["workflow"]["revision"]
    published = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    )
    assert published.status_code == 201
    published_detail = client.get(f"/api/v1/workflows/{workflow_uuid}").json()["data"]
    assert published_detail["status"] == "published"
    assert published_detail["published_revision"] == revision
    assert published_detail["has_unpublished_changes"] is False
    assert (
        client.get(f"/api/v1/workflows/{workflow_uuid}/graph").json()["data"][
            "workflow"
        ]["status"]
        == "published"
    )

    changed = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": revision,
            "nodes": [
                {
                    "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "name": "人工确认（修改）",
                    "type": "manual_confirm",
                    "pose": {"x": 100, "y": 60},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["workflow"]["status"] == "source"
    changed_detail = client.get(f"/api/v1/workflows/{workflow_uuid}").json()["data"]
    assert changed_detail["status"] == "source"
    assert changed_detail["published_revision"] == revision
    assert changed_detail["revision"] > revision
    assert changed_detail["has_unpublished_changes"] is True
    listed_changed = client.get(
        "/api/v1/workflows", params={"status": "source"}
    ).json()["data"]["items"]
    assert [item["uuid"] for item in listed_changed] == [workflow_uuid]
    assert listed_changed[0]["has_unpublished_changes"] is True
    store.close()


def test_withdrawn_workflow_has_no_center_visible_publication(tmp_path) -> None:
    """下架后中心不再收到合同，读模型也不能再声称已有发布版本。"""

    store = WorkflowStore(tmp_path / "workflow_publication.db")
    service = WorkflowService(store)
    client = TestClient(create_workflow_app(service))
    workflow_uuid, revision = _create_workflow_with_one_node(client)
    assert client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    ).status_code == 201
    service._withdrawals().withdraw(workflow_uuid)

    detail = client.get(f"/api/v1/workflows/{workflow_uuid}").json()["data"]
    assert detail["published_revision"] is None
    assert detail["has_unpublished_changes"] is False
    assert workflow_uuid not in {
        item["workflow_uuid"]
        for item in service.export_published_workflow_catalog()["contracts"]
    }
    store.close()


def test_publication_rejects_stale_revision_and_empty_graph(tmp_path) -> None:
    """发布必须命中当前修订，并拒绝没有节点的空工作流。"""

    client, store = _client(tmp_path)
    workflow_uuid, revision = _create_workflow_with_one_node(client)
    stale = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision + 1},
    )
    assert stale.status_code == 200
    assert stale.json()["code"] == 3003

    empty = client.post(
        "/api/v1/workflows",
        json={"name": "空工作流", "tags": [], "meta_data": {}},
    ).json()["data"]
    rejected = client.post(
        f"/api/v1/workflows/{empty['uuid']}/publications",
        json={"revision": empty["revision"]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["code"] == 1000
    store.close()


def test_composite_invocation_preserves_reference_and_private_frozen_contract(tmp_path) -> None:
    """定义 API 保留调用节点，内部执行图保留不可变子合同的私有节点。

    参数：``tmp_path`` 隔离引用方与实验操作。返回：无。异常：调用根身份、私有展开图、
    修订推进或冻结合同 pin 偏离 Backend 公共接口时由断言暴露。
    """

    client, store = _client(tmp_path)
    child_uuid, child_revision = _create_workflow_with_one_node(
        client,
        workflow_type="experiment_operation",
    )
    contract = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": child_revision},
    ).json()["data"]
    parent = client.post(
        "/api/v1/workflows",
        json={"name": "父工作流", "tags": [], "meta_data": {}},
    ).json()["data"]
    invocation_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    inserted = client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={
            "revision": parent["revision"],
            "contract_uuid": contract["uuid"],
            "invocation_uuid": invocation_uuid,
            "device_bindings": {},
            "pose": {"x": 320, "y": 100},
            "param": {},
        },
    )
    assert inserted.status_code == 200
    graph = inserted.json()["data"]
    assert graph["workflow"]["revision"] == 2
    assert len(graph["nodes"]) == 1
    root = next(node for node in graph["nodes"] if node["uuid"] == invocation_uuid)
    persisted = store.get_graph(parent["uuid"])
    assert len(persisted["nodes"]) == 2
    child = next(node for node in persisted["nodes"] if node["uuid"] != invocation_uuid)
    assert root["type"] == "workflow"
    assert root["workflow_node_template_uuid"] == contract["node_template_uuid"]
    assert root["meta_data"]["unilab"]["composite"]["contract_uuid"] == contract["uuid"]
    compatibility = root["meta_data"]["unilab"]["composite"]["contract_compatibility"]
    assert compatibility["parameters"] == []
    assert "inputs" not in compatibility
    assert child["parent_uuid"] == invocation_uuid
    assert child["name"] == "人工确认"
    store.close()


def test_composite_invocation_persists_isolated_resource_scopes_with_graph(
    tmp_path,
) -> None:
    """组合插入必须按调用隔离并原子持久化实验操作连续资源作用域。"""

    client, store = _client(tmp_path)
    child_uuid, child_revision = _create_workflow_with_one_node(
        client,
        workflow_type="experiment_operation",
    )
    child = store.get_workflow(child_uuid)
    store.update_workflow(
        child_uuid,
        name=child["name"],
        tags=child["tags"],
        description=child.get("description"),
        workflow_type=child["workflow_type"],
        meta_data={
            "unilab": {
                "resource_scopes": [
                    {
                        "scope_id": "operation-reservation",
                        "kind": "with",
                        "resources": [
                            "source-turntable",
                            "target-turntable",
                            "robot",
                        ],
                        "parent_scope_id": None,
                        "entry_node_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "exit_node_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "node_uuids": ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
                        "hard_boundary": True,
                        "source": "authoring.with.resources",
                    }
                ]
            }
        },
    )
    contract = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": child_revision},
    ).json()["data"]
    parent = client.post(
        "/api/v1/workflows",
        json={"name": "父工作流", "tags": [], "meta_data": {}},
    ).json()["data"]
    invocation_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    inserted = client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={
            "revision": parent["revision"],
            "contract_uuid": contract["uuid"],
            "invocation_uuid": invocation_uuid,
            "device_bindings": {},
            "pose": {"x": 320, "y": 100},
            "param": {},
        },
    )

    assert inserted.status_code == 200
    graph = inserted.json()["data"]
    persisted = store.get_graph(parent["uuid"])
    assert graph["workflow"]["meta_data"]["unilab"]["resource_scopes"] == []
    scopes = persisted["workflow"]["meta_data"]["unilab"]["resource_scopes"]
    assert len(scopes) == 1
    assert scopes[0]["resources"] == [
        "source-turntable",
        "target-turntable",
        "robot",
    ]
    assert scopes[0]["composite_invocation_uuid"] == invocation_uuid

    second_invocation_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    inserted_again = client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={
            "revision": graph["workflow"]["revision"],
            "contract_uuid": contract["uuid"],
            "invocation_uuid": second_invocation_uuid,
            "device_bindings": {},
            "pose": {"x": 520, "y": 100},
            "param": {},
        },
    )
    assert inserted_again.status_code == 200
    persisted_again = store.get_graph(parent["uuid"])
    scopes = persisted_again["workflow"]["meta_data"]["unilab"]["resource_scopes"]
    assert len(scopes) == 2
    assert len({scope["scope_id"] for scope in scopes}) == 2
    assert {scope["composite_invocation_uuid"] for scope in scopes} == {
        invocation_uuid,
        second_invocation_uuid,
    }
    store.close()


def test_same_published_operation_can_be_invoked_twice_in_one_parent(tmp_path) -> None:
    """同一已发布实验操作的两次调用须拥有独立调用根与展开节点。

    参数：``tmp_path`` 隔离发布合同与父工作流存储。返回：无。异常：若公共
    HTTP 接口按子工作流身份错误去重，或两次静态展开产生相同节点身份，由断言暴露。
    """

    client, store = _client(tmp_path)
    child_uuid, child_revision = _create_workflow_with_one_node(
        client,
        workflow_type="experiment_operation",
    )
    contract = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": child_revision},
    ).json()["data"]
    parent = client.post(
        "/api/v1/workflows",
        json={"name": "重复调用父工作流", "tags": [], "meta_data": {}},
    ).json()["data"]
    invocation_uuids = (
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )

    graph = None
    revision = parent["revision"]
    for invocation_uuid in invocation_uuids:
        response = client.post(
            f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
            json={
                "revision": revision,
                "contract_uuid": contract["uuid"],
                "invocation_uuid": invocation_uuid,
                "device_bindings": {},
                "pose": {"x": 120, "y": 100},
                "param": {},
            },
        )
        assert response.status_code == 200
        assert response.json()["code"] == 0
        graph = response.json()["data"]
        revision = graph["workflow"]["revision"]

    assert graph is not None
    roots = [node for node in graph["nodes"] if node["type"] == "workflow"]
    assert {node["uuid"] for node in roots} == set(invocation_uuids)
    assert len(graph["nodes"]) == 2
    descendants = [node for node in store.get_graph(parent["uuid"])["nodes"] if node.get("parent_uuid")]
    assert {node["parent_uuid"] for node in descendants} == set(invocation_uuids)
    assert len({node["uuid"] for node in descendants}) == 2
    store.close()


def test_composite_invocation_resolves_published_template_with_catalog_projection(
    tmp_path,
) -> None:
    """本地动作目录存在时，父图仍须解析发布合同生成的组合节点模板。"""

    projection = _EmptyTemplateProjection()
    store = WorkflowStore(
        tmp_path / "workflow_publication_local.db",
        template_snapshot_provider=projection,
    )
    service = WorkflowService(store)
    client = TestClient(
        create_workflow_app(service, template_snapshot_provider=projection)
    )
    try:
        child_uuid, child_revision = _create_workflow_with_one_node(
            client,
            workflow_type="experiment_operation",
        )
        contract = client.post(
            f"/api/v1/workflows/{child_uuid}/publications",
            json={"revision": child_revision},
        ).json()["data"]
        parent = client.post(
            "/api/v1/workflows",
            json={"name": "本地目录父工作流", "tags": [], "meta_data": {}},
        ).json()["data"]

        inserted = client.post(
            f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
            json={
                "revision": parent["revision"],
                "contract_uuid": contract["uuid"],
                "device_bindings": {},
                "pose": {"x": 120, "y": 80},
                "param": {},
            },
        )
        assert inserted.status_code == 200
        assert inserted.json()["code"] == 0
        graph = inserted.json()["data"]
        root = next(node for node in graph["nodes"] if node["type"] == "workflow")
        assert root["workflow_node_template_uuid"] == contract["node_template_uuid"]
        assert len(graph["nodes"]) == 1
        assert len(store.get_graph(parent["uuid"])["nodes"]) == 2
    finally:
        store.close()


def test_only_published_experiment_operation_can_be_composite_child(tmp_path) -> None:
    """组合调用只允许已发布实验操作，普通工作流发布后仍不得作为子工作流。

    参数：``tmp_path`` 隔离工作流与发布合同存储。返回：无；通过公开 HTTP
    接口验证普通工作流合同被拒绝且父图保持不变。异常：若类型门禁缺失或拒绝后
    仍写入调用节点，由 pytest 断言报告。
    """

    client, store = _client(tmp_path)
    child_uuid, child_revision = _create_workflow_with_one_node(client)
    published = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": child_revision},
    )
    assert published.status_code == 201
    contract = published.json()["data"]

    parent = client.post(
        "/api/v1/workflows",
        json={"name": "实验操作父图", "tags": [], "meta_data": {}},
    ).json()["data"]
    attempted = client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={
            "revision": parent["revision"],
            "contract_uuid": contract["uuid"],
            "invocation_uuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "device_bindings": {},
            "pose": {"x": 320, "y": 100},
            "param": {},
        },
    )
    assert attempted.status_code == 200
    assert attempted.json()["code"] == 1000
    graph = client.get(f"/api/v1/workflows/{parent['uuid']}/graph")
    assert graph.status_code == 200
    assert graph.json()["data"]["nodes"] == []
    store.close()


def test_referenced_by_lists_workflows_using_experiment_operation(tmp_path) -> None:
    """反向引用接口须分页返回真正包含实验操作调用的父工作流。

    参数：``tmp_path`` 隔离工作流定义和发布合同。返回：无。异常：接口依赖名称
    或标签猜测、漏掉组合调用、混入无关工作流、分页顺序不稳定或未知目标未关闭
    失败时由断言暴露；全部准备和查询均经过公开 HTTP 接口。
    """

    client, store = _client(tmp_path)
    operation_uuid, operation_revision = _create_workflow_with_one_node(
        client,
        workflow_type="experiment_operation",
    )
    contract = client.post(
        f"/api/v1/workflows/{operation_uuid}/publications",
        json={"revision": operation_revision},
    ).json()["data"]
    empty = client.get(f"/api/v1/workflows/{operation_uuid}/referenced-by").json()[
        "data"
    ]
    assert empty["items"] == []
    assert empty["has_more"] is False
    # 两个固定 UUID 分别表示两张引用方画布中的稳定调用节点身份；它们只用于
    # 证明反向引用按工作流去重，不是实验操作或发布合同身份。
    for name, invocation_uuid in (
        ("A-甲父工作流", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("B-乙父工作流", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    ):
        parent = client.post(
            "/api/v1/workflows",
            json={"name": name, "tags": [], "meta_data": {}},
        ).json()["data"]
        inserted = client.post(
            f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
            json={
                "revision": parent["revision"],
                "contract_uuid": contract["uuid"],
                "invocation_uuid": invocation_uuid,
                "device_bindings": {},
                "pose": {"x": 320, "y": 100},
                "param": {},
            },
        )
        assert inserted.status_code == 200, inserted.text
    client.post(
        "/api/v1/workflows",
        json={"name": "无关工作流", "tags": [], "meta_data": {}},
    )

    first_page = client.get(
        f"/api/v1/workflows/{operation_uuid}/referenced-by",
        params={"page": 1, "page_size": 1},
    )
    second_page = client.get(
        f"/api/v1/workflows/{operation_uuid}/referenced-by",
        params={"page": 2, "page_size": 1},
    )

    assert first_page.status_code == 200
    assert [item["name"] for item in first_page.json()["data"]["items"]] == [
        "A-甲父工作流"
    ]
    assert first_page.json()["data"]["has_more"] is True
    assert [item["name"] for item in second_page.json()["data"]["items"]] == [
        "B-乙父工作流"
    ]
    assert second_page.json()["data"]["has_more"] is False
    # 该固定 UUID 表示一个确定不存在的实验操作，用于验证未知目标关闭式失败。
    missing = client.get(
        "/api/v1/workflows/00000000-0000-4000-8000-000000000001/referenced-by"
    )
    assert missing.status_code == 200
    assert missing.json()["code"] != 0
    store.close()


def test_new_operation_publication_refreshes_parent_and_nested_grandparent(
    tmp_path,
) -> None:
    """实验操作重新发布后须自动替换引用方和上游图中的兼容调用。

    参数：``tmp_path`` 隔离实验操作、引用方及发布合同。返回：无。异常：图没有自动
    推进修订、嵌套调用没有向上游传播、调用身份或画布位置变化、合同仍指向旧版
    或内部节点没有更新时由断言暴露；修改只使用公开 HTTP 接口，另读取存储验证
    未在定义 API 展示的私有执行节点。
    """

    client, store = _client(tmp_path)
    child_uuid, child_revision = _create_workflow_with_one_node(
        client,
        workflow_type="experiment_operation",
    )
    first_contract = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": child_revision},
    ).json()["data"]
    parent = client.post(
        "/api/v1/workflows",
        json={
            "name": "自动更新父工作流",
            "tags": [],
            "meta_data": {},
            "workflow_type": "experiment_operation",
        },
    ).json()["data"]
    # ``invocation_uuid`` 是引用方图中对实验操作第一次调用的稳定身份；刷新前后必须
    # 保持不变，才能证明外部连线和画布布局不会因子版本替换而漂移。
    invocation_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    inserted = client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={
            "revision": parent["revision"],
            "contract_uuid": first_contract["uuid"],
            "invocation_uuid": invocation_uuid,
            "device_bindings": {},
            "pose": {"x": 320, "y": 100},
            "param": {},
        },
    ).json()["data"]
    assert inserted["workflow"]["revision"] == 2

    parent_contract = client.post(
        f"/api/v1/workflows/{parent['uuid']}/publications",
        json={"revision": inserted["workflow"]["revision"]},
    ).json()["data"]
    grandparent = client.post(
        "/api/v1/workflows",
        json={"name": "上游组合工作流", "tags": [], "meta_data": {}},
    ).json()["data"]
    # ``grandparent_invocation_uuid`` 是上游父图引用中间父工作流的稳定身份，用于
    # 证明刷新会向祖父依赖传播，而不会把两级调用误认成同一节点。
    grandparent_invocation_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    grandparent_graph = client.post(
        f"/api/v1/workflows/{grandparent['uuid']}/composite-invocations",
        json={
            "revision": grandparent["revision"],
            "contract_uuid": parent_contract["uuid"],
            "invocation_uuid": grandparent_invocation_uuid,
            "device_bindings": {},
            "pose": {"x": 80, "y": 40},
            "param": {},
        },
    ).json()["data"]
    assert grandparent_graph["workflow"]["revision"] == 2
    referenced_by = client.get(
        f"/api/v1/workflows/{child_uuid}/referenced-by",
        params={"page_size": 20},
    ).json()["data"]["items"]
    assert {item["uuid"] for item in referenced_by} == {
        parent["uuid"],
        grandparent["uuid"],
    }

    changed = client.put(
        f"/api/v1/workflows/{child_uuid}/graph",
        json={
            "revision": child_revision,
            "nodes": [
                {
                    "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "name": "人工确认（新版）",
                    "type": "manual_confirm",
                    "pose": {"x": 120, "y": 80},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert changed.status_code == 200
    second = client.post(
        f"/api/v1/workflows/{child_uuid}/publications",
        json={"revision": changed.json()["data"]["workflow"]["revision"]},
    )
    assert second.status_code == 201
    second_contract = second.json()["data"]
    assert set(second_contract["dependent_refresh"]["updated_workflow_uuids"]) == {
        parent["uuid"],
        grandparent["uuid"],
    }
    assert second_contract["dependent_refresh"]["pending"] == []

    refreshed = client.get(f"/api/v1/workflows/{parent['uuid']}/graph").json()["data"]
    assert refreshed["workflow"]["revision"] == 3
    assert len(refreshed["nodes"]) == 1
    root = next(node for node in refreshed["nodes"] if node["uuid"] == invocation_uuid)
    nested = next(
        node for node in store.get_graph(parent["uuid"])["nodes"] if node["uuid"] != invocation_uuid
    )
    assert root["pose"] == {"x": 320.0, "y": 100.0}
    assert (
        root["meta_data"]["unilab"]["composite"]["contract_uuid"]
        == (second_contract["uuid"])
    )
    assert (
        root["meta_data"]["unilab"]["composite"]["child_workflow_revision"]
        == second_contract["workflow_revision"]
    )
    assert nested["name"] == "人工确认（新版）"

    refreshed_grandparent = client.get(
        f"/api/v1/workflows/{grandparent['uuid']}/graph"
    ).json()["data"]
    assert refreshed_grandparent["workflow"]["revision"] == 3
    assert len(refreshed_grandparent["nodes"]) == 1
    assert refreshed_grandparent["nodes"][0]["uuid"] == grandparent_invocation_uuid
    nested_invocation = next(
        node
        for node in store.get_graph(grandparent["uuid"])["nodes"]
        if node.get("meta_data", {})
        .get("unilab", {})
        .get("composite", {})
        .get("child_workflow_uuid")
        == child_uuid
    )
    assert nested_invocation["parent_uuid"] == grandparent_invocation_uuid
    assert (
        nested_invocation["meta_data"]["unilab"]["composite"]["contract_uuid"]
        == second_contract["uuid"]
    )
    store.close()
