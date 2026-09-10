"""可信工作流创作纯转换 HTTP 请求预算测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from tests.app.test_authoring_transform_api import (
    HTTP_BODY_LIMIT,
    INTEGER_DIGIT_LIMIT,
    INVALID_INPUT,
    JSON_DEPTH_LIMIT,
    WORKFLOW_UUID,
    RecordingTransformEngine,
    _graph,
)
from unilabos.app.workflow_authoring_transform import create_authoring_transform_app


class _ReceiveSpy:
    """记录 ASGI 接收次数，证明声明超限时不消费请求体。"""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        """保存待发送字节块；参数 ``chunks`` 是完整接收序列，返回无。"""

        self.chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        """返回下一 ASGI 请求块；参数无，越界表示路由错误地继续读取。"""

        if self.calls >= len(self.chunks):
            raise AssertionError("请求体结束后仍继续 receive")
        index = self.calls
        self.calls += 1
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index + 1 < len(self.chunks),
        }


def _invoke_asgi(
    app: FastAPI,
    *,
    body: bytes,
    content_length: int | None = None,
) -> tuple[int, dict[str, Any], _ReceiveSpy]:
    """直接调用 ASGI 应用以观察请求体预算的读取行为。

    参数：``app`` 是 focused 应用，``body`` 是原始 JSON，``content_length`` 可覆盖声明。
    返回：HTTP 状态、JSON 响应和接收探针。
    """

    receive = _ReceiveSpy([body])
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """记录 ASGI 响应消息；参数是单条消息，返回无且不执行网络 I/O。"""

        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "root_path": "",
        "path": "/api/v1/authoring/compile",
        "raw_path": b"/api/v1/authoring/compile",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(
        item["status"] for item in sent if item["type"] == "http.response.start"
    )
    raw = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return status, json.loads(raw), receive


def _raw_compile_body(graph: bytes) -> bytes:
    """把指定原始图片段包入编译请求，避免 Python 递归构造深对象。

    参数：``graph`` 是未经解码的 JSON 对象。返回：完整请求字节。
    """

    return (
        b'{"workflow_uuid":"'
        + WORKFLOW_UUID.encode("ascii")
        + b'","revision":7,"python_source":"value = 1\\n",'
        b'"source_uri":"package://lab/f05.py","applied_graph":' + graph + b"}"
    )


def test_declared_body_oversize_fails_before_receive() -> None:
    """声明超过 8 MiB 时必须在读取首个字节前返回输入业务错误。

    参数：无。返回：无；断言接收次数与引擎调用次数均为零。
    """

    engine = RecordingTransformEngine()
    status, payload, receive = _invoke_asgi(
        create_authoring_transform_app(engine),
        body=_raw_compile_body(json.dumps(_graph()).encode()),
        content_length=HTTP_BODY_LIMIT + 1,
    )
    assert status == 200
    assert payload == INVALID_INPUT
    assert receive.calls == 0
    assert engine.calls == []


@pytest.mark.parametrize(
    ("digits", "expected_code", "expected_calls"),
    [(INTEGER_DIGIT_LIMIT, 0, 1), (INTEGER_DIGIT_LIMIT + 1, 1000, 0)],
    ids=["integer-limit-accepted", "integer-limit-plus-one-rejected"],
)
def test_integer_digit_budget(
    digits: int,
    expected_code: int,
    expected_calls: int,
) -> None:
    """外部 JSON 整数允许 4096 位并在第 4097 位关闭失败。

    参数：位数、业务码和调用数由边界矩阵提供。返回：无；断言预算先于引擎。
    """

    integer = b"1" + b"0" * (digits - 1)
    graph = json.dumps(_graph(), separators=(",", ":")).encode()[:-1]
    body = _raw_compile_body(graph + b',"external":' + integer + b"}")
    engine = RecordingTransformEngine("diagnostic")
    status, payload, _receive = _invoke_asgi(
        create_authoring_transform_app(engine),
        body=body,
        content_length=len(body),
    )
    assert status == 200
    assert payload["code"] == expected_code
    assert len(engine.calls) == expected_calls


@pytest.mark.parametrize(
    ("depth", "expected_code", "expected_calls"),
    [(JSON_DEPTH_LIMIT, 0, 1), (JSON_DEPTH_LIMIT + 1, 1000, 0)],
    ids=["depth-limit-accepted", "depth-limit-plus-one-rejected"],
)
def test_complete_json_depth_budget(
    depth: int,
    expected_code: int,
    expected_calls: int,
) -> None:
    """完整 JSON 文档允许 10000 层并在第 10001 层关闭失败。

    参数：深度、业务码和调用数由边界矩阵提供。返回：无；断言预算先于引擎。
    """

    # 请求根与 ``applied_graph`` 已占两层，数组只补足剩余深度；``valid_graph``
    # 同时保留入口所要求的规范工作流（Workflow）身份。
    array_depth = depth - 2
    valid_graph = json.dumps(_graph(), separators=(",", ":")).encode()[:-1]
    graph = (
        valid_graph
        + b',"deep":'
        + b"[" * array_depth
        + b"0"
        + b"]" * array_depth
        + b"}"
    )
    body = _raw_compile_body(graph)
    engine = RecordingTransformEngine("diagnostic")
    status, payload, _receive = _invoke_asgi(
        create_authoring_transform_app(engine),
        body=body,
        content_length=len(body),
    )
    assert status == 200
    assert payload["code"] == expected_code
    assert len(engine.calls) == expected_calls
