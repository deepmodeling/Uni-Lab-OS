"""测试用的信封断言：把「直出 DTO / HTTP 状态码」的旧断言翻译成 V1 信封合同。"""

from __future__ import annotations

from typing import Any


def ok_data(response: Any, *, status: int | tuple[int, ...] = (200, 201, 202)) -> Any:
    """断言成功信封（``code == 0``）并返回 ``data``（无数据命令返回 ``None``）。"""

    statuses = (status,) if isinstance(status, int) else status
    assert response.status_code in statuses, (response.status_code, response.text[:500])
    body = response.json()
    assert isinstance(body, dict) and body.get("code") == 0, body
    assert set(body) <= {"code", "data"}, body
    return body.get("data")


def error_of(response: Any, code: int, *, status: int = 200) -> dict[str, Any]:
    """断言失败信封（指定业务码，默认 HTTP 200）并返回 ``error`` 对象。"""

    assert response.status_code == status, (response.status_code, response.text[:500])
    body = response.json()
    assert isinstance(body, dict) and body.get("code") == code, body
    error = body.get("error")
    assert isinstance(error, dict) and isinstance(error.get("msg"), str), body
    return error


def envelope(data: Any = None) -> dict[str, Any]:
    """伪造服务端成功信封（假 HTTP 服务器 / 假 opener 用）。"""

    return {"code": 0} if data is None else {"code": 0, "data": data}


def command_result(response: Any) -> Any:
    """断言 ``POST /api/v1/inventory/commands`` 已完成（V1 裸结果，不包信封）并返回 ``result``。"""

    assert response.status_code == 200, (response.status_code, response.text[:500])
    body = response.json()
    assert isinstance(body, dict) and body.get("status") == "completed", body
    return body["result"]


def command_rejected(response: Any, code: int) -> dict[str, Any]:
    """断言库存命令被拒绝（HTTP 200 + ``status=rejected``，业务码 ``code``）并返回结果体。"""

    assert response.status_code == 200, (response.status_code, response.text[:500])
    body = response.json()
    assert isinstance(body, dict) and body.get("status") == "rejected" and body.get("code") == code, body
    return body


__all__ = ["command_rejected", "command_result", "envelope", "error_of", "ok_data"]
