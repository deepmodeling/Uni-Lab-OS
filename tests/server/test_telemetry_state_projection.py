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
        assert projection.set("pump", "temperature", 21.0) is True

        latest = projection.latest_all()
        assert latest["pump"]["temperature"]["value"] == 21.0
        assert latest["pump"]["pressure"]["value"] == 1.5
        assert projection.latest_for("pump")["temperature"] == {
            "value": 21.0,
            "value_type": "float",
            "updated_at": projection.latest_for("pump")["temperature"]["updated_at"],
        }
        assert [
            row["value"] for row in projection.history("pump", "temperature")
        ] == [21.0, 20.0]
        assert projection.history("pump", "temperature", limit=1)[0]["value"] == 21.0
        assert {row["property"] for row in projection.history_all()} == {
            "temperature",
            "pressure",
        }
        assert projection.stats() == {
            "devices": 1,
            "properties": 2,
            "history_rows": 3,
        }
        assert len(service.query_events()) == 3
        assert not (tmp_path / "device_state.db").exists()
    finally:
        service.close()
