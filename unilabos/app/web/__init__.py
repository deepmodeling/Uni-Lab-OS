"""Web UI 公共入口；按需加载页面、服务端和客户端模块。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "setup_web_pages": ("unilabos.app.web.pages", "setup_web_pages"),
    "setup_server": ("unilabos.app.web.server", "setup_server"),
    "start_server": ("unilabos.app.web.server", "start_server"),
    "http_client": ("unilabos.app.web.client", "http_client"),
    "setup_api_routes": ("unilabos.app.web.api", "setup_api_routes"),
}


def __getattr__(name: str) -> Any:
    """按属性首次访问加载对应模块，避免 Backend 进程导入设备注册表。

    参数：``name`` 是公开导出名。返回：对应函数或客户端对象。异常：未知名称抛
    ``AttributeError``，目标模块导入错误原样传播。
    """

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value

__all__ = [
    "http_client",
    "setup_api_routes",
    "setup_server",
    "setup_web_pages",
    "start_server",
]
