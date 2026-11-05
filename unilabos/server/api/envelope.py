"""信封式路由（workflow / graphs / chemicals）共用的业务码与错误形状。

``{"code": 0, "data": ...}`` 表示成功；失败固定 HTTP 200 + ``{"code": N, "error": {"msg": "..."}}``：

==== ============ ==========================================
code 类别         含义
==== ============ ==========================================
0    ok           成功（创建类接口 HTTP 201）
1000 invalid      请求参数 / 载荷不合法（含请求体校验失败、JSON 解析失败）
3002 not_found    资源不存在或已删除
3003 conflict     状态冲突：版本过期、唯一键重复、当前状态不允许该操作
5001 unavailable  依赖暂不可用或该部署不支持
1    internal     未归类的内部错误
==== ============ ==========================================

物料 / runtime / telemetry / history 等直出 DTO 的路由仍用 HTTP 状态码（404 / 409 / 422 / 503）
表达失败，见 docs/developer_guide/interfaces/http.md。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

BUSINESS_CODES: Dict[str, int] = {
    "ok": 0,
    "internal": 1,
    "invalid": 1000,
    # 写操作缺少或带错控制面 token（中间件直接以 HTTP 401 返回，不经路由）
    "unauthorized": 4001,
    "not_found": 3002,
    "conflict": 3003,
    "unavailable": 5001,
}

# 请求体 / 查询参数校验失败时也回信封（1000）的路径前缀；各信封式路由安装时登记。
_ENVELOPE_PREFIXES: set[str] = set()


def business_code(category: str) -> int:
    return BUSINESS_CODES.get(category, BUSINESS_CODES["internal"])


def envelope_error(category: str, message: str) -> JSONResponse:
    """业务错误统一形状：HTTP 200 + ``{"code", "error": {"msg"}}``。"""

    return JSONResponse(
        status_code=200,
        content={"code": business_code(category), "error": {"msg": str(message)}},
    )


def validation_detail(error: RequestValidationError) -> str:
    # 只回字段路径与校验信息，不把完整输入（可能含驱动凭据）或堆栈回显。
    return "; ".join(
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()[:5]
    )


def uses_envelope(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _ENVELOPE_PREFIXES)


def install_envelope_validation(app: FastAPI, prefixes: Iterable[str]) -> None:
    """登记信封式路径前缀；这些路径的请求校验失败返回 1000 信封，其余保持 FastAPI 默认 422。"""

    _ENVELOPE_PREFIXES.update(prefixes)
    if getattr(app.state, "unilab_envelope_validation", False):
        return
    app.state.unilab_envelope_validation = True

    @app.exception_handler(RequestValidationError)
    async def envelope_validation_handler(request: Request, error: RequestValidationError) -> Any:
        if uses_envelope(request.url.path):
            return envelope_error("invalid", f"提交内容格式不正确：{validation_detail(error)}")
        return await request_validation_exception_handler(request, error)


__all__ = [
    "BUSINESS_CODES",
    "business_code",
    "envelope_error",
    "install_envelope_validation",
    "uses_envelope",
    "validation_detail",
]
