"""领域交接检测器的只读查询合同；不暴露库存存储或写入接口。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ContainerBinding:
    container_uuid: str
    barcode: str
    barcode_namespace: str
    material_metadata: Mapping[str, Any]


@dataclass(frozen=True)
class IncomingHandoffItem:
    handoff_item_uuid: str
    container_uuid: str
    barcode: str
    barcode_namespace: str
    site_uuid: str


class HandoffQueryContext(Protocol):
    @property
    def station_uuid(self) -> str: ...

    def container_binding(self, container_uuid: str) -> ContainerBinding | None:
        """查询未删除载体的绑定；不存在时返回 None。"""
        ...

    def incoming_item(self, handoff_uuid: str, handoff_item_uuid: str) -> IncomingHandoffItem | None:
        """查询指定交接的接收项和预留库位；不确认物理到货或签收。"""
        ...
