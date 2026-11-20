"""Slave 经 HostLink 同步 Host 的完整物理拓扑图（协议编译器与内置虚拟设备用）。

受管 Slave 只装配一个根设备的子树，手动启动的 Slave 只装配自己的 ``-g`` 图；跨子树的连线、
别处的容器与设备只在 Host 的图里。Slave 可能在另一台机器上，所以图只经 HostLink 同步，不读
Host 的文件或进程状态：

- Host 在 HELLO 与心跳（PING）应答里带 ``physical_graph_revision``（本进程启动图的版本标记，
  还没有图时为 null），并应答 ``graph.physical.get``：``{"revision": ..., "graph": <node-link JSON>}``；
- Slave 看到新的版本标记就在后台取图，登记给协议编译器，与本机启动图合并使用
  （:func:`unilabos.client.host.runtime.workstation_protocol.physical_setup_graph`）。Slave 启动时连上
  Host 后先等这次取图（最多一个请求超时）再装配设备；
- 应答里没有这个字段的是旧版 Host，只用本机启动图；``--disable_hostlink``（纯 DDS）没有 HostLink，
  ``--slave_no_host`` 在连上 Host 之前同样只用本机启动图；取图失败时沿用现有的图，下一次心跳再试。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from unilabos.client.host.hostlink.protocol import PHYSICAL_GRAPH_REVISION, ActionType
from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.client.host.hostlink.client import HostLinkClient

#: Slave 在 HELLO 里声明的能力：会向 Host 取物理拓扑图
CAPABILITY = "physical-graph"


def physical_graph_payload(graph: Any, revision: Optional[str]) -> Dict[str, Any]:
    """Host 侧 ``graph.physical.get`` 的应答：版本标记与 node-link JSON（没有图时为 null）。"""

    if graph is None:
        return {"revision": None, "graph": None}
    import networkx as nx

    return {"revision": revision, "graph": nx.node_link_data(graph, edges="links")}


def physical_graph_from_payload(payload: Any) -> Tuple[Any, Optional[str]]:
    """Slave 侧解析 ``graph.physical.get`` 的应答，返回 ``(networkx.Graph 或 None, 版本标记)``。

    图经 JSON 传输：节点 / 连线属性里的元组到这边是列表，协议编译器不区分两者。
    """

    if not isinstance(payload, dict):
        raise ValueError(f"{ActionType.PHYSICAL_GRAPH_GET} 的应答不是对象：{type(payload).__name__}")
    data = payload.get("graph")
    raw_revision = payload.get("revision")
    if data is None or raw_revision is None:
        return None, None
    if not isinstance(data, dict):
        raise ValueError(f"{ActionType.PHYSICAL_GRAPH_GET} 应答的 graph 不是 node-link 对象")
    import networkx as nx

    return nx.node_link_graph(data, edges="links"), str(raw_revision)


class PhysicalGraphSync:
    """Slave 侧：跟着 Host 在 HELLO / 心跳里声明的版本标记取图，登记给协议编译器。"""

    def __init__(self, client: "HostLinkClient") -> None:
        self._client = client
        self._lock = threading.Lock()
        #: Host 最近一次声明的版本标记；None 表示 Host 没有图（或是不提供的旧版 Host）
        self._advertised: Optional[str] = None
        #: 已登记给协议编译器的版本标记
        self._applied: Optional[str] = None
        #: 每次 HELLO（新连接）加一：上一条连接上取到的图不再登记
        self._generation = 0
        self._worker: Optional[threading.Thread] = None
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self._failed_revision: Optional[str] = None
        self._legacy_reported = False
        if CAPABILITY not in client.capabilities:
            client.capabilities.append(CAPABILITY)
        client.add_host_info_listener(self._on_host_info)

    def wait(self, timeout: Optional[float]) -> bool:
        """等正在进行的取图结束（最多 ``timeout`` 秒）；返回 Host 声明的图是否已经登记。"""

        self._idle.wait(timeout)
        with self._lock:
            return self._advertised is not None and self._advertised == self._applied

    def close(self) -> None:
        """不再跟 Host 同步，并撤下本对象登记的 Host 图（协议编译回到只用本机启动图）。"""

        self._client.remove_host_info_listener(self._on_host_info)
        with self._lock:
            self._closed = True
            self._advertised = None
            had_graph, self._applied = self._applied is not None, None
            if had_graph:
                self._set_host_graph(None, None)

    # ── 内部 ──────────────────────────────────────────────────────

    @staticmethod
    def _set_host_graph(graph: Any, revision: Optional[str]) -> None:
        from unilabos.client.host.runtime.workstation_protocol import set_host_physical_setup_graph

        set_host_physical_setup_graph(graph, revision)

    def _on_host_info(self, info: Dict[str, Any], hello: bool) -> None:
        """HostLink 客户端在 HELLO / 心跳应答后调用（连接管理线程里，只做登记、起后台线程）。

        HELLO 的回调在连接标记为在线之前：``connect_blocking`` 返回时要取的图已经开始取，
        启动流程随后 :meth:`wait` 就能等到它。
        """

        legacy = PHYSICAL_GRAPH_REVISION not in info
        if legacy and not hello:
            # 只看握手：旧版 Host 的心跳应答同样没有这个字段，已在 HELLO 时处理
            return
        raw_revision = None if legacy else info.get(PHYSICAL_GRAPH_REVISION)
        revision = str(raw_revision) if raw_revision is not None else None
        worker: Optional[threading.Thread] = None
        with self._lock:
            if self._closed:
                return
            if hello:
                self._generation += 1
            self._advertised = revision
            if revision is None:
                # 旧版 Host，或 Host 没有图：之前登记的图来自别的 Host 进程，不能再用
                if self._applied is not None:
                    self._applied = None
                    self._set_host_graph(None, None)
            elif revision != self._applied and self._worker is None:
                self._idle.clear()
                worker = self._worker = threading.Thread(
                    target=self._run, name="hostlink-physical-graph", daemon=True
                )
            report_legacy = legacy and not self._legacy_reported
            if report_legacy:
                self._legacy_reported = True
        if report_legacy:
            logger.info("[HostLink] Host 不提供物理拓扑图（旧版 Host），协议编译只用本机启动图")
        if worker is not None:
            worker.start()

    def _finish(self) -> None:
        """持锁调用：后台线程结束。"""

        self._worker = None
        self._idle.set()

    def _run(self) -> None:
        client = self._client
        while True:
            with self._lock:
                wanted, generation = self._advertised, self._generation
                if self._closed or wanted is None or wanted == self._applied:
                    self._finish()
                    return
            try:
                # 第一次取图由 HELLO 触发，那时连接还没标记为在线：等握手完成再发请求
                if not client.wait_online(client.connect_timeout):
                    raise ConnectionError("HostLink 连接不在线")
                graph, revision = physical_graph_from_payload(
                    client.request(ActionType.PHYSICAL_GRAPH_GET, {})
                )
            except Exception as exc:  # noqa: BLE001 - 取不到就沿用现有的图，下一次心跳再试
                with self._lock:
                    # 失败期间换了连接或 Host 声明了新图：立刻按最新状态再取，否则等下一次心跳
                    retry_now = not self._closed and (
                        generation != self._generation or wanted != self._advertised
                    )
                    first = self._failed_revision != wanted
                    self._failed_revision = wanted
                    if not retry_now:
                        self._finish()
                if retry_now:
                    continue
                (logger.warning if first else logger.debug)(
                    "[HostLink] 从 Host 取物理拓扑图失败，协议编译沿用现有的图，下一次心跳再试：%s", exc
                )
                return
            with self._lock:
                if self._closed or generation != self._generation:
                    # 取图期间换了连接（Host 重启或换了 Host）：按新连接声明的版本重取
                    continue
                if self._advertised == wanted:
                    # 取图期间 Host 没有声明别的图：应答里的就是当前的（可能比心跳里的还新）
                    self._advertised = revision
                if graph is None:
                    if self._applied is not None:
                        self._applied = None
                        self._set_host_graph(None, None)
                    continue
                self._applied = revision
                self._failed_revision = None
                self._set_host_graph(graph, revision)
            logger.info(
                "[HostLink] 已从 Host 取得物理拓扑图（%d 个节点、%d 条连线），协议编译与本机启动图合并使用",
                graph.number_of_nodes(),
                graph.number_of_edges(),
            )


def attach_physical_graph_sync(client: "HostLinkClient") -> PhysicalGraphSync:
    """给 Slave 的 HostLink 客户端挂上物理拓扑图同步；须在连接 Host 之前调用，HELLO 才带上能力声明。"""

    return PhysicalGraphSync(client)


__all__ = [
    "CAPABILITY",
    "PhysicalGraphSync",
    "attach_physical_graph_sync",
    "physical_graph_from_payload",
    "physical_graph_payload",
]
