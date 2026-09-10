"""工作流（Workflow）HTTP 路由的请求体资源预算测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow import json_codec

_HTTP_BODY_LIMIT = 8 * 1024 * 1024
_EXTERNAL_INTEGER_DIGITS = 4096
_WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
_INVALID_INPUT = {
    "code": 1000,
    "error": {"msg": "提交内容格式不正确"},
}
_BODY_ROUTES = [
    pytest.param("POST", "/api/v1/workflows", id="post-workflow"),
    pytest.param(
        "PUT",
        f"/api/v1/workflows/{_WORKFLOW_UUID}",
        id="put-workflow",
    ),
]
_NON_JSON_CONTENT_TYPES = [
    pytest.param(b"text/plain", id="text-plain"),
    pytest.param(None, id="missing-content-type"),
    pytest.param(b"application/x-unilab-unknown", id="unknown-mime"),
]
_STREAM_FRAMING = [
    pytest.param([], id="missing-content-length"),
    pytest.param([(b"transfer-encoding", b"chunked")], id="chunked"),
]


class _RecordingWorkflowService:
    """记录 HTTP 适配器（Adapter）是否越过校验边界，不执行真实持久化。"""

    def __init__(self) -> None:
        """初始化调用轨迹；三个列表分别记录写调用、副作用和只读调用。"""

        self.body_calls: list[str] = []
        self.side_effects: list[str] = []
        self.read_calls: list[str] = []

    def create_workflow(self, **_values: Any) -> dict[str, bool]:
        """模拟创建工作流（Workflow），并记录越过边界的持久副作用。"""

        self.body_calls.append("create_workflow")
        self.side_effects.append("created")
        return {"accepted": True}

    def update_workflow(
        self,
        workflow_uuid: str,
        **_values: Any,
    ) -> dict[str, str]:
        """模拟更新工作流（Workflow）；`workflow_uuid` 是待更新稳定身份。"""

        self.body_calls.append("update_workflow")
        self.side_effects.append(f"updated:{workflow_uuid}")
        return {"uuid": workflow_uuid}

    def list_workflows(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
    ) -> dict[str, Any]:
        """模拟只读列表；参数保留分页和名称过滤的接口含义。"""

        self.read_calls.append("list_workflows")
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "name": name,
        }


class _ReceiveSpy:
    """可观测 ASGI receive 调用次数的分块请求体。"""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        """保存按顺序返回的 `chunks`，`calls` 记录实际读取次数。"""

        self._chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        """返回下一块请求体；越界读取表示 HTTP 适配器违反停止规则。"""

        if self.calls >= len(self._chunks):
            raise AssertionError("工作流路由在请求体结束后仍继续读取")
        index = self.calls
        self.calls += 1
        return {
            "type": "http.request",
            "body": self._chunks[index],
            "more_body": index + 1 < len(self._chunks),
        }


def _invoke_asgi(
    app: FastAPI,
    *,
    method: str,
    path: str,
    chunks: Sequence[bytes],
    content_type: bytes | None,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[int, dict[str, Any], _ReceiveSpy]:
    """直接调用 ASGI 应用并返回状态、JSON 和读取轨迹。

    参数说明：`method/path` 标识路由，`chunks` 是请求体分块，`content_type`
    和 `extra_headers` 构造边界条件；返回值中的 spy 用于验证是否提前停止。
    """

    receive = _ReceiveSpy(chunks)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集 ASGI 响应消息；`message` 是单个响应帧。"""

        sent.append(message)

    headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        headers.append((b"content-type", content_type))
    headers.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))

    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body), receive


def _assert_no_body_service_side_effect(
    service: _RecordingWorkflowService,
) -> None:
    """断言写接口未穿透；`service` 保存调用与副作用轨迹。"""

    assert service.body_calls == []
    assert service.side_effects == []


