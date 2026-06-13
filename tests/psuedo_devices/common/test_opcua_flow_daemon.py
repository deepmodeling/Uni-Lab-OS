from __future__ import annotations

import logging
import json

from tests.psuedo_devices.common.opcua_flow_daemon import FlowDaemon


class FakeNode:
    def __init__(self, value):
        self.value = value

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value


def test_write_action_logs_value_change(tmp_path, caplog):
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

    with caplog.at_level(logging.INFO, logger="pseudo-opcua-flow-daemon"):
        daemon._run_rule_if_triggered(0, rule, nodes)

    assert "写入 OPC UA 变量: node=done False -> True" in caplog.text
