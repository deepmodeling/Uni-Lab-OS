"""把工站调度事实通过 HTTP 批量投影到上游 Backend。"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from unilabos.utils.tracing import inject_trace_context
from unilabos.workflow.station_event_outbox import StationEventOutboxStore
from unilabos.workflow.station_event_publisher import StationEventPublisher
from unilabos.workflow.store import StoreConflict, WorkflowStore


class BackendStationEventSender:
    """实现工站事件批量 HTTP 载荷和精确 ACK 合同。"""

    def __init__(
        self,
        backend_address: str,
        api_key: str,
        station_key: str,
        *,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        """冻结上游地址、鉴权、工站身份与请求超时。

        参数：``backend_address`` 是上游 Backend 根地址；``api_key`` 是投影凭据；
        ``station_key`` 是稳定工站身份；``timeout`` 是单次 HTTP 预算；``session``
        允许测试注入。返回无。异常：任一身份为空或超时非正数时抛 ``ValueError``。
        """

        address = str(backend_address or "").strip().rstrip("/")
        token = str(api_key or "").strip()
        station = str(station_key or "").strip()
        if not address or not token or not station:
            raise ValueError("Backend 地址、投影凭据和工站身份不能为空")
        if timeout <= 0:
            raise ValueError("工站事件 HTTP 超时必须为正数")
        address = address.removesuffix("/api/v1")
        self._url = f"{address}/api/v1/edge/station-events"
        self._api_key = token
        self._station_key = station
        self._timeout = timeout
        self._session = session or requests.Session()

    def __call__(
        self,
        events: list[dict[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        """发送一个单调工站事件批次并返回 Backend 的精确 ACK 前缀。

        参数：``events`` 必须是同一工站按 ``station_sequence`` 升序的非空批次。
        返回 ACK 对象序列。异常：批次乱序、HTTP/JSON/业务响应非法时抛出异常；
        调用方保留全部未 ACK 事件并按退避策略重试。
        """

        if not events:
            raise ValueError("工站事件批次不能为空")
        sequences = [event.get("station_sequence") for event in events]
        if any(
            isinstance(sequence, bool) or not isinstance(sequence, int)
            for sequence in sequences
        ) or sequences != sorted(sequences):
            raise StoreConflict("工站事件批次必须按整数序列升序排列")
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": (f"{self._station_key}:{sequences[0]}:{sequences[-1]}"),
        }
        inject_trace_context(headers)
        response = self._session.post(
            self._url,
            json={
                "station_key": self._station_key,
                "events": [_public_event(event) for event in events],
            },
            headers=headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise StoreConflict("Backend 工站事件响应必须是对象")
        if "code" in payload and int(payload.get("code") or 0) != 0:
            raise StoreConflict(
                f"Backend 拒绝工站事件：{payload.get('error') or payload}"
            )
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            raise StoreConflict("Backend 工站事件响应缺少 data 对象")
        acknowledgements = data.get("acknowledgements")
        if not isinstance(acknowledgements, list):
            raise StoreConflict("Backend 工站事件响应缺少 acknowledgements 列表")
        return acknowledgements


_projection_lock = threading.RLock()
_projection_publisher: StationEventPublisher | None = None
_projection_store: WorkflowStore | None = None


def start_station_event_projection(
    store: WorkflowStore,
    *,
    backend_address: str,
    api_key: str,
    station_key: str,
    timeout: float = 10.0,
) -> StationEventPublisher:
    """幂等启动工作流运行库的 Backend 异步投影线程。

    参数：``store`` 是工站运行事实权威，其余参数构造 HTTP 适配器。返回当前进程
    唯一发布器。异常：第二次用另一存储装配或 HTTP 配置非法时关闭式失败。
    """

    global _projection_publisher, _projection_store
    with _projection_lock:
        if _projection_publisher is not None:
            if _projection_store is not store:
                raise StoreConflict("工站事件发布器已经绑定另一份运行事实库")
            return _projection_publisher
        outbox = StationEventOutboxStore(store)
        sender = BackendStationEventSender(
            backend_address,
            api_key,
            station_key,
            timeout=timeout,
        )
        publisher = StationEventPublisher(outbox, sender)
        publisher.start()
        _projection_publisher = publisher
        _projection_store = store
        return publisher


def shutdown_station_event_projection() -> None:
    """停止工站事件发布线程，并保留所有未获 ACK 的本地事件。"""

    global _projection_publisher, _projection_store
    with _projection_lock:
        publisher = _projection_publisher
        _projection_publisher = None
        _projection_store = None
    if publisher is not None:
        publisher.stop()


def _public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """移除本地投递状态，只发送 Backend 持久化所需的工站事实。"""

    fields = (
        "event_uuid",
        "station_sequence",
        "created_at",
        "event_type",
        "aggregate_type",
        "aggregate_uuid",
        "source_kind",
        "source_uuid",
        "idempotency_key",
        "payload",
    )
    return {field: event[field] for field in fields}


__all__ = [
    "BackendStationEventSender",
    "shutdown_station_event_projection",
    "start_station_event_projection",
]
