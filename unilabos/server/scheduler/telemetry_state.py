"""把设备执行 bridge 的标量属性写入新 ``telemetry.db``。"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import uuid4

from unilabos.server.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventWrite,
)
from unilabos.server.services.telemetry import TelemetryService


class TelemetryDeviceStateProjection:
    """兼容执行 backend 的 ``set/latest_all`` 形状，不持有第二个 DB。"""

    def __init__(self, service: TelemetryService, *, endpoint_uuid: str):
        self.service = service
        self.endpoint_uuid = str(endpoint_uuid).strip()
        if not self.endpoint_uuid:
            raise ValueError("endpoint_uuid is required")
        self._source_epoch = str(uuid4())
        self._source_generation = 0
        self._source_sequence = 0
        self._lock = threading.RLock()

    def close(self) -> None:
        """Connection 由 ServerServices 组合根持有。"""

    def set(self, device_id: str, prop: str, value: Any) -> bool:
        now_ms = int(time.time() * 1000)
        with self._lock:
            current = self.service.get_device_state(self.endpoint_uuid, device_id)
            properties = dict(current.properties) if current is not None else {}
            if properties.get(prop) == value:
                return False
            properties[prop] = value
            next_sequence = self._source_sequence + 1
            event_uuid = str(uuid4())
            self.service.ingest_event(
                TelemetryEventWrite(
                    event_uuid=event_uuid,
                    endpoint_uuid=self.endpoint_uuid,
                    device_uuid=device_id,
                    source_epoch=self._source_epoch,
                    source_generation=self._source_generation,
                    source_sequence=next_sequence,
                    event_type="property_sample",
                    event_key=prop,
                    payload={"value": value},
                    observed_at_ms=now_ms,
                    received_at_ms=now_ms,
                ),
                device_state=DeviceStateSnapshot(
                    device_uuid=device_id,
                    state=dict(current.state) if current is not None else {},
                    properties=properties,
                    connection_state=(
                        current.connection_state if current is not None else "unknown"
                    ),
                    alarms=list(current.alarms) if current is not None else [],
                    observed_at_ms=now_ms,
                ),
            )
            self._source_sequence = next_sequence
            return True

    def latest_all(self) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for state in self.service.list_device_states(self.endpoint_uuid):
            result[state.device_uuid] = {
                name: {
                    "value": value,
                    "updated_at": state.observed_at_ms,
                }
                for name, value in state.properties.items()
            }
        return result


__all__ = ["TelemetryDeviceStateProjection"]
