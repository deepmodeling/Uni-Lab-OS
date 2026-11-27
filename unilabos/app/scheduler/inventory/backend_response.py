"""Backend 同形 FastAPI 路由共享的数值响应信封。"""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi.responses import JSONResponse

from unilabos.app.scheduler.inventory.backend_contract import BackendContractError


def success(data: Any = None, *, status_code: int = 200) -> JSONResponse:
    """构造 code=0 的 Backend 成功信封；无数据命令不输出 data 字段。"""

    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = data
    return JSONResponse(status_code=status_code, content=content)


def error_response(error: BackendContractError) -> JSONResponse:
    """把领域错误稳定映射为 Backend 数值错误信封。"""

    return JSONResponse(
        status_code=200,
        content={"code": error.code, "error": {"msg": error.message}},
    )


def call(
    callback: Callable[..., Any], *args: Any, status_code: int = 200, **kwargs: Any
) -> JSONResponse:
    """调用领域方法并统一封装成功与已知业务错误。"""

    try:
        return success(callback(*args, **kwargs), status_code=status_code)
    except BackendContractError as error:
        return error_response(error)


__all__ = ["call", "error_response", "success"]
