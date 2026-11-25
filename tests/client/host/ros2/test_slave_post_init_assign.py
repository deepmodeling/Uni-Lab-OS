"""ROS 2 Slave：设备在 post_init 里 materials.assign，权威经 HostLink 回调本进程的 material.sync。

按 ``main_slave_run.slave()`` 的真实启动顺序跑（不初始化 rclpy）：Host 侧是真实的 HostLink
组网服务与物料权威，Slave 侧是 ``setup_slave_network_client`` 建的真实链路。ROS 设备节点建不起来
（没有 rclpy 上下文），换成同一 DeviceNode 契约的替身节点，并像 ``BaseROS2DeviceNode.__init__``
那样先登记进 ``registered_devices`` 再跑驱动的 post_init。

覆盖：设备还在 post_init 时 Slave 已能应答 material.sync、按 device_id 找得到这台设备；
assign 同步阻塞在设备自己的 post_init 里时不会等到自己；来源与目标都是调用方设备。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tests.plr_fixtures import plate, site_deck
from unilabos.client.host.hostlink.client import get_hostlink_client
from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.hostlink.network import (
    setup_host_network_service,
    setup_slave_network_client,
    shutdown_network_services,
)
from unilabos.client.host.hostlink.protocol import ActionType
from unilabos.client.host.ros2 import main_slave_run
from unilabos.client.host.ros2.base_device_node import registered_devices
from unilabos.client.host.runtime.resource import AuthorityResourceService
from unilabos.client.materials import HostLinkMaterialsClient, LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.resources import materials
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.server.services.materials import MaterialsService

DEVICE_ID = "bench"
TEMPLATE = "post_init_assign_bench"


class _Bench:
    """post_init 里把台面挂到自身，再把自己持有的板换到台面库位上（来源与目标都是自己）。"""

    deck_uuid = ""

    def __init__(self, **_kwargs: Any) -> None:
        self.deck: Any = None
        self.placed: Any = None

    def post_init(self, node: Any) -> None:
        # 台面在权威里不属于任何设备：来源设备按本设备算
        [self.deck] = materials.assign(node, self.deck_uuid)["resources"]
        # 本设备持有的板：同一台设备先 unload 再 load
        held = materials.create(plate("bench_plate"), node=node)
        [self.placed] = materials.assign(node, held, parent=self.deck_uuid, slot="T1")["resources"]


class _Executor:
    def add_node(self, _node: Any) -> None:
        pass

    def spin(self) -> None:
        pass


def _graph(device_uuid: str) -> ResourceTreeSet:
    device = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": DEVICE_ID,
                "uuid": device_uuid,
                "name": DEVICE_ID,
                "type": "device",
                "class": TEMPLATE,
                "template_name": TEMPLATE,
                "config": {},
                "data": {},
                "extra": {},
            }
        )
    )
    return ResourceTreeSet([ResourceTreeInstance(device)])


@pytest.fixture
def network(monkeypatch):
    shutdown_network_services()
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "advertise_ip", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.2)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 5.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 15.0)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", False)
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "off")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)
    monkeypatch.setattr(BasicConfig, "backend", "ros2")
    monkeypatch.setattr(BasicConfig, "machine_name", "post-init-slave")
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    for key in ("ROS_DOMAIN_ID", "ROS_AUTOMATIC_DISCOVERY_RANGE", "ROS_STATIC_PEERS", "ROS_DISCOVERY_SERVER"):
        monkeypatch.delenv(key, raising=False)
    yield
    shutdown_network_services()


def test_a_device_assigns_from_its_post_init_on_a_ros2_slave(tmp_path, monkeypatch, network) -> None:
    material_service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(material_service)
    host = setup_host_network_service(material_gateway=gateway)
    assert host is not None
    # Host 与 Slave 同在测试进程：Host 侧对 Slave 设备按跨机经 HostLink 下发
    # （material_sync_to_device 的跨机分支），不在本进程直调 Slave 的设备节点
    material_service.set_resource_sync_dispatcher(
        lambda command: host.server.request_device(
            command.device_id,
            ActionType.MATERIAL_SYNC,
            command.model_dump(mode="json", exclude_none=False),
            15.0,
        )
    )
    device_uuid, deck_uuid = str(uuid4()), str(uuid4())
    graph = _graph(device_uuid)
    apply_device_authority_state(graph.root_nodes[0], {"available_sites": []}, TEMPLATE, gateway=gateway)
    # 台面先在权威里（不属于任何设备），本地还没有它
    materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1", "T2")), gateway=gateway)
    _Bench.deck_uuid = deck_uuid

    # 与 app/main.py 一致：rclpy 之前先接入 HostLink（Slave 进程身份）
    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = host.server.port
    BasicConfig.is_host_mode = False
    client, _ = setup_slave_network_client(device_ids=[DEVICE_ID])
    assert client is not None and client.online
    # 跳过经 ROS service 报送节点信息（第 3 步），其余按 slave() 原样执行
    monkeypatch.setattr(BasicConfig, "slave_no_host", True)

    runtimes: list[HostLinkLocalRuntime] = []
    errors: list[BaseException] = []
    events: list[str] = []

    def initialize(device_id: str, device_config: Any) -> Any:
        runtime = HostLinkLocalRuntime()
        runtimes.append(runtime)
        node = runtime.add_driver(
            HostLinkDriverSpec(device_id, _Bench, {}, resource_uuid=device_config.res_content.uuid)
        )
        runtime.set_resource_service(AuthorityResourceService(HostLinkMaterialsClient(get_hostlink_client())))
        # 与 BaseROS2DeviceNode.__init__ 一致：节点先登记进 registered_devices，之后才跑驱动的 post_init
        monkeypatch.setitem(registered_devices, device_id, {"base_node_instance": node})
        events.append("post_init")
        try:
            runtime.start()
        except Exception as exc:  # noqa: BLE001 - 与 ROS2DeviceNode 一样：post_init 失败只记下，设备照常算初始化过
            errors.append(exc)
        return node

    monkeypatch.setattr(main_slave_run, "_init_rclpy", lambda *_args: None)
    monkeypatch.setattr(main_slave_run.rclpy, "__executor", _Executor(), raising=False)
    monkeypatch.setattr(main_slave_run, "_spin_forever", lambda _executor: None)
    monkeypatch.setattr(main_slave_run, "Node", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_slave_run, "initialize_device_from_dict", initialize)
    monkeypatch.setattr(main_slave_run, "spin_until_shutdown", lambda: events.append("spin"))
    monkeypatch.setattr(main_slave_run, "shutdown_ros_runtime", lambda *_args, **_kwargs: None)

    try:
        main_slave_run.slave(graph, graph)

        assert events == ["post_init", "spin"]
        assert errors == []
        [runtime] = runtimes
        node = runtime.devices[DEVICE_ID]
        driver = node.driver
        tracker = node.resource_tracker
        # 台面按权威重建挂到设备自身，板从设备自身换到了台面 T1
        assert tracker.uuid_to_resources[deck_uuid] is driver.deck
        assert driver.placed.parent is driver.deck
        assert material_service.get_material(deck_uuid).material.parent_material_uuid == device_uuid
        t1 = next(site for site in material_service.get_material(deck_uuid).sites if site.label == "T1")
        assert t1.occupied_material_uuid == driver.placed.unilabos_uuid
        assert [res.name for res in tracker.resources] == ["Bench_Deck"]
        # 设备建好之后才对 Host 报就绪
        assert host.server.peers()[0]["devices"][DEVICE_ID]["ready"] is True
    finally:
        for runtime in runtimes:
            runtime.stop()
        material_service.close()
