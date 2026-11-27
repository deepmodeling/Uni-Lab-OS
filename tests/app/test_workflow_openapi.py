"""工作流与实验操作 Swagger/OpenAPI 公共合同回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowService, WorkflowStore]:
    """建立只通过公开 HTTP 文档端点读取合同的隔离应用。

    参数：``tmp_path`` 是 pytest 临时目录。返回：FastAPI 公共客户端、工作流
    服务和待关闭存储。异常：应用或存储装配失败时原样抛出；不调用私有 OpenAPI
    生成函数，确保测试覆盖前端和客户端生成器实际读取的接缝。
    """

    store = WorkflowStore(tmp_path / "workflow-history.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), service, store


def _parameter(
    operation: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    """按 wire 名称取得一个公开路径或查询参数。

    参数：``operation`` 是 `/openapi.json` 中的单个 operation；``name`` 是公开
    参数名。返回：对应参数定义。异常：缺少或重复参数时由断言关闭式失败。
    """

    matches = [item for item in operation["parameters"] if item["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _response_schema(
    operation: dict[str, Any],
    status: int,
) -> dict[str, Any]:
    """取得指定 HTTP 状态的 JSON 响应 schema。

    参数：``operation`` 是公开 OpenAPI operation；``status`` 是 HTTP 状态码。
    返回：`application/json` schema。异常：文档遗漏状态或 JSON 内容时由键访问
    直接暴露，避免把空响应误判为已有合同。
    """

    return operation["responses"][str(status)]["content"]["application/json"][
        "schema"
    ]


def _schema_contains_ref(schema: dict[str, Any], component_name: str) -> bool:
    """判断响应 schema 是否直接或通过联合类型引用指定组件。

    参数：``schema`` 是公开响应 schema；``component_name`` 是组件类名。返回：
    直接 `$ref` 或 `anyOf` 中存在该组件时为真。异常：无；未知形状返回假。
    """

    expected_suffix = f"/{component_name}"
    if str(schema.get("$ref") or "").endswith(expected_suffix):
        return True
    return any(
        str(candidate.get("$ref") or "").endswith(expected_suffix)
        for candidate in schema.get("anyOf", [])
        if isinstance(candidate, dict)
    )


def test_workflow_swagger_marks_required_fields_and_real_responses(
    tmp_path: Path,
) -> None:
    """工作流 Swagger 须准确说明必填项、筛选参数和成功/错误返回。

    参数：``tmp_path`` 隔离运行事实。返回：无。异常：字段必填性、默认值、说明、
    路径参数、HTTP 状态或 Backend 响应包络与真实接口不一致时由断言暴露。
    """

    client, service, store = _client(tmp_path)
    try:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "Swagger UI" in docs.text

        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        create_schema = schema["components"]["schemas"]["WorkflowCreateRequest"]
        assert create_schema["required"] == ["name"]
        assert create_schema["properties"]["workflow_type"]["default"] == "normal"
        assert create_schema["properties"]["workflow_type"]["description"]
        assert create_schema["properties"]["operation_category_uuid"]["description"]
        assert create_schema["properties"]["name"]["examples"] == ["样品前处理"]

        create_operation = schema["paths"]["/api/v1/workflows"]["post"]
        assert create_operation["summary"] == "创建工作流或实验操作"
        assert _response_schema(create_operation, 201)["$ref"].endswith(
            "/WorkflowSuccessResponse"
        )
        assert _response_schema(create_operation, 200)["$ref"].endswith(
            "/BackendErrorResponse"
        )

        list_operation = schema["paths"]["/api/v1/workflows"]["get"]
        for parameter_name in (
            "page",
            "page_size",
            "keyword",
            "name",
            "workflow_type",
            "status",
            "operation_category_uuid",
        ):
            parameter = _parameter(list_operation, parameter_name)
            assert parameter["required"] is False
            assert parameter["description"]
        assert _parameter(list_operation, "page_size")["schema"]["default"] == 20
        assert _schema_contains_ref(
            _response_schema(list_operation, 200),
            "WorkflowListSuccessResponse",
        )
        # 历史合同查询仍是运行时兼容入口，不在 Swagger 中制造“发布记录”业务概念；
        # 发布动作本身必须公开，普通工作流与实验操作共用 published 状态。
        assert "/api/v1/published-workflow-contracts" not in schema["paths"]
        publish_path = "/api/v1/workflows/{workflow_uuid}/publications"
        publish_operation = schema["paths"][publish_path]["post"]
        assert publish_operation["summary"] == "发布工作流"
        assert _parameter(publish_operation, "workflow_uuid")["required"] is True
        request_schema = publish_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert request_schema["$ref"].endswith("/PublishWorkflowContractRequest")
        assert _response_schema(publish_operation, 201)["$ref"].endswith(
            "/WorkflowPublishSuccessResponse"
        )

        get_operation = schema["paths"]["/api/v1/workflows/{workflow_uuid}"][
            "get"
        ]
        workflow_uuid = _parameter(get_operation, "workflow_uuid")
        assert workflow_uuid["in"] == "path"
        assert workflow_uuid["required"] is True
        assert workflow_uuid["description"]
    finally:
        service.close()
        store.close()


def test_experiment_operation_category_swagger_documents_crud_contract(
    tmp_path: Path,
) -> None:
    """实验操作类别 CRUD 须展示真实必填项、限制与返回包络。

    参数：``tmp_path`` 隔离应用。返回：无。异常：类别创建/更新 DTO、路径身份或
    200/201/空成功响应没有进入公开 OpenAPI 时由断言暴露。
    """

    client, service, store = _client(tmp_path)
    try:
        schema = client.get("/openapi.json").json()
        components = schema["components"]["schemas"]
        create_schema = components["OperationCategoryCreateRequest"]
        update_schema = components["OperationCategoryUpdateRequest"]
        assert create_schema["required"] == ["name"]
        assert "required" not in update_schema
        assert create_schema["properties"]["name"]["description"]
        assert create_schema["properties"]["sort_order"]["default"] == 100
        assert create_schema["properties"]["sort_order"]["description"]

        category_path = "/api/v1/experiment-operation-categories"
        create_operation = schema["paths"][category_path]["post"]
        assert create_operation["summary"] == "新增实验操作类别"
        assert _response_schema(create_operation, 201)["$ref"].endswith(
            "/OperationCategorySuccessResponse"
        )
        list_operation = schema["paths"][category_path]["get"]
        assert _schema_contains_ref(
            _response_schema(list_operation, 200),
            "OperationCategoryListSuccessResponse",
        )

        item_path = f"{category_path}/{{category_uuid}}"
        get_operation = schema["paths"][item_path]["get"]
        category_uuid = _parameter(get_operation, "category_uuid")
        assert category_uuid["required"] is True
        assert category_uuid["description"]
        delete_operation = schema["paths"][item_path]["delete"]
        assert _schema_contains_ref(
            _response_schema(delete_operation, 200),
            "BackendEmptySuccessResponse",
        )
    finally:
        service.close()
        store.close()
