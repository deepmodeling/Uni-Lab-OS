"""可信工作流创作转换（Trusted Authoring Transform）的纯 HTTP 适配层。"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Protocol, Self

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from unilabos.app.workflow_api import (
    BackendJSONRoute,
    workflow_error_response,
    workflow_success_response,
)
from unilabos.workflow.candidate_validation import validate_candidate_bundle
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    validate_json_value,
    validate_uuid,
)
from unilabos.workflow.service import WorkflowError
from unilabos.workflow.source_coordinates import require_utf8_text, source_ranges_fit

_LOGGER = logging.getLogger(__name__)
_INT64_MAX = (1 << 63) - 1
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKFLOW_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "revision",
    "description",
}
_NODE_FIELDS = set(WorkflowNodeWrite.model_fields) | {
    "workflow_uuid",
    "create_time",
    "update_time",
}
_EDGE_FIELDS = set(WorkflowEdgeWrite.model_fields) | {"create_time", "update_time"}
_NODE_TEMPLATE_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_HANDLE_TEMPLATE_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
    "description",
    "data_source",
    "data_key",
}


class _StrictRequest(BaseModel):
    """拒绝未知字段的纯转换请求基类。"""

    model_config = ConfigDict(extra="forbid")


class _AuthoringTransformRequest(_StrictRequest):
    """三个可信创作转换（Trusted Authoring Transform）的共同身份合同。"""

    workflow_uuid: str = Field(strict=True)
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    source_uri: str = Field(strict=True)

    @field_validator("workflow_uuid")
    @classmethod
    def _valid_workflow_uuid(cls, value: str) -> str:
        """规范工作流（Workflow）身份；参数是原始 UUID，返回非 nil 规范值。"""

        return validate_uuid(value)

    @field_validator("source_uri")
    @classmethod
    def _valid_source_uri(cls, value: str) -> str:
        """校验源码 URI；参数是外部文本，返回有效且非空的 UTF-8 原值。"""

        require_utf8_text(value)
        if not value.strip():
            raise ValueError("源码 URI 不能为空")
        return value

    @field_validator("python_source", check_fields=False)
    @classmethod
    def _valid_python_source(cls, value: str) -> str:
        """校验工作流源码（Workflow Source）；参数是源码，返回有效 UTF-8 原值。"""

        return require_utf8_text(value)

    def _validate_graph_identity(self, graph: dict[str, Any]) -> None:
        """校验图身份；参数是请求图，返回无，身份或修订分叉时抛出值错误。"""

        validate_json_value(graph)
        workflow = graph.get("workflow")
        if not isinstance(workflow, Mapping):
            raise TypeError("工作流图缺少工作流身份")
        if (
            validate_uuid(workflow.get("uuid")) != self.workflow_uuid
            or workflow.get("revision") != self.revision
        ):
            raise ValueError("工作流图身份或修订不一致")


class AuthoringCompileRequest(_AuthoringTransformRequest):
    """工作流源码（Workflow Source）到候选图的闭合编译请求。"""

    python_source: str = Field(strict=True)
    applied_graph: dict[str, Any]

    @model_validator(mode="after")
    def _matching_applied_graph(self) -> Self:
        """校验已应用图；参数隐含于模型，返回自身，分叉时拒绝请求。"""

        self._validate_graph_identity(self.applied_graph)
        return self


class AuthoringGeneratePythonRequest(_AuthoringTransformRequest):
    """候选图到规范 Python 的闭合生成请求。"""

    graph: dict[str, Any]

    @model_validator(mode="after")
    def _matching_graph(self) -> Self:
        """校验候选图；参数隐含于模型，返回自身，身份分叉时拒绝请求。"""

        self._validate_graph_identity(self.graph)
        return self


class AuthoringValidateRequest(AuthoringGeneratePythonRequest):
    """候选图与工作流源码（Workflow Source）的共同校验请求。"""

    python_source: str = Field(strict=True)


class AuthoringTransform(Protocol):
    """可信工作流创作转换（Trusted Authoring Transform）的最小只读端口。"""

    def compile(self, **values: Any) -> CandidateCompilation:
        """编译源码；参数是闭合请求字段，返回候选编译结果。"""

    def generate_python(self, **values: Any) -> CandidateCompilation:
        """生成规范源码；参数是闭合请求字段，返回候选编译结果。"""

    def validate(self, **values: Any) -> CandidateCompilation:
        """共同校验图与源码；参数是闭合请求字段，返回候选编译结果。"""


def _assert_entity_fields(
    values: Any,
    *,
    allowed: set[str],
    collection_name: str,
) -> None:
    """关闭候选实体字段集合。

    参数：``values`` 是图内实体数组，``allowed`` 是公开字段，``collection_name``
    仅供日志定位。返回：无；非对象或私有字段抛出值错误。
    """

    if not isinstance(values, list):
        raise TypeError(f"{collection_name} 不是数组")
    for entity in values:
        if not isinstance(entity, Mapping) or not set(entity) <= allowed:
            raise ValueError(f"{collection_name} 包含非公开字段")


def _assert_closed_graph(graph: dict[str, Any]) -> None:
    """确认候选图（Candidate Graph）的每层公共实体都不泄漏私有字段。

    参数：``graph`` 已通过公共候选校验。返回：无；未知字段抛出值错误。
    """

    workflow = graph.get("workflow")
    if not isinstance(workflow, Mapping) or not set(workflow) <= _WORKFLOW_FIELDS:
        raise ValueError("工作流投影包含非公开字段")
    _assert_entity_fields(
        graph.get("nodes"), allowed=_NODE_FIELDS, collection_name="nodes"
    )
    _assert_entity_fields(
        graph.get("edges"), allowed=_EDGE_FIELDS, collection_name="edges"
    )
    _assert_entity_fields(
        graph.get("node_templates"),
        allowed=_NODE_TEMPLATE_FIELDS,
        collection_name="node_templates",
    )
    _assert_entity_fields(
        graph.get("handle_templates"),
        allowed=_HANDLE_TEMPLATE_FIELDS,
        collection_name="handle_templates",
    )


def _closed_transform_data(
    result: Any,
    *,
    input_source: str | None,
    workflow_uuid: str,
    revision: int,
    base_graph: dict[str, Any],
    require_unchanged_graph: bool,
) -> dict[str, Any]:
    """把内部引擎结果收紧为唯一公开转换 DTO。

    参数：``result`` 是不可信引擎返回值；源码、身份、修订和基线图限定可接受
    结果；``require_unchanged_graph`` 用于生成与共同校验。返回闭合字典，任何越界
    字段、范围、图或变更集抛出异常且不会泄漏到 HTTP。
    """

    compilation = CandidateCompilation.model_validate(result)
    if not isinstance(compilation.diagnostics, list):
        raise TypeError("诊断必须是数组")
    diagnostics = [
        CandidateDiagnostic.model_validate(item).model_dump(exclude_none=True)
        for item in compilation.diagnostics
    ]
    diagnostic_ranges = [
        item["source_range"] for item in diagnostics if "source_range" in item
    ]
    if diagnostic_ranges and (
        input_source is None or not source_ranges_fit(input_source, diagnostic_ranges)
    ):
        raise ValueError("诊断源码范围越界")

    if not isinstance(compilation.source_map, list):
        raise TypeError("源码映射必须是数组")
    source_map = [
        CandidateSourceMapEntry.model_validate(item).model_dump()
        for item in compilation.source_map
    ]
    graph = compilation.graph
    normalized_source = compilation.normalized_python_source
    changeset: dict[str, Any] | None = None
    has_error = any(item["severity"].strip().lower() == "error" for item in diagnostics)

    if graph is None:
        if (
            not diagnostics
            or not has_error
            or normalized_source is not None
            or source_map
            or compilation.changeset is not None
        ):
            raise ValueError("失败转换结果不闭合")
    else:
        if has_error or not isinstance(normalized_source, str):
            raise ValueError("成功转换结果不闭合")
        require_utf8_text(normalized_source)
        if not source_ranges_fit(normalized_source, source_map):
            raise ValueError("源码映射超出规范源码")
        changeset = CandidateChangeset.model_validate(
            compilation.changeset
        ).model_dump()
        graph = validate_candidate_bundle(
            graph=graph,
            base_graph=base_graph,
            workflow_uuid=workflow_uuid,
            revision=revision,
            source_map=source_map,
            changeset=changeset,
            require_unchanged_graph=require_unchanged_graph,
        )
        _assert_closed_graph(graph)
        if require_unchanged_graph and not strict_json_equal(graph, base_graph):
            raise ValueError("纯源码转换改变了输入图")

    compiler_version = compilation.compiler_version
    fingerprint = compilation.template_catalog_fingerprint
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise ValueError("编译器版本不能为空")
    require_utf8_text(compiler_version)
    if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("目录指纹无效")
    return {
        "diagnostics": diagnostics,
        "graph": graph,
        "normalized_python_source": normalized_source,
        "source_map": source_map,
        "changeset": changeset,
        "compiler_version": compiler_version,
        "template_catalog_fingerprint": fingerprint,
    }


def _transform_response(
    engine: AuthoringTransform,
    operation_name: str,
    values: dict[str, Any],
    *,
    input_source: str | None,
    workflow_uuid: str,
    revision: int,
    base_graph: dict[str, Any],
    require_unchanged_graph: bool = False,
) -> JSONResponse:
    """执行一次转换并生成净化响应。

    参数：``engine`` 是只读转换端口，``operation_name`` 与 ``values`` 指定唯一
    调用；其余参数约束出站结果。返回 Backend 形状 HTTP 200 响应；内部异常记录
    后映射为稳定业务错误。
    """

    try:
        operation = getattr(engine, operation_name)
        data = _closed_transform_data(
            operation(**values),
            input_source=input_source,
            workflow_uuid=workflow_uuid,
            revision=revision,
            base_graph=base_graph,
            require_unchanged_graph=require_unchanged_graph,
        )
        if any(
            diagnostic["code"] == "template_catalog_unavailable"
            for diagnostic in data["diagnostics"]
        ):
            return workflow_error_response(
                WorkflowError("template_catalog_unavailable")
            )
        return workflow_success_response(data)
    except Exception:
        _LOGGER.exception("可信工作流创作纯转换失败")
        return workflow_error_response(WorkflowError("internal_error"))


def create_authoring_transform_router(engine: AuthoringTransform) -> APIRouter:
    """创建三个纯可信创作转换路由；参数是转换端口，返回无写操作的 Router。"""

    router = APIRouter(
        prefix="/api/v1/authoring",
        tags=["authoring-transform"],
        route_class=BackendJSONRoute,
    )

    @router.post("/compile")
    def compile_authoring(body: AuthoringCompileRequest) -> JSONResponse:
        """编译工作流源码；参数是闭合请求，返回候选图或结构化诊断。"""

        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "python_source": body.python_source,
            "source_uri": body.source_uri,
            "applied_graph": body.applied_graph,
        }
        return _transform_response(
            engine,
            "compile",
            values,
            input_source=body.python_source,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.applied_graph,
        )

    @router.post("/generate-python")
    def generate_authoring_python(body: AuthoringGeneratePythonRequest) -> JSONResponse:
        """生成规范 Python；参数是闭合候选图请求，返回图不变的源码结果。"""

        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "graph": body.graph,
            "source_uri": body.source_uri,
        }
        return _transform_response(
            engine,
            "generate_python",
            values,
            input_source=None,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.graph,
            require_unchanged_graph=True,
        )

    @router.post("/validate")
    def validate_authoring(body: AuthoringValidateRequest) -> JSONResponse:
        """共同校验图和源码；参数是闭合请求，返回语义固定点结果。"""

        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "graph": body.graph,
            "python_source": body.python_source,
            "source_uri": body.source_uri,
        }
        return _transform_response(
            engine,
            "validate",
            values,
            input_source=body.python_source,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.graph,
            require_unchanged_graph=True,
        )

    return router


def create_authoring_transform_app(engine: AuthoringTransform) -> FastAPI:
    """创建聚焦纯转换应用；参数是转换端口，返回统一错误形状的 FastAPI 应用。"""

    app = FastAPI(title="Uni-Lab Authoring Transform", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        """净化请求校验错误；参数是请求与框架错误，返回业务码 1000。"""

        return workflow_error_response(WorkflowError("invalid_input"))

    app.include_router(create_authoring_transform_router(engine))
    return app


__all__ = [
    "AuthoringCompileRequest",
    "AuthoringGeneratePythonRequest",
    "AuthoringTransform",
    "AuthoringValidateRequest",
    "create_authoring_transform_app",
    "create_authoring_transform_router",
]
