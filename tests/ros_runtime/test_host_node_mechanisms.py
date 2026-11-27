"""HostNode 与机构运行时的接缝：按设备不构造驱动、按时长执行、失败回报、跟随真实动作。"""
from __future__ import annotations

from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest

import unilabos.ros.nodes.presets.host_node as host_node_module
from unilabos.ros.nodes.presets.host_node import HostNode


class _Logger:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, message):
        self.messages.append(message)

    warning = error = trace = info


class _Mechanisms:
    """EdgeMechanisms 的替身：同步执行，记录调用。"""

    def __init__(self, *, direct=(), bound=(), outcome=None):
        self.direct, self.bound, self.outcome = set(direct), set(bound), outcome
        self.calls: list[tuple] = []

    def simulated(self, device_id):
        return device_id in self.direct

    def has(self, device_id, action):
        return (device_id, action) in self.bound

    def execute(self, device_id, action, parameters, *, on_success, on_failure):
        self.calls.append(("execute", device_id, action, dict(parameters)))
        if isinstance(self.outcome, tuple):
            on_failure(*self.outcome)
        else:
            on_success(self.outcome)

    def remember(self, job_id, device_id, action, parameters):
        self.calls.append(("remember", job_id, device_id, action, dict(parameters)))

    def job_started(self, job_id):
        self.calls.append(("started", job_id))

    def job_finished(self, job_id, success):
        self.calls.append(("finished", job_id, success))


class _Bridge:
    def __init__(self):
        self.started: list[str] = []
        self.status: list[tuple] = []

    def publish_job_started(self, item):
        self.started.append(item.job_id)

    def publish_job_status(self, data, item, status, return_info=None):
        self.status.append((item.job_id, status, data, return_info))


def _host(mechanisms, monkeypatch):
    bridge = _Bridge()
    host = SimpleNamespace(_mechanisms=mechanisms, bridges=[bridge], lab_logger=lambda: _Logger(),
                           _action_value_mappings={}, _action_clients={}, devices_instances={})
    for name in ("send_goal", "_build_simulated_action_return", "_handle_simulated_action_result",
                 "_handle_simulated_action_failure", "_publish_job_started"):
        setattr(host, name, MethodType(getattr(HostNode, name), host))
    monkeypatch.setattr("unilabos.app.edge_control.execution_start.confirm_execution_start", lambda item: None)
    monkeypatch.setattr("unilabos.app.web.controller.store_job_result", lambda *args: None)
    return host, bridge


def _item(device_id="freezer", action="BY_OpenDoor"):
    return SimpleNamespace(job_id=str(uuid4()), task_id=str(uuid4()), device_id=device_id, action_name=action,
                           trace_context={})


def test_direct_device_registers_catalog_only_while_other_devices_keep_real_drivers(monkeypatch):
    monkeypatch.setattr(host_node_module.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(host_node_module.BasicConfig, "action_mode", "real")
    simulated, initialized = [], []
    host = SimpleNamespace(
        _mechanisms=_Mechanisms(direct={"freezer"}),
        _register_simulated_device=lambda device_id, _config: simulated.append(device_id),
        initialize_device=lambda device_id, _config: initialized.append(device_id),
    )
    HostNode._initialize_configured_device(host, "freezer", SimpleNamespace())
    HostNode._initialize_configured_device(host, "centrifuge", SimpleNamespace())
    assert simulated == ["freezer"] and initialized == ["centrifuge"]


def test_direct_action_bound_to_mechanism_reports_started_once_then_evidence(monkeypatch):
    monkeypatch.setattr(host_node_module.BasicConfig, "action_mode", "real")
    evidence = {"evidence": "kinematic_readback", "duration_s": 4.0}
    mechanisms = _Mechanisms(direct={"freezer"}, bound={("freezer", "BY_OpenDoor")}, outcome=evidence)
    host, bridge = _host(mechanisms, monkeypatch)
    item = _item()
    host.send_goal(item, "UniLabJsonCommandAsync", {"timeout_tcp": 5000}, {})
    assert mechanisms.calls == [("execute", "freezer", "BY_OpenDoor", {"timeout_tcp": 5000})]
    assert bridge.started == [item.job_id]
    job_id, status, data, return_info = bridge.status[0]
    assert (job_id, status) == (item.job_id, "success") and data["mechanism"] == evidence
    assert data["action_mode"] == "simulate" and return_info["suc"] is True


def test_mechanism_rejection_is_reported_as_failed_job(monkeypatch):
    monkeypatch.setattr(host_node_module.BasicConfig, "action_mode", "real")
    mechanisms = _Mechanisms(direct={"turntable"}, bound={("turntable", "BY_Rotate")},
                             outcome=("target_out_of_range", "rotate_joint=5.2 超出资产声明范围"))
    host, bridge = _host(mechanisms, monkeypatch)
    item = _item("turntable", "BY_Rotate")
    host.send_goal(item, "UniLabJsonCommandAsync", {"angleEnum": 300}, {})
    job_id, status, data, return_info = bridge.status[0]
    assert status == "failed" and data["mechanism_error"] == "target_out_of_range"
    assert return_info["suc"] is False and "超出资产声明范围" in return_info["error"]


def test_direct_action_without_mechanism_binding_keeps_instant_mock_result(monkeypatch):
    monkeypatch.setattr(host_node_module.BasicConfig, "action_mode", "real")
    mechanisms = _Mechanisms(direct={"freezer"})
    host, bridge = _host(mechanisms, monkeypatch)
    item = _item(action="query_status")
    host.send_goal(item, "UniLabJsonCommandAsync", {}, {})
    assert mechanisms.calls == [] and bridge.started == [item.job_id]
    assert bridge.status[0][1] == "success" and "mechanism" not in bridge.status[0][2]


def test_driver_executed_action_is_remembered_before_dispatch_and_hooks_follow_it(monkeypatch):
    monkeypatch.setattr(host_node_module.BasicConfig, "action_mode", "real")
    mechanisms = _Mechanisms()
    host, _bridge = _host(mechanisms, monkeypatch)
    item = _item("centrifuge", "BY_OpenDoor")
    with pytest.raises(ValueError):  # 替身里没有 ROS 动作客户端；只验证派发前已经记下动作
        host.send_goal(item, "UniLabJsonCommandAsync", {"timeout_tcp": 1}, {})
    assert mechanisms.calls == [("remember", item.job_id, "centrifuge", "BY_OpenDoor", {"timeout_tcp": 1})]
