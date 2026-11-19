"""上联 Backend 适配层（``edge -> 微后端 -> Backend`` 的最后一跳）。

进程分层中，微后端对 Edge 扮演 Backend（见 ``server.backend.edge_control``）；
本包收敛微后端对**上游 Backend** 的全部上联适配，并按线协议分成两族：

runtime.v1（微后端 / ``--role backend``）
    - ``websocket`` 控制面 WebSocket 轻通知客户端；
    - ``http``      HTTP 数据面客户端（WS 只通知变化，权威正文经此拉取）；
    - ``sync``      模板图 / 资源实例向正式后端的显式同步。
legacy（旧云端 Backend：``job_start`` / ``host_node_ready`` 消息族）
    - ``legacy.ws``        旧协议 WebSocket 客户端，job 生命周期交给微后端执行权威；
    - ``legacy.http``      旧 HTTP 数据面（``/lab/resource``、``/edge/material*``）；
    - ``legacy.sync``      注册表上报与物料镜像（开机全量 + 账本增量）；
    - ``legacy.materials`` 旧后端物料通知 → 微后端权威的翻译；
    - ``legacy.graph``     旧形状图/节点/边 → 当前契约的入站转换（``-g`` 文件与
      ``unilab graph upload`` 的读取边界调用；Graph Authority 只认当前契约）；
    - ``legacy.startup``   启动期接线（启动图转换、旧后端开机上联）；``app.main``
      只保留对启动图转换的一处调用。

公共部分：``session`` 会话工厂（Edge 固定 runtime.v1）、``url`` 连接地址
构建（runtime.v1 与 HTTP 同端口；``+1`` 只在 ``build_legacy_backend_websocket_url``）。
``probe`` 与 ``legacy`` 客户端只供 Backend 侧显式兼容旧云端；调度权威、执行
bridge 与 Edge 控制面等本地微后端代码留在 ``server.backend`` 直下，不属于本包。
"""

from unilabos.server.backend.legacy_adaptor.http import (
    BackendHTTPClient,
    BackendHTTPError,
)
from unilabos.server.backend.legacy_adaptor.probe import (
    PROTOCOL_LEGACY,
    PROTOCOL_RUNTIME_V1,
    detect_backend_protocol,
)
from unilabos.server.backend.legacy_adaptor.session import (
    BackendSessionFactory,
    BaseBackendClient,
    get_backend_client,
)
from unilabos.server.backend.legacy_adaptor.websocket import BackendWebSocketClient

__all__ = [
    "BackendHTTPClient",
    "BackendHTTPError",
    "BackendSessionFactory",
    "BackendWebSocketClient",
    "BaseBackendClient",
    "PROTOCOL_LEGACY",
    "PROTOCOL_RUNTIME_V1",
    "detect_backend_protocol",
    "get_backend_client",
]
