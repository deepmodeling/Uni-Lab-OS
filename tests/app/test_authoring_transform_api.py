"""可信工作流创作纯转换 HTTP 合同的公共接缝测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_authoring_transform import (
    create_authoring_transform_app,
    create_authoring_transform_router,
)
from unilabos.workflow.models import CandidateCompilation

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
OTHER_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000002"
SOURCE_URI = "package://lab/workflows/f05.py"
FINGERPRINT = "sha256:" + "7" * 64
HTTP_BODY_LIMIT = 8 * 1024 * 1024
INTEGER_DIGIT_LIMIT = 4096
JSON_DEPTH_LIMIT = 10_000

INVALID_INPUT = {"code": 1000, "error": {"msg": "提交内容格式不正确"}}
CATALOG_UNAVAILABLE = {
    "code": 5001,
    "error": {"msg": "设备动作模板暂不可用，请稍后重试"},
}
INTERNAL_ERROR = {
    "code": 1,
    "error": {"msg": "本地工作流服务出现错误，请重试或查看日志"},
}


def _graph() -> dict[str, Any]:
    """构造属于固定工作流（Workflow）身份和修订的空五集合图。

    参数：无。返回：可作为纯转换请求和基线的独立工作流图；调用方可安全修改。
    """

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "name": "F05 trusted authoring",
            "tags": [],
            "description": None,
            "meta_data": {},
            "revision": 7,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _source_only_changeset() -> dict[str, Any]:
    """返回图生命周期集合全部为空的源码变更集（Source-only ChangeSet）。

    参数：无。返回：生成 Python 与共同校验成功时唯一允许的空变更集。
    """

    return {
        "kind": "source_only",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


class RecordingTransformEngine:
    """记录纯转换调用，并可注入非法出站结果验证适配器关闭失败。"""

    def __init__(
        self,
        mode: Literal[
            "valid",
            "diagnostic",
            "catalog-unavailable",
            "internal-error",
            "private-diagnostic",
            "private-graph",
            "changed-graph",
            "nonempty-source-only",
            "foreign-identity",
        ] = "valid",
    ) -> None:
        """创建指定行为的引擎替身。

        参数：``mode`` 决定成功、诊断或不可信内部输出。返回：无；调用记录初始为空。
        """

        self.mode = mode
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _result(
        self,
        operation: str,
        values: dict[str, Any],
    ) -> CandidateCompilation:
        """记录一次操作并生成与该请求对应的候选编译结果。

        参数：``operation`` 是三个公开操作之一；``values`` 是适配器传入的闭合参数。
        返回：成功或诊断结果；``internal-error`` 模式抛出含秘密文本的异常。
        """

        self.calls.append((operation, values))
        if self.mode == "internal-error":
            raise RuntimeError("secret database password")
        if self.mode in {"diagnostic", "catalog-unavailable", "private-diagnostic"}:
            # ``diagnostic_code`` 是稳定公开诊断身份；目录不可用稍后由适配器提升为业务错误。
            diagnostic_code = (
                "template_catalog_unavailable"
                if self.mode == "catalog-unavailable"
                else "python_syntax_error"
            )
            result = CandidateCompilation(
                diagnostics=[
                    {
                        "severity": "error",
                        "code": diagnostic_code,
                        "message": "稳定编译诊断",
                    }
                ],
                graph=None,
                normalized_python_source=None,
                source_map=[],
                changeset=None,
                compiler_version="f05-c7-spy/v1",
                template_catalog_fingerprint=FINGERPRINT,
            )
            if self.mode == "private-diagnostic":
                result.diagnostics[0]["private_detail"] = "must not leak"
            return result

        # ``request_graph`` 是调用方提供的图快照；引擎返回前必须复制，避免污染请求证据。
        request_graph = values.get("applied_graph", values.get("graph"))
        result_graph = deepcopy(request_graph)
        if self.mode == "private-graph":
            result_graph["canonical_ir"] = "must not leak"
        elif self.mode == "changed-graph":
            result_graph["workflow"]["name"] = "unexpected mutation"
        elif self.mode == "foreign-identity":
            result_graph["workflow"]["uuid"] = OTHER_WORKFLOW_UUID
        changeset = _source_only_changeset()
        if self.mode == "nonempty-source-only":
            changeset["created_node_uuids"] = ["20000000-0000-4000-8000-000000000001"]
        normalized_source = values.get("python_source") or "# generated\n"
        return CandidateCompilation(
            diagnostics=[],
            graph=result_graph,
            normalized_python_source=normalized_source,
            source_map=[],
            changeset=changeset,
            compiler_version="f05-c7-spy/v1",
            template_catalog_fingerprint=FINGERPRINT,
        )

    def compile(self, **values: Any) -> CandidateCompilation:
        """执行源码编译替身；参数是闭合编译 DTO，返回记录后的候选结果。"""

        return self._result("compile", values)

    def generate_python(self, **values: Any) -> CandidateCompilation:
        """执行图到 Python 替身；参数是闭合生成 DTO，返回记录后的候选结果。"""

        return self._result("generate_python", values)

    def validate(self, **values: Any) -> CandidateCompilation:
        """执行共同校验替身；参数是闭合校验 DTO，返回记录后的候选结果。"""

        return self._result("validate", values)


def _compile_body(**overrides: Any) -> dict[str, Any]:
    """构造规范编译请求并按调用方字段覆盖。

    参数：``overrides`` 用于生成单一边界反例。返回：新的请求对象。
    """

    body = {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": 7,
        "python_source": "value = 1\n",
        "source_uri": SOURCE_URI,
        "applied_graph": _graph(),
    }
    body.update(overrides)
    return body


def _generate_body(**overrides: Any) -> dict[str, Any]:
    """构造规范 Python 生成请求并按调用方字段覆盖。

    参数：``overrides`` 用于生成单一边界反例。返回：新的请求对象。
    """

    body = {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": 7,
        "graph": _graph(),
        "source_uri": SOURCE_URI,
    }
    body.update(overrides)
    return body


def _validate_body(**overrides: Any) -> dict[str, Any]:
    """构造规范共同校验请求并按调用方字段覆盖。

    参数：``overrides`` 用于生成单一边界反例。返回：新的请求对象。
    """

    body = {**_generate_body(), "python_source": "value = 1\n"}
    body.update(overrides)
    return body


def _assert_success(payload: dict[str, Any]) -> dict[str, Any]:
    """断言产品成功封装与纯转换数据都是闭合结构。

    参数：``payload`` 是 HTTP JSON 响应。返回：闭合 ``data``，供用例继续断言。
    """

    assert set(payload) == {"code", "data"}
    assert payload["code"] == 0
    data = payload["data"]
    assert set(data) == {
        "diagnostics",
        "graph",
        "normalized_python_source",
        "source_map",
        "changeset",
        "compiler_version",
        "template_catalog_fingerprint",
    }
    assert "candidate_hash" not in data
    assert "draft_hash" not in data
    assert "canonical_ir" not in data
    return data


@pytest.mark.parametrize(
    ("path", "body", "operation", "expected_keys"),
    [
        (
            "/api/v1/authoring/compile",
            _compile_body(),
            "compile",
            {
                "workflow_uuid",
                "workflow_revision",
                "python_source",
                "source_uri",
                "applied_graph",
            },
        ),
        (
            "/api/v1/authoring/generate-python",
            _generate_body(),
            "generate_python",
            {"workflow_uuid", "workflow_revision", "graph", "source_uri"},
        ),
        (
            "/api/v1/authoring/validate",
            _validate_body(),
            "validate",
            {
                "workflow_uuid",
                "workflow_revision",
                "graph",
                "python_source",
                "source_uri",
            },
        ),
    ],
    ids=["compile", "generate-python", "validate"],
)
def test_three_routes_are_closed_and_call_the_engine_once(
    path: str,
    body: dict[str, Any],
    operation: str,
    expected_keys: set[str],
) -> None:
    """三个纯转换路由必须仅传闭合参数并恰好调用一次可信创作引擎。

    参数：路径、请求、预期操作及键集由参数矩阵提供。返回：无；断言 HTTP 200。
    """

    engine = RecordingTransformEngine()
    with TestClient(create_authoring_transform_app(engine)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 200
    _assert_success(response.json())
    assert len(engine.calls) == 1
    called_operation, values = engine.calls[0]
    assert called_operation == operation
    assert set(values) == expected_keys
    assert values["workflow_uuid"] == WORKFLOW_UUID
    assert values["workflow_revision"] == 7


def test_router_contains_only_the_three_pure_routes() -> None:
    """独立纯转换路由不得夹带草稿保存或候选应用写操作。

    参数：无。返回：无；断言路由集合严格等于三个 POST 接口。
    """

    router = create_authoring_transform_router(RecordingTransformEngine())
    routes = {
        (route.path, frozenset(route.methods or set())) for route in router.routes
    }
    assert routes == {
        ("/api/v1/authoring/compile", frozenset({"POST"})),
        ("/api/v1/authoring/generate-python", frozenset({"POST"})),
        ("/api/v1/authoring/validate", frozenset({"POST"})),
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/authoring/compile", _compile_body(workflow_id=WORKFLOW_UUID)),
        ("/api/v1/authoring/compile", _compile_body(base_revision_id=7)),
        ("/api/v1/authoring/compile", _compile_body(canonical_ir={})),
        ("/api/v1/authoring/compile", _compile_body(revision=True)),
        ("/api/v1/authoring/compile", _compile_body(revision=0)),
        (
            "/api/v1/authoring/compile",
            _compile_body(workflow_uuid="00000000-0000-0000-0000-000000000000"),
        ),
        ("/api/v1/authoring/compile", _compile_body(source_uri="\t ")),
        ("/api/v1/authoring/compile", _compile_body(applied_graph=[])),
        (
            "/api/v1/authoring/compile",
            _compile_body(
                applied_graph={
                    **_graph(),
                    "workflow": {**_graph()["workflow"], "revision": 8},
                }
            ),
        ),
        (
            "/api/v1/authoring/generate-python",
            _generate_body(
                graph={
                    **_graph(),
                    "workflow": {**_graph()["workflow"], "uuid": OTHER_WORKFLOW_UUID},
                }
            ),
        ),
    ],
    ids=[
        "workflow-id-alias",
        "base-revision-alias",
        "canonical-ir-alias",
        "boolean-revision",
        "zero-revision",
        "nil-workflow-uuid",
        "blank-source-uri",
        "non-object-graph",
        "graph-revision-mismatch",
        "graph-workflow-mismatch",
    ],
)
def test_noncanonical_request_fails_before_engine(
    path: str,
    body: dict[str, Any],
) -> None:
    """未知字段、旧别名、非法身份和图身份分叉必须在引擎前关闭失败。

    参数：``path`` 与 ``body`` 描述单一非法请求。返回：无；断言业务码 1000。
    """

    engine = RecordingTransformEngine()
    with TestClient(create_authoring_transform_app(engine)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 200
    assert response.json() == INVALID_INPUT
    assert engine.calls == []


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("catalog-unavailable", CATALOG_UNAVAILABLE),
        ("internal-error", INTERNAL_ERROR),
        ("private-diagnostic", INTERNAL_ERROR),
        ("private-graph", INTERNAL_ERROR),
        ("foreign-identity", INTERNAL_ERROR),
    ],
)
def test_unavailable_or_illegal_engine_result_is_a_sanitized_business_error(
    mode: Any,
    expected: dict[str, Any],
) -> None:
    """目录不可用及内部非法结果必须返回产品业务错误且不泄漏内部字段。

    参数：``mode`` 注入失败类型，``expected`` 是稳定封装。返回：无；断言一次调用。
    """

    engine = RecordingTransformEngine(mode)
    with TestClient(create_authoring_transform_app(engine)) as client:
        response = client.post("/api/v1/authoring/compile", json=_compile_body())

    assert response.status_code == 200
    assert response.json() == expected
    assert "secret" not in response.text
    assert "private" not in response.text
    assert len(engine.calls) == 1


def test_well_formed_diagnostic_remains_success_data() -> None:
    """确定性编译诊断必须作为 HTTP 200 成功数据返回，而非业务异常。

    参数：无。返回：无；断言诊断结构闭合且没有候选图。
    """

    engine = RecordingTransformEngine("diagnostic")
    with TestClient(create_authoring_transform_app(engine)) as client:
        response = client.post("/api/v1/authoring/compile", json=_compile_body())

    data = _assert_success(response.json())
    assert data["diagnostics"] == [
        {
            "severity": "error",
            "code": "python_syntax_error",
            "message": "稳定编译诊断",
        }
    ]
    assert data["graph"] is None
    assert data["changeset"] is None


@pytest.mark.parametrize(
    ("path", "body", "mode"),
    [
        ("/api/v1/authoring/generate-python", _generate_body(), "changed-graph"),
        ("/api/v1/authoring/validate", _validate_body(), "changed-graph"),
        (
            "/api/v1/authoring/generate-python",
            _generate_body(),
            "nonempty-source-only",
        ),
        ("/api/v1/authoring/validate", _validate_body(), "nonempty-source-only"),
    ],
)
def test_generate_and_validate_require_exact_graph_and_empty_source_only_changeset(
    path: str,
    body: dict[str, Any],
    mode: Any,
) -> None:
    """生成与校验不得改变输入图，也不得宣称任何图生命周期变化。

    参数：路径、请求和非法模式由矩阵提供。返回：无；断言净化后的内部业务错误。
    """

    engine = RecordingTransformEngine(mode)
    with TestClient(create_authoring_transform_app(engine)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 200
    assert response.json() == INTERNAL_ERROR
    assert len(engine.calls) == 1
