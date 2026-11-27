"""实验操作类别公共 HTTP 合同回归。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _engine, _source
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore


def _client(
    tmp_path: Path,
    *,
    compiler: WorkflowAuthoringEngine | None = None,
) -> tuple[TestClient, WorkflowService, WorkflowStore, WorkflowStore, Path]:
    """建立带唯一领域包的隔离工作流应用。

    参数：``tmp_path`` 是 pytest 临时目录；可选 ``compiler`` 可替换默认真实
    AST 编译器。返回：公共客户端、服务、运行事实库、内存定义目录和类别配置
    文件路径。异常：目录发现或应用装配失败时原样抛出。
    """

    selected_root = tmp_path / "domain"
    package_root = selected_root / "demo_domain"
    package_root.mkdir(parents=True, exist_ok=True)
    manifest_path = selected_root / "package.yaml"
    if not manifest_path.exists():
        manifest_path.write_text(
            "package:\n  name: demo_domain\nworkflows: []\n",
            encoding="utf-8",
        )
    plan = discover_editable_sources((selected_root,))
    runtime_store = WorkflowStore(
        tmp_path / "workflow-history.db",
        persist_workflow_definitions=False,
    )
    definition_store = WorkflowStore(":memory:")
    effective_compiler = compiler if compiler is not None else _engine()
    service = WorkflowService(
        runtime_store,
        definition_store=definition_store,
        compiler=effective_compiler,
        source_target=DomainWorkflowSourceTarget.from_discovery_plan(plan),
    )
    service.replace_discovered_source_authorizations(plan)
    if plan.registrations:
        service.activate_registered_sources_to_fixed_point()
    return (
        TestClient(create_workflow_app(service)),
        service,
        runtime_store,
        definition_store,
        package_root / "operation_categories.json",
    )


def _close(
    service: WorkflowService,
    runtime_store: WorkflowStore,
    definition_store: WorkflowStore,
) -> None:
    """关闭一次测试装配创建的服务与存储。

    参数：服务、运行事实库和定义目录来自 ``_client``。返回：无。异常：关闭失败
    时原样抛出，避免资源泄漏被静默忽略。
    """

    service.close()
    runtime_store.close()
    definition_store.close()


def test_default_categories_support_create_rename_delete_and_restart(
    tmp_path: Path,
) -> None:
    """默认类别可增改删，并通过领域包配置跨 OS 重启保留。

    参数：``tmp_path`` 隔离领域包。返回：无。异常：默认值、CRUD 或领域包持久化
    回归时由断言暴露。
    """

    client, service, runtime_store, definition_store, category_file = _client(tmp_path)
    try:
        defaults = client.get("/api/v1/experiment-operation-categories").json()["data"][
            "items"
        ]
        assert [item["name"] for item in defaults] == [
            "设备操作",
            "集成操作",
            "人工操作",
        ]
        assert not category_file.exists()

        created_response = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "数据处理", "sort_order": 25},
        )
        assert created_response.status_code == 201
        created = created_response.json()["data"]
        renamed = client.put(
            f"/api/v1/experiment-operation-categories/{created['uuid']}",
            json={"name": "数据集成", "sort_order": 15},
        )
        assert renamed.json()["data"]["name"] == "数据集成"
        assert category_file.is_file()

        manual = next(item for item in defaults if item["name"] == "人工操作")
        deleted = client.delete(
            f"/api/v1/experiment-operation-categories/{manual['uuid']}"
        )
        assert deleted.json()["code"] == 0
        deleted_legacy_write = client.post(
            "/api/v1/workflows",
            json={
                "name": "已删除类别的旧前端请求",
                "workflow_type": "experiment_operation",
                "tags": ["manual_operation"],
                "meta_data": {},
            },
        )
        assert deleted_legacy_write.status_code == 200
        assert deleted_legacy_write.json()["code"] != 0
        assert client.get("/api/v1/workflows").json()["data"]["items"] == []
    finally:
        _close(service, runtime_store, definition_store)

    reopened, service, runtime_store, definition_store, _ = _client(tmp_path)
    try:
        persisted = reopened.get("/api/v1/experiment-operation-categories").json()[
            "data"
        ]["items"]
        assert [item["name"] for item in persisted] == [
            "设备操作",
            "数据集成",
            "集成操作",
        ]
    finally:
        _close(service, runtime_store, definition_store)


def test_experiment_operation_category_round_trips_and_filters_legacy_tags(
    tmp_path: Path,
) -> None:
    """实验操作保存类别并可筛选，旧类别标签仍映射到默认类别。

    参数：``tmp_path`` 隔离领域包和内存定义。返回：无。异常：类别引用、列表筛选
    或旧标签兼容回归时由断言暴露。
    """

    client, service, runtime_store, definition_store, _ = _client(tmp_path)
    try:
        categories = client.get("/api/v1/experiment-operation-categories").json()[
            "data"
        ]["items"]
        device_category = next(
            item for item in categories if item["name"] == "设备操作"
        )
        explicit = client.post(
            "/api/v1/workflows",
            json={
                "name": "显式设备操作",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": device_category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        )
        legacy = client.post(
            "/api/v1/workflows",
            json={
                "name": "旧标签设备操作",
                "workflow_type": "experiment_operation",
                "tags": ["device_operation"],
                "meta_data": {},
            },
        )
        normal = client.post(
            "/api/v1/workflows",
            json={"name": "普通工作流", "tags": [], "meta_data": {}},
        )

        assert explicit.status_code == 201
        assert (
            explicit.json()["data"]["operation_category_uuid"]
            == (device_category["uuid"])
        )
        assert (
            legacy.json()["data"]["operation_category_uuid"]
            == (device_category["uuid"])
        )
        renamed_category = client.put(
            (f"/api/v1/experiment-operation-categories/{device_category['uuid']}"),
            json={"name": "仪器操作"},
        ).json()["data"]
        assert renamed_category == {
            **device_category,
            "name": "仪器操作",
        }
        assert (
            client.get(f"/api/v1/workflows/{explicit.json()['data']['uuid']}").json()[
                "data"
            ]["operation_category_uuid"]
            == device_category["uuid"]
        )
        filtered = client.get(
            "/api/v1/workflows",
            params={
                "workflow_type": "experiment_operation",
                "operation_category_uuid": device_category["uuid"],
            },
        ).json()["data"]["items"]
        assert {item["uuid"] for item in filtered} == {
            explicit.json()["data"]["uuid"],
            legacy.json()["data"]["uuid"],
        }
        combined = client.get(
            "/api/v1/workflows",
            params={
                "workflow_type": "experiment_operation",
                "status": "source",
                "name": "显式设备",
                "operation_category_uuid": device_category["uuid"],
            },
        ).json()["data"]["items"]
        assert [item["uuid"] for item in combined] == [explicit.json()["data"]["uuid"]]

        cleared_legacy = client.put(
            f"/api/v1/workflows/{legacy.json()['data']['uuid']}",
            json={
                "name": "旧标签设备操作",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": None,
                "tags": ["device_operation"],
                "meta_data": {},
            },
        )
        assert cleared_legacy.json()["data"]["operation_category_uuid"] is None
        renamed_after_clear = client.put(
            f"/api/v1/workflows/{legacy.json()['data']['uuid']}",
            json={
                "name": "旧标签设备操作（改名）",
                "workflow_type": "experiment_operation",
                "tags": ["device_operation"],
                "meta_data": {},
            },
        )
        assert renamed_after_clear.json()["data"]["operation_category_uuid"] is None
        filtered_after_clear = client.get(
            "/api/v1/workflows",
            params={"operation_category_uuid": device_category["uuid"]},
        ).json()["data"]["items"]
        assert [item["uuid"] for item in filtered_after_clear] == [
            explicit.json()["data"]["uuid"]
        ]
        all_items = client.get("/api/v1/workflows").json()["data"]["items"]
        assert normal.json()["data"]["uuid"] in {item["uuid"] for item in all_items}
    finally:
        _close(service, runtime_store, definition_store)


def test_category_rejects_normal_workflow_and_delete_while_referenced(
    tmp_path: Path,
) -> None:
    """普通工作流不能挂类别，被实验操作引用的类别不能静默删除。

    参数：``tmp_path`` 隔离领域包。返回：无。异常：分类边界或引用保护失效时由
    断言暴露。
    """

    client, service, runtime_store, definition_store, _ = _client(tmp_path)
    try:
        created_category = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "自定义操作", "sort_order": 40},
        ).json()["data"]
        invalid_normal = client.post(
            "/api/v1/workflows",
            json={
                "name": "错误分类",
                "workflow_type": "normal",
                "operation_category_uuid": created_category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        )
        assert invalid_normal.json()["code"] == 1000

        child = client.post(
            "/api/v1/workflows",
            json={
                "name": "引用类别",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": created_category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        in_use = client.delete(
            f"/api/v1/experiment-operation-categories/{created_category['uuid']}"
        )
        assert in_use.json()["code"] == 3003

        cleared = client.put(
            f"/api/v1/workflows/{child['uuid']}",
            json={
                "name": child["name"],
                "workflow_type": "experiment_operation",
                "operation_category_uuid": None,
                "tags": [],
                "meta_data": {},
            },
        )
        assert cleared.json()["data"]["operation_category_uuid"] is None
        assert (
            client.delete(
                f"/api/v1/experiment-operation-categories/{created_category['uuid']}"
            ).json()["code"]
            == 0
        )
    finally:
        _close(service, runtime_store, definition_store)


def test_category_survives_python_source_round_trip_and_cold_start(
    tmp_path: Path,
) -> None:
    """实验操作类别随领域 Python 源码保存并在冷启动后恢复。

    参数：``tmp_path`` 隔离领域包。返回：无。异常：AST、源码生成、公共图投影或
    冷启动目录恢复丢失类别时由断言暴露。
    """

    compiler = _engine()
    client, service, runtime_store, definition_store, _ = _client(
        tmp_path,
        compiler=compiler,
    )
    device_category_uuid = client.get("/api/v1/experiment-operation-categories").json()[
        "data"
    ]["items"][0]["uuid"]
    python_source = _source().replace(
        '    description="Prepare and analyze one sample.",',
        (
            '    description="Prepare and analyze one sample.",\n'
            '    workflow_type="experiment_operation",\n'
            '    meta_data={"operation_category_uuid": '
            f'"{device_category_uuid}"}},'
        ),
    )
    try:
        imported = client.post(
            "/api/v1/local/workflows/import-python",
            content=python_source.encode("utf-8"),
            headers={
                "content-type": "text/x-python",
                "X-Workflow-Filename": "categorized_workflow.py",
            },
        )
        assert imported.status_code == 201
        assert (
            imported.json()["data"]["workflow"]["operation_category_uuid"]
            == device_category_uuid
        )
    finally:
        _close(service, runtime_store, definition_store)

    reopened, service, runtime_store, definition_store, _ = _client(
        tmp_path,
        compiler=_engine(),
    )
    try:
        workflow = reopened.get(f"/api/v1/workflows/{WORKFLOW_UUID}").json()["data"]
        graph = reopened.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph").json()["data"]
        assert workflow["operation_category_uuid"] == device_category_uuid
        assert graph["workflow"]["operation_category_uuid"] == (device_category_uuid)
        assert "operation_category_uuid" not in graph["workflow"]["meta_data"]
    finally:
        _close(service, runtime_store, definition_store)


def test_python_and_json_imports_cannot_bypass_category_rules(
    tmp_path: Path,
) -> None:
    """Python 与旧 JSON 导入必须复用公开创建接口的类别约束。

    参数：``tmp_path`` 隔离领域包、源码和内存定义。返回：无。异常：普通工作流
    能挂类别、实验操作能引用不存在类别，或失败后留下部分定义时由断言暴露。
    """

    client, service, runtime_store, definition_store, _ = _client(
        tmp_path,
        compiler=_engine(),
    )
    default_category_uuid = client.get(
        "/api/v1/experiment-operation-categories"
    ).json()["data"]["items"][0]["uuid"]
    normal_with_category = _source().replace(
        '    description="Prepare and analyze one sample.",',
        (
            '    description="Prepare and analyze one sample.",\n'
            '    meta_data={"operation_category_uuid": '
            f'"{default_category_uuid}"}},'
        ),
    )
    try:
        python_response = client.post(
            "/api/v1/local/workflows/import-python",
            content=normal_with_category.encode("utf-8"),
            headers={
                "content-type": "text/x-python",
                "X-Workflow-Filename": "invalid_normal_category.py",
            },
        )
        assert python_response.status_code == 200
        assert python_response.json()["code"] != 0

        json_response = client.post(
            "/api/v1/workflows/import",
            json={
                "name": "错误类别实验操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "meta_data": {
                    # 固定身份代表类别目录中明确不存在的类别，用于证明旧 JSON
                    # 导入不会绕过类别存在性校验。
                    "operation_category_uuid": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
                },
                "nodes": [
                    {
                        # 该固定身份只代表本次非法导入携带的人工确认节点；它与
                        # 上面的缺失类别 UUID 分离，避免身份角色含混。
                        "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "name": "人工确认",
                        "type": "manual_confirm",
                        "pose": {},
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
        assert json_response.status_code == 200
        assert json_response.json()["code"] != 0
        assert client.get("/api/v1/workflows").json()["data"]["items"] == []
    finally:
        _close(service, runtime_store, definition_store)
