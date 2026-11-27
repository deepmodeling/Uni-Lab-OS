"""库存表结构留在 OS 内，检测器仅得到独立、不可变的查询结果。"""

import json
from types import MappingProxyType
from typing import Any

from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.runtime.handoff import ContainerBinding, IncomingHandoffItem


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class InventoryHandoffQueries:
    def __init__(self, store: InventoryStore, station_uuid: str) -> None:
        self._store = store
        self._station_uuid = station_uuid

    @property
    def station_uuid(self) -> str:
        return self._station_uuid

    def container_binding(self, container_uuid: str) -> ContainerBinding | None:
        row = self._store.query_one(
            "SELECT b.barcode,b.barcode_namespace,m.meta_data FROM container_station_binding b "
            "JOIN material m ON m.uuid=b.carrier_material_uuid "
            "WHERE b.container_uuid=? AND m.deleted_at IS NULL",
            (container_uuid,),
        )
        if row is None:
            return None
        return ContainerBinding(
            container_uuid, row["barcode"], row["barcode_namespace"],
            _freeze(json.loads(row["meta_data"])),
        )

    def incoming_item(self, handoff_uuid: str, handoff_item_uuid: str) -> IncomingHandoffItem | None:
        row = self._store.query_one(
            "SELECT i.container_uuid,b.barcode,b.barcode_namespace,r.site_uuid "
            "FROM station_handoff_item i "
            "JOIN container_station_binding b ON b.container_uuid=i.container_uuid "
            "JOIN station_ingress_reservation r ON r.uuid=i.reservation_uuid "
            "WHERE i.handoff_uuid=? AND i.handoff_item_uuid=?",
            (handoff_uuid, handoff_item_uuid),
        )
        if row is None:
            return None
        return IncomingHandoffItem(handoff_item_uuid, row["container_uuid"], row["barcode"],
                                   row["barcode_namespace"], row["site_uuid"])
