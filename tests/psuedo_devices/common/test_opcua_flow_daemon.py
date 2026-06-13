from __future__ import annotations

import json

from tests.psuedo_devices.common import opcua_flow_daemon
from tests.psuedo_devices.common.opcua_flow_daemon import FlowDaemon


class FakeNode:
    def __init__(self, value):
        self.value = value

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value


def test_write_action_logs_value_change(tmp_path, monkeypatch):
    flow_path = tmp_path / "flow.json"
    flow_path.write_text(json.dumps({"rules": []}), encoding="utf-8")
    daemon = FlowDaemon(
        url="opc.tcp://example/",
        object_name="FakeDevice",
        flow_path=flow_path,
        poll_interval=0.02,
        stop_requested=lambda: False,
    )
    rule = {
        "name": "toggle done",
        "trigger": {"node": "trigger", "value": True, "edge": "rising"},
        "actions": [{"write": {"node": "done", "value": True}}],
    }
    nodes = {
        "trigger": FakeNode(True),
        "done": FakeNode(False),
    }
    daemon.previous_values[daemon._rule_key(0, rule)] = False
    log_messages = []

    def capture_info(message, *args):
        log_messages.append(message % args if args else message)

    monkeypatch.setattr(opcua_flow_daemon.LOGGER, "info", capture_info)

    daemon._run_rule_if_triggered(0, rule, nodes)

    assert "写入 OPC UA 变量: node=done False -> True" in log_messages
