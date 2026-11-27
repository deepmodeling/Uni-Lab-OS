"""工作流类型公开 HTTP 合同回归。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _client(tmp_path) -> tuple[TestClient, WorkflowStore]:
    """创建隔离的工作流应用。

    参数：``tmp_path`` 是 pytest 提供的临时目录。返回：公共 HTTP 客户端和需由
    调用方关闭的存储。异常：应用装配失败时原样抛出。
    """

    store = WorkflowStore(tmp_path / "workflow-history.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), store


def test_workflow_create_defaults_type_and_list_filters_without_breaking_all(
    tmp_path,
) -> None:
    """创建两类工作流，描述可读取且实验操作可按类型和名称模糊筛选。

    参数：``tmp_path`` 隔离工作流目录。返回：无。异常：默认值、显式类型或列表
    兼容合同回归时由断言暴露。
    """

    client, store = _client(tmp_path)
    try:
        normal = client.post(
            "/api/v1/workflows",
            json={
                "name": "普通流程",
                "description": "普通工作流说明",
                "tags": [],
                "meta_data": {},
            },
        )
        operation = client.post(
            "/api/v1/workflows",
            json={
                "name": "实验操作",
                "description": "实验操作说明",
                "workflow_type": "experiment_operation",
                "tags": ["device_operation"],
                "meta_data": {},
            },
        )

        assert normal.status_code == 201
        assert normal.json()["data"]["workflow_type"] == "normal"
        assert normal.json()["data"]["description"] == "普通工作流说明"
        assert operation.status_code == 201
        assert operation.json()["data"]["workflow_type"] == ("experiment_operation")
        assert operation.json()["data"]["description"] == "实验操作说明"

        all_items = client.get("/api/v1/workflows").json()["data"]["items"]
        operation_items = client.get(
            "/api/v1/workflows",
            params={"workflow_type": "experiment_operation"},
        ).json()["data"]["items"]
        named_items = client.get(
            "/api/v1/workflows",
            params={"name": "实验"},
        ).json()["data"]["items"]
        assert {item["uuid"] for item in all_items} == {
            normal.json()["data"]["uuid"],
            operation.json()["data"]["uuid"],
        }
        assert [item["uuid"] for item in operation_items] == [
            operation.json()["data"]["uuid"]
        ]
        assert [item["uuid"] for item in named_items] == [
            operation.json()["data"]["uuid"]
        ]
        legacy_name = client.post(
            "/api/v1/workflows",
            json={
                "name": "旧业务名称",
                "workflow_type": "subworkflow",
                "tags": [],
                "meta_data": {},
            },
        )
        assert legacy_name.json()["code"] != 0
    finally:
        store.close()


def test_workflow_list_combines_type_and_current_publication_status(tmp_path) -> None:
    """列表可组合类型与当前发布状态筛选，并按筛选结果计算分页。

    参数：``tmp_path`` 隔离工作流与发布合同。返回：无。异常：状态含义、组合筛选
    或 ``has_more`` 按未筛选集合计算时由断言暴露。
    """

    client, store = _client(tmp_path)
    try:
        source_operation = client.post(
            "/api/v1/workflows",
            json={
                "name": "未发布实验操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        published_operation = client.post(
            "/api/v1/workflows",
            json={
                "name": "已发布实验操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        client.post(
            "/api/v1/workflows",
            json={"name": "普通流程", "tags": [], "meta_data": {}},
        )
        graph = client.put(
            f"/api/v1/workflows/{published_operation['uuid']}/graph",
            json={
                "revision": published_operation["revision"],
                "nodes": [
                    {
                        # 固定身份仅代表本用例中的人工确认节点，保证发布前后
                        # 能精确断言同一节点，不与工作流或类别身份混用。
                        "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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
        ).json()["data"]
        published = client.post(
            f"/api/v1/workflows/{published_operation['uuid']}/publications",
            json={"revision": graph["workflow"]["revision"]},
        )
        assert published.status_code == 201

        response = client.get(
            "/api/v1/workflows",
            params={
                "workflow_type": "experiment_operation",
                "status": "source",
                "page": 1,
                "page_size": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert [item["uuid"] for item in data["items"]] == [source_operation["uuid"]]
        assert data["has_more"] is False

        published_items = client.get(
            "/api/v1/workflows",
            params={
                "workflow_type": "experiment_operation",
                "status": "published",
            },
        ).json()["data"]["items"]
        assert [item["uuid"] for item in published_items] == [
            published_operation["uuid"]
        ]
    finally:
        store.close()


def test_workflow_update_preserves_type_and_rejects_cross_directory_change(
    tmp_path,
) -> None:
    """旧更新请求保持类型，显式跨目录类型转换会关闭式失败。

    参数：``tmp_path`` 隔离工作流目录。返回：无。异常：旧版 PUT 把实验操作重置
    为普通流程，或类型转换修改了定义时由断言暴露。
    """

    client, store = _client(tmp_path)
    try:
        created = client.post(
            "/api/v1/workflows",
            json={
                "name": "可复用操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        preserved = client.put(
            f"/api/v1/workflows/{created['uuid']}",
            json={
                "name": "可复用操作（改名）",
                "tags": ["manual_operation"],
                "meta_data": {},
            },
        )
        changed = client.put(
            f"/api/v1/workflows/{created['uuid']}",
            json={
                "name": "普通流程",
                "workflow_type": "normal",
                "tags": [],
                "meta_data": {},
            },
        )

        assert preserved.status_code == 200
        assert preserved.json()["data"]["workflow_type"] == ("experiment_operation")
        assert changed.status_code == 200
        assert changed.json()["code"] != 0
        current = client.get(f"/api/v1/workflows/{created['uuid']}").json()["data"]
        assert current["workflow_type"] == "experiment_operation"
    finally:
        store.close()


def test_existing_workflow_catalog_defaults_legacy_rows_to_normal(tmp_path) -> None:
    """旧目录升级后保留工作流，并把缺少类型的历史行归为普通工作流。

    参数：``tmp_path`` 隔离升级前数据库。返回：无。异常：初始化未补列、历史行
    丢失或默认类型不兼容时由断言暴露。
    """

    database_path = tmp_path / "legacy-workflow.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE workflow (
                uuid TEXT PRIMARY KEY,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                deleted_at TEXT,
                description TEXT,
                meta_data TEXT NOT NULL,
                name TEXT NOT NULL,
                tags TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO workflow(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, name, tags, revision
            ) VALUES (
                'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                '2026-08-31T00:00:00+00:00',
                '2026-08-31T00:00:00+00:00',
                NULL, NULL, '{}', '旧工作流', '[]', 1
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = WorkflowStore(database_path)
    try:
        workflow = store.get_workflow("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        assert workflow["name"] == "旧工作流"
        assert workflow["workflow_type"] == "normal"
    finally:
        store.close()
