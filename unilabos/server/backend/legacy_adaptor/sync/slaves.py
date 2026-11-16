"""Host 把它连着的 Slave 告诉调度权威：注册表接受规则 B（单机部署全部自动接受）的依据。

注册表上报本身已带上报那一刻连着的 Slave；Slave 往往比上报晚接入（Host 重启后重连、Slave
后起），所以 Host 在每次 Slave HELLO 后再补报一次当前的 Slave 列表（``POST /api/v1/registry/slaves``，
单进程时直接写进程内服务）。只在列表变化时发送，短时间内的多次接入合并成一次。权威只记跨机器
的 Slave，补报失败只打日志：下一次 Slave 接入或注册表上报时会再带上。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

SlaveList = List[Dict[str, str]]
#: 多个 Slave 接连接入（Host 重启后一起重连）时合并成一次补报
DEBOUNCE_S = 1.0


class SlaveAnnouncer:
    """监听 HostLink 的 Slave 接入，把变化后的 Slave 列表交给 ``send``。"""

    def __init__(
        self,
        send: Callable[[SlaveList], None],
        *,
        slaves: Optional[Callable[[], SlaveList]] = None,
        debounce_s: float = DEBOUNCE_S,
    ) -> None:
        if slaves is None:
            from unilabos.server.backend.legacy_adaptor.sync.templates import current_hostlink_slaves

            slaves = current_hostlink_slaves
        self._send = send
        self._slaves = slaves
        self._debounce_s = debounce_s
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._sent: Optional[FrozenSet[Tuple[str, str]]] = None
        self._remove_listener: Optional[Callable[[], None]] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, *, already_sent: SlaveList = ()) -> "SlaveAnnouncer":
        """开始监听；``already_sent`` 是注册表上报里已经带过的列表，与它相同就不重复发。"""

        from unilabos.backend.hostlink.server import add_peer_listener

        self._sent = _key(already_sent)
        self._remove_listener = add_peer_listener(self._on_peer)
        self._thread = threading.Thread(target=self._run, name="registry-slave-announcer", daemon=True)
        self._thread.start()
        # 上报之后、监听装上之前接入的 Slave 不会再触发 HELLO：启动时核对一次
        self._wake.set()
        return self

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _on_peer(self, peer: Dict[str, object]) -> None:
        if str(peer.get("role") or "slave") == "slave":
            self._wake.set()

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait()
            if self._stopped.is_set():
                return
            # 合并紧接着的多次接入
            self._stopped.wait(self._debounce_s)
            self._wake.clear()
            if self._stopped.is_set():
                return
            current = self._slaves()
            key = _key(current)
            if key == self._sent:
                continue
            try:
                self._send(current)
            except Exception as exc:  # noqa: BLE001 - 补报失败等下一次接入或上报再带上
                logger.warning("[Registry Sync] 向调度权威补报 Slave 列表失败: %s", exc)
                continue
            self._sent = key


def _key(slaves: SlaveList) -> FrozenSet[Tuple[str, str]]:
    return frozenset(
        (str(item.get("machine_name") or ""), str(item.get("machine_id") or "")) for item in slaves
    )


def http_slave_sender(backend_address: str, *, edge_uuid: str, timeout: float = 10.0) -> Callable[[SlaveList], None]:
    """经 ``POST /api/v1/registry/slaves`` 补报（带控制面 token）。"""

    import requests

    from unilabos.config.control_token import client_headers
    from unilabos.server.backend.legacy_adaptor.sync.templates import _api_base
    from unilabos.utils.machine_identity import machine_id

    url = f"{_api_base(backend_address)}/registry/slaves"

    def send(slaves: SlaveList) -> None:
        response = requests.post(
            url,
            json={"edge_uuid": edge_uuid, "machine_id": machine_id(), "slaves": slaves},
            headers=client_headers(),
            timeout=timeout,
        )
        if response.status_code == 404:
            # 旧权威没有该端点：它也不做规则 B 判定，不必再发
            logger.info("[Registry Sync] 调度权威不接收 Slave 补报（旧版本），跳过")
            return
        response.raise_for_status()

    return send


def local_slave_sender(registry_service: object, *, edge_uuid: str) -> Callable[[SlaveList], None]:
    """单进程时直接写进程内 Registry Authority。"""

    from unilabos.server.services.runtime.registry import RegistryReporter

    def send(slaves: SlaveList) -> None:
        registry_service.record_slaves(  # type: ignore[attr-defined]
            RegistryReporter.local(edge_uuid=edge_uuid, slaves=slaves)
        )

    return send


_announcer: Optional[SlaveAnnouncer] = None
_announcer_lock = threading.Lock()


def start_slave_announcer(send: Callable[[SlaveList], None], *, already_sent: SlaveList = ()) -> SlaveAnnouncer:
    """进程内只起一个补报器（安静点重启是整个 Host 进程重启，不会重复）。"""

    global _announcer
    with _announcer_lock:
        if _announcer is None:
            _announcer = SlaveAnnouncer(send).start(already_sent=already_sent)
        return _announcer


__all__ = [
    "SlaveAnnouncer",
    "http_slave_sender",
    "local_slave_sender",
    "start_slave_announcer",
]
