"""ROS 2 Host：Slave 断开或不在 ROS 图上的设备离线——退出能力快照、收尾在途作业，重连后恢复。"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from unilabos.client.host.capabilities import build_endpoint_capabilities
from unilabos.client.host.execution_start import ExecutionNotStarted
from unilabos.client.host.hostlink import server as hostlink_server
from unilabos.client.host.ros2.presets.host_node import HostNode
from unilabos.registry.action_policy import SUCCESS_TYPE_CANCELLATION, SUCCESS_TYPE_EXECUTION_UNKNOWN

HOST_GRAPH = [("host_node", "/devices/host_node"), ("host_valve", "/devices/host_valve")]
SLAVE_GRAPH = [("slave_valve", "/devices/slave_valve")]


class Host:
    """只带 ROS 2 HostNode 离线判定所需状态的替身（不起 rclpy）。"""

    _discover_devices = HostNode._discover_devices
    _remote_devices_absent_from = HostNode._remote_devices_absent_from
    offline_device_ids = HostNode.offline_device_ids
    _hostlink_liveness_callback = HostNode._hostlink_liveness_callback
    _settle_jobs_on_offline_devices = HostNode._settle_jobs_on_offline_devices
    _take_settled = HostNode._take_settled
    _send_goal_ros2 = HostNode._send_goal_ros2

    def __init__(self, graph):
        self.graph = list(graph)
        self.devices_instances = {"host_node": object(), "host_valve": object()}
        self.devices_names = {"host_node": "/devices/host_node", "host_valve": "/devices/host_valve"}
        self._online_devices = set()
        self._action_value_mappings = {
            name: {"open": {"type": "EmptyIn"}} for name in ("host_node", "host_valve", "slave_valve")
        }
        self._action_clients = {}
        self._liveness_lock = threading.Lock()
        self._dds_offline_devices = set()
        self._hostlink_offline_devices = set()
        self._hostlink_owned_devices = set()
        self._goal_items = {}
        self._settled_jobs = set()
        self._goal_state_lock = threading.RLock()
        self._canceled_jobs = set()
        self._goals = {}
        self._inflight_goal_jobs = set()
        self.lab_logger = Mock()
        self._notify_capabilities_changed = Mock()
        self._create_action_clients_for_device = Mock()
        self._spawn_re_register = Mock()
        self._publish_terminal_result = Mock()

    def get_node_names_and_namespaces(self):
        return list(self.graph)


class FakeServer:
    def __init__(self) -> None:
        self.online = True
        #: None 表示 Slave 不带设备描述（旧版本）；True / False 是它报告的设备就绪状态
        self.ready = None

    def peers(self):
        peer = {"node_id": "slave-a", "online": self.online, "device_ids": ["slave_valve"]}
        if self.ready is not None:
            peer["devices"] = {"slave_valve": {"id": "slave_valve", "ready": self.ready}}
        return [peer]


def _snapshot_devices(host) -> set:
    routes, capabilities = build_endpoint_capabilities(host, observed_at_ms=1)
    assert {cap.device_uuid for cap in capabilities} <= {route.device_uuid for route in routes}
    return {route.device_uuid for route in routes}


def _settled(host) -> dict:
    return {call.args[0].job_id: call.args[2] for call in host._publish_terminal_result.call_args_list}


def _goal(job_id: str, device_id: str):
    return SimpleNamespace(job_id=job_id, device_id=device_id, action_name="open")


def test_remote_device_missing_from_ros_graph_leaves_snapshot_and_returns(monkeypatch) -> None:
    monkeypatch.setattr(hostlink_server, "get_hostlink_server", lambda: None)
    host = Host(HOST_GRAPH + SLAVE_GRAPH)
    host._discover_devices()
    assert _snapshot_devices(host) == {"host_node", "host_valve", "slave_valve"}

    host._goal_items["job-dds"] = _goal("job-dds", "slave_valve")
    # Slave 的节点从 ROS 图上消失（没经 HostLink 接入，只能按 DDS 判定）；本机设备不受影响
    host.graph = [("host_node", "/devices/host_node")]
    host._discover_devices()
    assert host.offline_device_ids == {"slave_valve"}
    assert _snapshot_devices(host) == {"host_node", "host_valve"}
    assert _settled(host)["job-dds"]["suc_type"] == SUCCESS_TYPE_EXECUTION_UNKNOWN

    host.graph = HOST_GRAPH + SLAVE_GRAPH
    host._discover_devices()
    assert host.offline_device_ids == set()
    assert _snapshot_devices(host) == {"host_node", "host_valve", "slave_valve"}


def test_hostlink_disconnect_takes_slave_devices_offline_and_settles_jobs(monkeypatch) -> None:
    server = FakeServer()
    monkeypatch.setattr(hostlink_server, "get_hostlink_server", lambda: server)
    host = Host(HOST_GRAPH + SLAVE_GRAPH)
    host._discover_devices()
    for job_id, device_id in (("job-run", "slave_valve"), ("job-cancel", "slave_valve"), ("job-host", "host_valve")):
        host._goal_items[job_id] = _goal(job_id, device_id)
    host._canceled_jobs.add("job-cancel")

    host._hostlink_liveness_callback()
    assert host.offline_device_ids == set()
    host._notify_capabilities_changed.reset_mock()

    # Slave 进程退出 / 被杀：HostLink 连接立刻断开，不等 DDS 租约
    server.online = False
    host._hostlink_liveness_callback()
    assert host.offline_device_ids == {"slave_valve"}
    host._notify_capabilities_changed.assert_called_once()
    assert _snapshot_devices(host) == {"host_node", "host_valve"}
    settled = _settled(host)
    assert settled["job-cancel"]["suc_type"] == SUCCESS_TYPE_CANCELLATION
    assert settled["job-run"]["suc_type"] == SUCCESS_TYPE_EXECUTION_UNKNOWN
    assert "job-host" not in settled and "job-host" in host._goal_items
    # 已收尾作业的迟到回调被忽略
    assert host._take_settled("job-run") is True
    assert host._take_settled("job-run") is False

    # DDS 仍看得到设备时（租约未过期），HostLink 断开已足以判离线；DDS 不再兜底收尾同一作业
    host._discover_devices()
    assert len(host._publish_terminal_result.call_args_list) == 2

    server.online = True
    host._hostlink_liveness_callback()
    assert host.offline_device_ids == set()
    assert _snapshot_devices(host) == {"host_node", "host_valve", "slave_valve"}


def test_restarted_slave_stays_offline_until_its_device_nodes_are_ready(monkeypatch) -> None:
    """Slave 进程重启后先接回 HostLink、后建设备节点：这段时间设备仍按离线算，派发在开始边界之前失败，
    而不是把 goal 发给还不存在的 Action server。两次检查之间断开又接回时，旧进程上的在途作业同样收尾。"""

    server = FakeServer()
    server.ready = True
    monkeypatch.setattr(hostlink_server, "get_hostlink_server", lambda: server)
    host = Host(HOST_GRAPH + SLAVE_GRAPH)
    host._discover_devices()
    host._hostlink_liveness_callback()
    assert host.offline_device_ids == set()
    host._goal_items["job-old"] = _goal("job-old", "slave_valve")

    server.ready = False
    host._hostlink_liveness_callback()
    assert host.offline_device_ids == {"slave_valve"}
    assert _snapshot_devices(host) == {"host_node", "host_valve"}
    assert _settled(host)["job-old"]["suc_type"] == SUCCESS_TYPE_EXECUTION_UNKNOWN
    item = SimpleNamespace(job_id=str(uuid4()), device_id="slave_valve", action_name="open", start_guard=Mock())
    with pytest.raises(ExecutionNotStarted, match="已离线"):
        host._send_goal_ros2(item, "EmptyIn", {}, {})

    server.ready = True
    host._hostlink_liveness_callback()
    assert host.offline_device_ids == set()
    assert _snapshot_devices(host) == {"host_node", "host_valve", "slave_valve"}


def test_down_managed_slave_device_gets_a_disabled_route_with_its_reason(monkeypatch) -> None:
    """所在受管 Slave 没在运行的设备：能力快照登记停用 route 并带原因；原因变了（在线集合没变）也重发快照。"""

    server = FakeServer()
    monkeypatch.setattr(hostlink_server, "get_hostlink_server", lambda: server)
    host = Host(HOST_GRAPH + SLAVE_GRAPH)
    host._discover_devices()
    host._hostlink_liveness_callback()
    record = {"device_id": "slave_valve", "registry_name": "valve", "backend": "ros2", "reason": "受管设备进程 slave_valve 意外退出"}
    host.unavailable_devices = {"slave_valve": record}

    server.online = False
    host._hostlink_liveness_callback()
    routes, capabilities = build_endpoint_capabilities(host, observed_at_ms=1)
    disabled = [route for route in routes if not route.enabled]
    assert [(route.device_uuid, route.config["unavailable"]["reason"]) for route in disabled] == [
        ("slave_valve", record["reason"])
    ]
    assert "slave_valve" not in {capability.device_uuid for capability in capabilities}

    host._notify_capabilities_changed.reset_mock()
    host.unavailable_devices = {"slave_valve": {**record, "reason": "受管设备进程 slave_valve 已自动重启 5 次仍退出，停止看护"}}
    host._hostlink_liveness_callback()
    host._notify_capabilities_changed.assert_called_once()

    # 设备回到线上后不再登记停用 route，即使原因记录还在
    server.online = True
    host._hostlink_liveness_callback()
    routes, _ = build_endpoint_capabilities(host, observed_at_ms=2)
    assert all(route.enabled for route in routes)


def test_dispatch_to_offline_device_fails_before_start_boundary(monkeypatch) -> None:
    server = FakeServer()
    monkeypatch.setattr(hostlink_server, "get_hostlink_server", lambda: server)
    host = Host(HOST_GRAPH + SLAVE_GRAPH)
    host._discover_devices()
    server.online = False
    host._hostlink_liveness_callback()

    guard = Mock()
    item = SimpleNamespace(job_id=str(uuid4()), device_id="slave_valve", action_name="open", start_guard=guard)
    with pytest.raises(ExecutionNotStarted, match="已离线"):
        host._send_goal_ros2(item, "EmptyIn", {}, {})
    guard.assert_not_called()
    assert item.job_id not in host._goal_items


def test_host_restart_restores_slave_action_mappings_from_local_registry(monkeypatch) -> None:
    """Host 重启后没有 Slave 首次上报的注册表；设备重注册只带 registry_name 时用本机注册表兜底。"""

    import json

    from unilabos.registry import registry as registry_module

    mappings = {"set_valve_position": {"type": "SendCmd"}, "open": {"type": "EmptyIn"}}
    monkeypatch.setattr(
        registry_module.lab_registry,
        "device_type_registry",
        {"virtual_solenoid_valve": {"class": {"action_value_mappings": mappings}}},
        raising=False,
    )

    class RestartedHost:
        _node_info_update_callback = HostNode._node_info_update_callback
        _local_registry_action_mappings = staticmethod(HostNode._local_registry_action_mappings)

        def __init__(self) -> None:
            self._slave_registry_configs = {}
            self._action_value_mappings = {}
            self.device_machine_names = {}
            self.lab_logger = Mock()
            self._notify_capabilities_changed = Mock()

    host = RestartedHost()
    request = SimpleNamespace(command=json.dumps({"SYNC_SLAVE_NODE_INFO": {
        "machine_name": "slave-a", "type": "slave", "edge_device_id": "slave_valve",
        "registry_name": "virtual_solenoid_valve",
    }}))
    response = SimpleNamespace(response="")
    host._node_info_update_callback(request, response)
    assert response.response == "OK"
    assert host._action_value_mappings["slave_valve"] == mappings
    assert host.device_machine_names["slave_valve"] == "slave-a"
    host._notify_capabilities_changed.assert_called_once()
