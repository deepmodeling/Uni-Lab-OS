"""执行 bridge 的设备属性只写入新 telemetry authority。"""

from __future__ import annotations

from unilabos.server.scheduler.telemetry_state import TelemetryDeviceStateProjection
from unilabos.server.services.telemetry import TelemetryService


def test_projection_persists_latest_properties_and_events(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    projection = TelemetryDeviceStateProjection(service, endpoint_uuid="host")
    try:
        assert projection.set("pump", "temperature", 20.0) is True
        assert projection.set("pump", "temperature", 20.0) is False
        assert projection.set("pump", "pressure", 1.5) is True

        latest = projection.latest_all()
        assert latest["pump"]["temperature"]["value"] == 20.0
        assert latest["pump"]["pressure"]["value"] == 1.5
        assert len(service.query_events()) == 2
        assert not (tmp_path / "device_state.db").exists()
    finally:
        service.close()
