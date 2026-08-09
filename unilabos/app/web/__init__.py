"""使用延迟导出的 Web UI 包。

导入 :mod:`unilabos.app.web.client` 时不能连带加载 ROS2 专属状态页。该延迟门面保留
原有 ``from unilabos.app.web import ...`` API，同时允许 basic 和 Dora 在不初始化
ROS Web 模块的情况下使用 HTTP 数据访问。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "setup_web_pages",
    "setup_server",
    "start_server",
    "http_client",
    "setup_api_routes",
]


def __getattr__(name: str) -> Any:
    if name == "setup_web_pages":
        from unilabos.app.web.pages import setup_web_pages

        value = setup_web_pages
    elif name in {"setup_server", "start_server"}:
        from unilabos.app.web.server import setup_server, start_server

        value = {"setup_server": setup_server, "start_server": start_server}[name]
    elif name == "http_client":
        from unilabos.app.web.client import http_client

        value = http_client
    elif name == "setup_api_routes":
        from unilabos.app.web.api import setup_api_routes

        value = setup_api_routes
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
