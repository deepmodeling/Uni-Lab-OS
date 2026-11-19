from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_host_status_exposes_json_business_actions(monkeypatch) -> None:
    fake_host = SimpleNamespace(
        devices_names={},
        _online_devices=set(),
        device_machine_names={},
        _subscribed_topics=set(),
        _action_clients={},
        _action_value_mappings={
            "CONDUCTIVITY_STATION": {
                "station_status": {
                    "type": "UniLabJsonCommand",
                    "schema": {"description": "查询工站状态"},
                },
                "auto-close": {"type": "UniLabJsonCommand", "schema": {}},
                "legacy_action": {"type": "EmptyIn", "schema": {}},
            }
        },
        device_status={},
        device_status_timestamps={},
    )

    config_module = ModuleType("unilabos.config.config")
    config_module.BasicConfig = SimpleNamespace(is_host_mode=True)
    host_node_module = ModuleType("unilabos.ros.nodes.presets.host_node")
    host_node_module.HostNode = SimpleNamespace(get_instance=lambda _timeout: fake_host)
    action_utils_module = ModuleType("unilabos.app.web.utils.action_utils")
    action_utils_module.get_action_info = lambda client, full_name: {
        "client": client,
        "full_name": full_name,
    }

    monkeypatch.setitem(sys.modules, "unilabos.config.config", config_module)
    monkeypatch.setitem(sys.modules, "unilabos.ros.nodes.presets.host_node", host_node_module)
    monkeypatch.setitem(sys.modules, "unilabos.app.web.utils.action_utils", action_utils_module)

    module_path = ROOT / "unilabos" / "app" / "web" / "utils" / "host_utils.py"
    spec = importlib.util.spec_from_file_location("test_host_utils", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    info = module.get_host_node_info()

    assert info["business_actions"] == {
        "CONDUCTIVITY_STATION/station_status": {
            "device_id": "CONDUCTIVITY_STATION",
            "action_name": "station_status",
            "action_type": "UniLabJsonCommand",
            "description": "查询工站状态",
        }
    }
