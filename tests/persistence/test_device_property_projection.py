"""两种后端数据库使用相同的属性接受、历史投影和恢复合同。"""

from uuid import uuid4

from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore, LocalEdgeControlAuthority
from unilabos.app.scheduler.device_state import DeviceStateStore


def test_device_property_projection_uses_selected_database(database_mode, tmp_path):
    authority = LocalEdgeControlAuthority(LocalEdgeAuthorityStore(tmp_path / "authority.db"), api_key="test")
    history = DeviceStateStore(str(tmp_path / "properties.db"))
    try:
        registration = authority.store.register_session({
            "edge_key": "station", "instance_uuid": str(uuid4()),
            "devices": [{"local_id": "heater", "actions": []}],
        })
        authority.store.set_session_connected(registration["session_uuid"], True)
        for value in (25.0, 26.0):
            authority.store.update_device_status(registration["session_uuid"], "heater", {
                "online": True, "dispatch_block_reason": "", "unknown_command_ids": [],
                "status": {"temperature": value},
            })
        assert len(authority.store.pending_device_property_reports()) == 2
        authority.configure_device_state(history)
        assert [r["value"] for r in history.history("heater", "temperature")] == [26.0, 25.0]
        assert not authority.store.pending_device_property_reports()
    finally:
        history.close()
        authority.stop()
