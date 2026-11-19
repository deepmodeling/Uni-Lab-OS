"""旧云端 Backend（``job_start`` / ``host_node_ready`` 消息族）的适配实现。

runtime.v1 之前的 Backend 直接在 WebSocket 上下发执行命令、在 HTTP 上接收
注册表与物料树。本包把这套线协议翻译成微后端的执行 / 物料权威调用：

- ``http``      旧 HTTP 数据面（``/lab/resource``、``/edge/material*``）；
- ``ws``        旧 WebSocket 客户端，job 生命周期交给 ``JobExecutionBackend``；
- ``sync``      注册表与物料树向旧 Backend 的显式同步（开机全量 + 账本增量）；
- ``graph``     旧后端导出图 / 旧示例图形状 → 当前 node-link 契约的入站转换
  （图文件读取边界与旧后端物料拉取共用；微后端本体只认当前契约）；
- ``materials`` 旧后端物料通知 → 微后端权威的翻译；
- ``startup``   Edge 启动期接线：``-g`` 启动图的旧格式转换、探测到旧后端后的
  注册表上报 + 物料镜像（``app.main`` 对旧后端的全部触碰都只是调用这里）。
"""

from unilabos.server.backend.legacy_adaptor.legacy.http import (
    LegacyBackendHTTPClient,
    LegacyBackendHTTPError,
)
from unilabos.server.backend.legacy_adaptor.legacy.ws import LegacyBackendWebSocketClient

__all__ = [
    "LegacyBackendHTTPClient",
    "LegacyBackendHTTPError",
    "LegacyBackendWebSocketClient",
]
