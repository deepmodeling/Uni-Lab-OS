"""Slave 经 HostLink 取 Host 的完整物理拓扑图（协议编译用），取不到时用本机启动图。

受管 Slave 只装配一个根设备的子树，跨子树的连线、别处的设备只在 Host 的图里；Slave 也可能在另一台
机器上，所以图只能经 HostLink 同步：HELLO / 心跳应答带版本标记，``graph.physical.get`` 给图。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import networkx as nx
import pytest
from pydantic import BaseModel

from unilabos.client.host.hostlink.backend import HostLinkBackend
from unilabos.client.host.hostlink.client import HostLinkClient
from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.hostlink.physical_graph import (
    CAPABILITY,
    attach_physical_graph_sync,
    physical_graph_from_payload,
    physical_graph_payload,
)
from unilabos.client.host.hostlink.protocol import PHYSICAL_GRAPH_REVISION, ActionType
from unilabos.client.host.hostlink.server import HostLinkServer
from unilabos.client.host.runtime import workstation_protocol
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.experiments import models as protocol_models
from unilabos.experiments.compile import action_protocol_generators

STATION_UUID, REACTOR_UUID, HEATER_UUID, WASTE_UUID = (str(uuid.uuid4()) for _ in range(4))


def _host_graph() -> nx.Graph:
    """Host 的完整图：工作站子树（station + reactor）、另一个根设备 heater_b、根级物料 waste，
    reactor—heater_b、reactor—waste 两条连线跨出了工作站子树。"""

    graph = nx.Graph()
    graph.add_node("station", uuid=STATION_UUID, type="device", parent=None)
    graph.add_node("reactor", uuid=REACTOR_UUID, type="container", parent="station", display_name="host label")
    graph.add_node("heater_b", uuid=HEATER_UUID, type="device", parent=None)
    graph.add_node("waste", uuid=WASTE_UUID, type="container", parent=None)
    graph.add_edge("reactor", "heater_b", type="physical", port={"reactor": "in", "heater_b": "out"})
    graph.add_edge("reactor", "waste", type="fluid", port={"reactor": "drain", "waste": "in"})
    return graph


def _station_subtree() -> nx.Graph:
    """受管 Slave 自己的启动图：只有工作站子树。"""

    graph = nx.Graph()
    graph.add_node("station", uuid=STATION_UUID, type="device", parent=None)
    graph.add_node("reactor", uuid=REACTOR_UUID, type="container", parent="station", display_name="local label")
    return graph


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture(autouse=True)
def _isolated_graphs(monkeypatch):
    """进程内的 Host / Slave 共用一份模块状态：每个用例从「本机启动图 = 工作站子树、没有 Host 图」开始。"""

    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", _station_subtree())
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph_revision", (None, None))
    monkeypatch.setattr(workstation_protocol, "_host_physical_setup_graph", None)
    monkeypatch.setattr(workstation_protocol, "_host_physical_setup_graph_revision", None)
    monkeypatch.setattr(workstation_protocol, "_merged_physical_setup_graph", None)
    yield


def _server(source=None) -> HostLinkServer:
    server = HostLinkServer("127.0.0.1", 0, heartbeat_timeout=5)
    if source is not None:
        server.physical_graph_source = source
    return server.start()


def _client(server: HostLinkServer, name: str = "slave-graph") -> HostLinkClient:
    return HostLinkClient(
        "127.0.0.1",
        server.port,
        machine_name=name,
        device_ids=["station"],
        heartbeat_interval=60,
        connect_timeout=2,
        request_timeout=2,
    )


def test_payload_round_trip_keeps_nodes_links_and_ports() -> None:
    graph = _host_graph()
    graph.nodes["reactor"]["substances"] = [("water", 5.0, "ml")]
    wire = physical_graph_payload(graph, "rev-1")
    received, revision = physical_graph_from_payload(wire)
    assert revision == "rev-1"
    assert sorted(received.edges) == sorted(graph.edges)
    assert received.edges["reactor", "heater_b"]["port"] == {"reactor": "in", "heater_b": "out"}
    assert received.nodes["waste"]["uuid"] == WASTE_UUID
    # 没有图：版本标记与图都为空
    assert physical_graph_payload(None, None) == {"revision": None, "graph": None}
    assert physical_graph_from_payload({"revision": None, "graph": None}) == (None, None)
    with pytest.raises(ValueError):
        physical_graph_from_payload(["not", "an", "object"])


def test_merge_keeps_local_attributes_and_drops_clashing_host_nodes() -> None:
    host = _host_graph()
    # Host 图里另有一个同名不同 uuid 的 "flask"（别处的物料），连着 heater_b
    host.add_node("flask", uuid=str(uuid.uuid4()), type="container")
    host.add_edge("flask", "heater_b", type="physical")
    local = _station_subtree()
    local.add_node("flask", uuid=str(uuid.uuid4()), type="container", parent="station")
    local.add_edge("flask", "reactor", type="physical")

    merged = workstation_protocol.merge_physical_setup_graphs(host, local)

    assert merged.has_edge("reactor", "heater_b") and merged.has_edge("reactor", "waste")
    assert merged.nodes["reactor"]["display_name"] == "local label"
    # 同名不同 uuid：留本机的，Host 那个节点的连线不跟过来
    assert merged.nodes["flask"]["uuid"] == local.nodes["flask"]["uuid"]
    assert merged.has_edge("flask", "reactor") and not merged.has_edge("flask", "heater_b")
    # Host 的图本身不被改动
    assert host.has_edge("flask", "heater_b")
    assert workstation_protocol.merge_physical_setup_graphs(host, None) is host


def test_slave_fetches_the_full_graph_over_hostlink() -> None:
    host_graph = _host_graph()
    server = _server(lambda: (host_graph, "rev-1"))
    client = _client(server)
    sync = attach_physical_graph_sync(client)
    local = workstation_protocol._physical_setup_graph
    try:
        assert client.connect_blocking(timeout=3)
        assert sync.wait(3)
        graph = workstation_protocol.physical_setup_graph()
        assert graph is not local
        # 跨子树的连线、别的根设备与根级物料都来自 Host 的图
        assert graph.has_edge("reactor", "heater_b") and graph.has_edge("reactor", "waste")
        assert graph.edges["reactor", "heater_b"]["port"]["heater_b"] == "out"
        # 两边都有的节点以本机启动图的属性为准
        assert graph.nodes["reactor"]["display_name"] == "local label"
        assert workstation_protocol.host_physical_setup_graph_revision() == "rev-1"
        assert workstation_protocol.physical_setup_graph() is graph  # 合并结果复用
        # HELLO 带上了能力声明，Host 的握手应答带版本标记
        assert CAPABILITY in server.peers()[0]["capabilities"]
        assert client.hello_info[PHYSICAL_GRAPH_REVISION] == "rev-1"
    finally:
        sync.close()
        client.close()
        server.stop()
    # 撤下同步后协议编译回到本机启动图
    assert workstation_protocol.physical_setup_graph() is local


def test_slave_refetches_when_the_host_announces_a_new_graph() -> None:
    graphs = {"current": (_host_graph(), "rev-1")}
    calls: list[str] = []
    server = _server(lambda: graphs["current"])
    original_get = server.handlers[ActionType.PHYSICAL_GRAPH_GET]

    def counting_get(data, peer):
        calls.append(graphs["current"][1])
        return original_get(data, peer)

    server.register_handler(ActionType.PHYSICAL_GRAPH_GET, counting_get)
    client = _client(server)
    sync = attach_physical_graph_sync(client)
    try:
        assert client.connect_blocking(timeout=3) and sync.wait(3)
        client.heartbeat_now()  # 版本没变：不重取
        assert calls == ["rev-1"]

        changed = _host_graph()
        changed.add_node("stirrer_c", uuid=str(uuid.uuid4()), type="device")
        changed.add_edge("reactor", "stirrer_c", type="physical")
        graphs["current"] = (changed, "rev-2")
        client.heartbeat_now()
        assert _wait_until(lambda: workstation_protocol.host_physical_setup_graph_revision() == "rev-2")
        assert workstation_protocol.physical_setup_graph().has_edge("reactor", "stirrer_c")
        assert calls == ["rev-1", "rev-2"]
    finally:
        sync.close()
        client.close()
        server.stop()


def test_old_host_without_the_graph_keeps_the_local_graph() -> None:
    """旧版 Host：握手 / 心跳应答没有版本标记，也不认 graph.physical.get。新 Slave 照常工作，只用本机图。"""

    server = _server(lambda: (_host_graph(), "rev-1"))
    server.handlers.pop(ActionType.PHYSICAL_GRAPH_GET)
    for action in (ActionType.HELLO, ActionType.PING):
        handler = server.handlers[action]
        server.register_handler(
            action,
            lambda data, peer, _handler=handler: {
                key: value for key, value in _handler(data, peer).items() if key != PHYSICAL_GRAPH_REVISION
            },
        )
    requested: list[str] = []
    dispatch = server.dispatch

    def recording_dispatch(message, peer_key):
        requested.append(str(message.get("action_type")))
        return dispatch(message, peer_key)

    server.dispatch = recording_dispatch
    client = _client(server)
    sync = attach_physical_graph_sync(client)
    local = workstation_protocol._physical_setup_graph
    try:
        assert client.connect_blocking(timeout=3)
        assert sync.wait(1) is False
        client.heartbeat_now()
        assert workstation_protocol.physical_setup_graph() is local
        assert ActionType.PHYSICAL_GRAPH_GET not in requested
    finally:
        sync.close()
        client.close()
        server.stop()


def test_host_without_a_graph_and_failed_fetches_keep_the_local_graph() -> None:
    state = {"graph": (None, None), "fail": True}
    server = _server(lambda: state["graph"])
    original_get = server.handlers[ActionType.PHYSICAL_GRAPH_GET]

    def flaky_get(data, peer):
        if state["fail"]:
            raise RuntimeError("graph source busy")
        return original_get(data, peer)

    server.register_handler(ActionType.PHYSICAL_GRAPH_GET, flaky_get)
    client = _client(server)
    sync = attach_physical_graph_sync(client)
    local = workstation_protocol._physical_setup_graph
    try:
        # Host 还没有图（hostlink Host 在装配设备图之前就接受握手）：只用本机图
        assert client.connect_blocking(timeout=3)
        assert client.hello_info[PHYSICAL_GRAPH_REVISION] is None
        assert sync.wait(1) is False and workstation_protocol.physical_setup_graph() is local

        # 图有了但取图失败：沿用本机图，下一次心跳再试
        state["graph"] = (_host_graph(), "rev-1")
        client.heartbeat_now()
        assert sync.wait(3) is False and workstation_protocol.physical_setup_graph() is local
        state["fail"] = False
        client.heartbeat_now()
        assert _wait_until(lambda: workstation_protocol.host_physical_setup_graph_revision() == "rev-1")
        assert workstation_protocol.physical_setup_graph().has_edge("reactor", "heater_b")
    finally:
        sync.close()
        client.close()
        server.stop()


def test_hello_and_ping_advertise_the_process_graph_revision(monkeypatch) -> None:
    """默认图源是本进程启动图：换图就换版本标记；没有图时为 null。"""

    server = HostLinkServer("127.0.0.1", 0)
    peer = server._touch_peer("p", ActionType.HELLO, {"machine_name": "slave-a"})
    first = server._handle_hello({}, peer)[PHYSICAL_GRAPH_REVISION]
    assert first and server._handle_ping({}, peer)[PHYSICAL_GRAPH_REVISION] == first
    workstation_protocol.set_physical_setup_graph(_host_graph())
    second = server._handle_ping({}, peer)[PHYSICAL_GRAPH_REVISION]
    assert second and second != first
    payload = server._handle_physical_graph_get({}, peer)
    assert payload["revision"] == second and len(payload["graph"]["nodes"]) == 4
    workstation_protocol.set_physical_setup_graph(None)
    assert server._handle_hello({}, peer)[PHYSICAL_GRAPH_REVISION] is None


def test_ros2_slave_network_client_fetches_the_graph_before_returning(monkeypatch) -> None:
    """ROS 2 Slave 也经 HostLink 取图：setup_slave_network_client 返回（rclpy.init 之前）时图已就位。"""

    from unilabos.client.host.hostlink import network

    server = _server(lambda: (_host_graph(), "rev-ros"))
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", server.port)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", False)
    monkeypatch.setattr(BasicConfig, "machine_name", "ros2-slave-graph")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    try:
        client, _domain = network.setup_slave_network_client(device_ids=["station"])
        assert client is not None and client.online
        assert workstation_protocol.host_physical_setup_graph_revision() == "rev-ros"
        assert workstation_protocol.physical_setup_graph().has_edge("reactor", "heater_b")
    finally:
        network.shutdown_slave_network_client()
        server.stop()
    assert workstation_protocol.host_physical_setup_graph_revision() is None


def test_disabled_hostlink_slave_compiles_against_the_local_graph(monkeypatch) -> None:
    from unilabos.client.host.hostlink import network

    monkeypatch.setattr(HostLinkConfig, "enable", False)
    assert network.setup_slave_network_client(device_ids=["station"]) == (None, None)
    assert workstation_protocol.physical_setup_graph() is workstation_protocol._physical_setup_graph


# ── 端到端（进程内 Host + Slave，真实 HostLink TCP）：Slave 上的工作站按 Host 的图编译协议 ──


class ReachHeaterProtocol(BaseModel):
    temp: float


def _reach_heater(G=None, **kwargs):
    """按物理拓扑图找跟 reactor 相连的加热设备；图里没有就编译失败（与真实编译器一致）。"""

    heaters = [node for node in G.neighbors("reactor") if G.nodes[node].get("type") == "device"] if G is not None else []
    if not heaters:
        raise ValueError("reactor 没有相连的加热设备（物理拓扑图里找不到）")
    return [{"device_id": heaters[0], "action_name": "heat", "action_kwargs": {"temp": kwargs["temp"]}}]


class StationDriver(WorkstationBase):
    def __init__(self, protocol_type=None, **kwargs) -> None:
        super().__init__(deck=None)
        self.protocol_type = protocol_type


class HeaterDriver:
    calls: list[float] = []

    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id

    def heat(self, temp: float) -> dict[str, float]:
        HeaterDriver.calls.append(temp)
        return {"reached": temp}


def test_slave_workstation_compiles_against_the_host_graph(monkeypatch) -> None:
    """工作站在 Slave 里、加热设备是 Host 那边的另一个根设备，连线只在 Host 的图里：
    Slave 经 HostLink 取到完整图后协议能编译，步骤经 Host 派给另一个根设备执行。"""

    monkeypatch.setattr(protocol_models, "ReachHeaterProtocol", ReachHeaterProtocol, raising=False)
    monkeypatch.setitem(action_protocol_generators, ReachHeaterProtocol, _reach_heater)
    monkeypatch.setattr(HeaterDriver, "calls", [])
    monkeypatch.setattr(BasicConfig, "test_mode", False)
    monkeypatch.setattr(BasicConfig, "machine_name", "graph-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.2)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 5.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 5.0)

    host_runtime = HostLinkLocalRuntime()
    host_runtime.add_driver(
        HostLinkDriverSpec(
            "heater_b", HeaterDriver, {}, action_names=("heat",),
            action_value_mappings={"heat": {"type": "UniLabJsonCommand"}},
        )
    )
    slave_runtime = HostLinkLocalRuntime()
    slave_runtime.add_driver(
        HostLinkDriverSpec(
            "station", StationDriver, {"protocol_type": ["ReachHeaterProtocol"]},
            action_names=("ReachHeaterProtocol",),
            action_value_mappings={"ReachHeaterProtocol": {"type": "ReachHeaterProtocol", "placeholder_keys": {}}},
        )
    )
    host = HostLinkBackend(host_runtime, is_slave=False)
    slave = HostLinkBackend(slave_runtime, is_slave=True)
    host.start()
    assert host.server is not None
    host_graph = _host_graph()
    host.server.physical_graph_source = lambda: (host_graph, "rev-e2e")
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        # Slave 启动时连上 Host 就取图，装配设备之前已经就位
        assert workstation_protocol.host_physical_setup_graph_revision() == "rev-e2e"
        assert _wait_until(lambda: "station" in host.devices())
        result = asyncio.run(slave.call_action_async("station", "ReachHeaterProtocol", temp=72.5))
        assert result["protocol_name"] == "ReachHeaterProtocol"
        assert result["step_results"][0]["result"]["reached"] == 72.5
        assert HeaterDriver.calls == [72.5]
    finally:
        slave.stop()
        host.stop()
    # Slave 停了：Host 图随之撤下
    assert workstation_protocol.host_physical_setup_graph_revision() is None