def _integer_token(digits: int) -> bytes:
    """生成指定十进制位数的正整数 token；`digits` 必须为正数。"""

    assert digits > 0
    return b"1" + b"0" * (digits - 1)


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
def test_declared_oversized_body_rejects_before_receive_for_any_mime(
    method: str,
    path: str,
    content_type: bytes | None,
) -> None:
    """声明超限时不读取请求体，也不因 MIME 类型不同而绕过预算。"""

    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method=method,
        path=path,
        chunks=[b"{}"],
        content_type=content_type,
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT + 1).encode())],
    )

    assert status == 200
    assert payload == _INVALID_INPUT
    assert receive.calls == 0
    _assert_no_body_service_side_effect(service)


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
@pytest.mark.parametrize("framing_headers", _STREAM_FRAMING)
def test_streamed_oversized_body_stops_at_first_excess_byte_for_any_mime(
    method: str,
    path: str,
    content_type: bytes | None,
    framing_headers: list[tuple[bytes, bytes]],
) -> None:
    """流式请求超过 8 MiB 后立即停止，不继续读取后续块。"""

    service = _RecordingWorkflowService()
    one_mib_chunk = b" " * (1024 * 1024)
    chunks = [one_mib_chunk] * 8 + [b"x", b"must-not-be-read"]

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method=method,
        path=path,
        chunks=chunks,
        content_type=content_type,
        extra_headers=framing_headers,
    )

    assert status == 200
    assert payload == _INVALID_INPUT
    assert receive.calls == 9
    _assert_no_body_service_side_effect(service)


@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
def test_exact_limit_non_json_body_keeps_backend_validation_envelope(
    content_type: bytes | None,
) -> None:
    """恰好 8 MiB 的非 JSON 请求可读完，再由后端（Backend）错误信封拒绝。"""

    service = _RecordingWorkflowService()
    body = b"x" * _HTTP_BODY_LIMIT

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method="POST",
        path="/api/v1/workflows",
        chunks=[body],
        content_type=content_type,
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT).encode())],
    )

    assert status == 200
    assert payload == _INVALID_INPUT
    assert receive.calls == 1
    _assert_no_body_service_side_effect(service)


def test_json_integer_budget_rejects_before_bigint_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4097 位外部整数在大整数构造和工作流服务调用前失败关闭。"""

    decoder_calls: list[int] = []

    def record_bigint_construction(raw: str) -> int:
        """记录大整数构造尝试；`raw` 是未经构造的十进制 token。"""

        decoder_calls.append(len(raw))
        return 0

    monkeypatch.setattr(
        json_codec,
        "_decode_json_integer",
        record_bigint_construction,
    )
    service = _RecordingWorkflowService()
    body = (
        b'{"name":"must not persist","future_integer":'
        + _integer_token(_EXTERNAL_INTEGER_DIGITS + 1)
        + b"}"
    )

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method="POST",
        path="/api/v1/workflows",
        chunks=[body],
        content_type=b"application/json",
        extra_headers=[(b"content-length", str(len(body)).encode())],
    )

    assert status == 200
    assert payload == _INVALID_INPUT
    assert receive.calls == 1
    assert decoder_calls == []
    _assert_no_body_service_side_effect(service)


def test_bodyless_get_with_json_content_type_does_not_read_request_body() -> None:
    """无请求体的工作流列表路由不能因 JSON MIME 类型触发 receive。"""

    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method="GET",
        path="/api/v1/workflows",
        chunks=[b""],
        content_type=b"application/json",
    )

    assert status == 200
    assert payload["code"] == 0
    assert payload["data"]["items"] == []
    assert receive.calls == 0
    assert service.read_calls == ["list_workflows"]
    _assert_no_body_service_side_effect(service)


def test_bodyless_sse_route_with_json_content_type_does_not_read_body() -> None:
    """无请求体的 SSE（服务端事件）路由不得读取 body，非法游标照常拒绝。"""

    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),  # type: ignore[arg-type]
        method="GET",
        path="/api/v1/events",
        chunks=[b""],
        content_type=b"application/json",
        extra_headers=[(b"last-event-id", b"not-an-integer")],
    )

    assert status == 200
    assert payload == _INVALID_INPUT
    assert receive.calls == 0
    assert service.read_calls == []
    _assert_no_body_service_side_effect(service)
