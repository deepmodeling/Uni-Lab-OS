"""HostLink 内置 host 服务设备（host_node）的物料编排动作测试。

微前端单点动作与工作流画布在 HostLink 全栈下对 ``host_node`` 提交
apply_deduct_resource / set_substance / transfer_resource / discard_resource
等动作；本测试验证内置服务设备的注册（backend.start 自动注册、descriptor
带 action_value_mappings）与各动作对权威库、设备台面投影的实际效果：

- apply_deduct_resource 仅登记（ensure/adopt 落权威）与「扣减 + 下行挂载」；
- set_substance 从权威拉取实例、写内容物并快照回权威（data.substances 落库）；
- transfer_resource 经 materials.transfer 提交权威 move，微后端按
  unload(source=host_node，幂等跳过) -> load(目标设备) 收敛台面；
- discard_resource 权威销毁 + RESOURCE_TREE_SYNC(remove) 通知设备移除；
- manual_confirm（系统自带的通用人工确认）只读透传。

物料四动作的业务实现在 backend 无关的 host_material_actions（两种
backend 的 host_node 都是薄壳），本测试同时覆盖该共享层。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import HostLinkConfig
from unilabos.backend.runtime.async_utils import run_node_coroutine
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.host_services import (
    HOST_SERVICE_ACTIONS,
    register_host_services,
)
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.registry.registry import lab_registry
from unilabos.resources import materials
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials import MaterialNotFoundError
from tests.plr_fixtures import PLATE_MODULE, SiteDeck, plate, registry_entry, site_deck, tip_rack


DEV_A = "warehouse_a"
DEV_A_UUID = "warehouse-a-uuid"
DEV_B = "warehouse_b"
DEV_B_UUID = "warehouse-b-uuid"


class _WarehouseDriver:
    """记录物料回调的最小仓储设备替身。"""

    def __init__(self, **_kwargs) -> None:
        self.added_batches: list[list] = []

    def resource_tree_add(self, resources) -> None:
        self.added_batches.append(list(resources))


def _identity_mapping(*params: str) -> dict:
    return {"goal": {name: name for name in params}}


def _host_node_registry_entry() -> dict:
    """与 AST 扫描产物同构的最小 host_node 条目（goal 恒等映射）。"""

    return {
        "class": {
            "action_value_mappings": {
                "apply_deduct_resource": _identity_mapping(
                    "resource", "registry_class", "material_name",
                    "device_id", "mount_resource",
                    "bind_locations", "slot_on_deck",
                ),
                "set_substance": _identity_mapping(
                    "resource", "substance_names", "amounts", "slots", "is_solid",
                ),
                "discard_resource": _identity_mapping("resource", "device_id"),
                "transfer_resource": _identity_mapping(
                    "resource", "target_device", "mount_resource", "site",
                ),
                "manual_confirm": _identity_mapping(
                    "timeout_seconds", "assignee_user_ids",
                ),
                "test_resource": _identity_mapping(
                    "sample_uuids", "resource", "resources", "device", "devices",
                ),
                "test_latency": _identity_mapping(),
            }
        }
    }


def _build_deck(deck_uuid: str, name: str, labels: tuple = ("T1", "T2")) -> SiteDeck:
    return site_deck(deck_uuid, name, labels)


def _site_by_label(service: MaterialsService, root_uuid: str, label: str):
    for node in service.get_tree(root_uuid).nodes:
        for site in node.sites:
            if site.label == label:
                return site
    raise AssertionError(f"权威树 {root_uuid} 中找不到 Site {label!r}")


def _append_local(host: HostLinkBackend, device_id: str, payload: dict) -> dict:
    node = host.local.get_device(device_id)
    assert node is not None
    return run_node_coroutine(node, node.append_resource(payload))


def test_register_host_services_skipped_without_registry_entry(monkeypatch) -> None:
    monkeypatch.setitem(lab_registry.device_type_registry, "host_node", {})
    runtime = HostLinkLocalRuntime()
    backend = SimpleNamespace(local=runtime)
    assert register_host_services(backend) is None
    assert "host_node" not in runtime.devices


def test_host_services_visible_in_endpoint_capabilities(monkeypatch) -> None:
    """host_node 物料动作应进入 runtime.v1 能力快照（画布/单点动作目录）。"""
    from unilabos.backend.hostlink.host_node import HostNode
    from unilabos.server.backend.capabilities import build_endpoint_capabilities

    monkeypatch.setitem(
        lab_registry.device_type_registry, "host_node", _host_node_registry_entry()
    )
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)

    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(DEV_A, _WarehouseDriver, {}, resource_uuid=DEV_A_UUID)
    )
    host = HostLinkBackend(runtime, is_slave=False)
    adapter: HostNode | None = None
    try:
        host.start()
        adapter = HostNode("host_node", host)

        routes, capabilities = build_endpoint_capabilities(
            adapter, observed_at_ms=int(time.time() * 1000)
        )
        route_devices = {route.device_uuid for route in routes}
        assert {"host_node", DEV_A} <= route_devices

        host_actions = {
            cap.action_name: cap
            for cap in capabilities
            if cap.device_uuid == "host_node"
        }
        assert set(HOST_SERVICE_ACTIONS) <= set(host_actions)
        transfer = host_actions["transfer_resource"]
        assert transfer.descriptor["goal"]["site"] == "site"
    finally:
        if adapter is not None:
            adapter.stop()
            type(adapter).reset_state()
        host.stop()


def test_host_services_material_actions(tmp_path, monkeypatch) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)

    monkeypatch.setitem(
        lab_registry.device_type_registry, "host_node", _host_node_registry_entry()
    )
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 5.0)

    runtime = HostLinkLocalRuntime()
    node_a = runtime.add_driver(
        HostLinkDriverSpec(DEV_A, _WarehouseDriver, {}, resource_uuid=DEV_A_UUID)
    )
    node_b = runtime.add_driver(
        HostLinkDriverSpec(DEV_B, _WarehouseDriver, {}, resource_uuid=DEV_B_UUID)
    )
    host = HostLinkBackend(runtime, is_slave=False)
    try:
        host.start()

        # ---- backend.start 自动注册内置 host 服务设备，descriptor 带 schema ----
        services_node = runtime.get_device("host_node")
        assert services_node is not None
        descriptor = services_node.describe()
        assert set(descriptor["actions"]) == set(HOST_SERVICE_ACTIONS)
        assert set(descriptor["action_value_mappings"]) == set(HOST_SERVICE_ACTIONS)
        # 幂等：重复注册直接复用
        assert register_host_services(host) is services_node

        # ---- 准备台面：deckA -> devA，deckB -> devB（开机图对齐语义） ----
        deck_a_uuid, deck_b_uuid = str(uuid4()), str(uuid4())
        materials.ensure(_build_deck(deck_a_uuid, "Deck_A"), gateway=gateway)
        materials.ensure(_build_deck(deck_b_uuid, "Deck_B"), gateway=gateway)
        for device_id, deck_uuid in ((DEV_A, deck_a_uuid), (DEV_B, deck_b_uuid)):
            _append_local(
                host,
                device_id,
                {
                    "resource_uuid": [deck_uuid],
                    "bind_parent_id": device_id,
                    "bind_location": {"x": 0.0, "y": 0.0, "z": 0.0},
                },
            )
        deck_a_inst = node_a.resource_tracker.uuid_to_resources[deck_a_uuid]
        deck_b_inst = node_b.resource_tracker.uuid_to_resources[deck_b_uuid]
        assert isinstance(deck_a_inst, SiteDeck)
        assert isinstance(deck_b_inst, SiteDeck)
        # 根树上台面即登记归属（append_resource 写根 extra，快照落权威），
        # 之后 transfer/discard/deduct 的设备参数全部可自动推断
        assert materials.owner_device_of(deck_a_uuid, gateway=gateway) == DEV_A
        assert materials.owner_device_of(deck_b_uuid, gateway=gateway) == DEV_B

        # ---- apply_deduct_resource：仅登记（未指定挂载目标） ----
        # 输入语义与 HostNode 一致：服务端已扣减的物料（整树带权威 uuid）
        tips = materials.create(tip_rack("tips_out"), gateway=gateway)
        tips_uuid = tips.unilabos_uuid
        register_only = host.call_action(
            "host_node",
            "apply_deduct_resource",
            resource=tips,
            device_id="",
            mount_resource="",
        )
        assert register_only["created_resource_tree"]
        assert register_only["substance_resource_tree"] == []
        assert register_only["mount_resource"] == []
        assert service.get_material(tips_uuid).material.name == "tips_out"

        # ---- apply_deduct_resource：扣减 + 挂载到 devA 的 deckA T1 ----
        mounted = host.call_action(
            "host_node",
            "apply_deduct_resource",
            resource={"uuid": tips_uuid},
            device_id=DEV_A,
            mount_resource={"uuid": deck_a_uuid},
            slot_on_deck="0",
        )
        assert mounted["created_resource_tree"]
        assert mounted["mount_resource"]
        tips_inst = node_a.resource_tracker.uuid_to_resources[tips_uuid]
        assert tips_inst.parent is deck_a_inst
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid
            == deck_a_uuid
        )
        assert (
            _site_by_label(service, deck_a_uuid, "T1").occupied_material_uuid
            == tips_uuid
        )

        # ---- apply_deduct_resource：registry_class 现场创建（出库=创建）+ 挂载 ----
        # 动作路径无需前端先调 instantiate 端点：类名 + 实例名 -> 权威发号 -> 挂载
        monkeypatch.setitem(
            lab_registry.resource_type_registry,
            "test_deep_well_plate",
            registry_entry(PLATE_MODULE),
        )
        # device_id 免传：由挂载目标 deckA 的归属自动推断为 devA
        created_out = host.call_action(
            "host_node",
            "apply_deduct_resource",
            resource=None,
            registry_class="test_deep_well_plate",
            material_name="fresh_plate",
            mount_resource={"uuid": deck_a_uuid},
            slot_on_deck="T2",
        )
        assert created_out["created_resource_tree"]
        fresh_trees = materials.search("fresh_plate", gateway=gateway)
        assert len(fresh_trees) == 1
        fresh_uuid = fresh_trees[0].root_node.res_content.uuid
        fresh_inst = node_a.resource_tracker.uuid_to_resources[fresh_uuid]
        assert fresh_inst.parent is deck_a_inst
        assert (
            _site_by_label(service, deck_a_uuid, "T2").occupied_material_uuid
            == fresh_uuid
        )

        # resource 与 registry_class 互斥
        with pytest.raises(Exception, match="二选一"):
            host.call_action(
                "host_node",
                "apply_deduct_resource",
                resource={"uuid": tips_uuid},
                registry_class="test_deep_well_plate",
                material_name="dup",
            )

        # ---- set_substance：权威拉取 plate，写 A1 内容物并快照回权威 ----
        plate_sub = materials.create(plate("plate_sub"), gateway=gateway)
        plate_uuid = plate_sub.unilabos_uuid
        substance_result = host.call_action(
            "host_node",
            "set_substance",
            resource={"uuid": plate_uuid},
            substance_names=["Water"],
            amounts=[50.0],
            slots=["A1"],
        )
        assert substance_result["resource"]
        a1_uuid = plate_sub.get_well("A1").unilabos_uuid
        a1_node = next(
            item
            for item in service.get_tree(plate_uuid).nodes
            if item.material.material_uuid == a1_uuid
        )
        assert [
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in a1_node.data.substances
        ] == [("Water", 50.0, "ul")]

        # ---- transfer_resource：devA/deckA T1 -> devB/deckB T2（site=uuid） ----
        # 两端设备全部自动推断：来源 devA = tips 所在根树（deckA）的归属，
        # 目标 devB = 挂载目标 deckB 的归属——调用方只给物料与目标物料
        target_site = _site_by_label(service, deck_b_uuid, "T2")
        transferred = host.call_action(
            "host_node",
            "transfer_resource",
            resource={"uuid": tips_uuid},
            mount_resource={"uuid": deck_b_uuid},
            site=target_site.site_uuid,
        )
        assert transferred["result"]["material_uuids"] == [tips_uuid]
        assert transferred["result"]["source_device_id"] == DEV_A
        assert transferred["result"]["target_device_id"] == DEV_B
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid
            == deck_b_uuid
        )
        assert (
            _site_by_label(service, deck_b_uuid, "T2").occupied_material_uuid
            == tips_uuid
        )
        assert _site_by_label(service, deck_a_uuid, "T1").occupied_material_uuid is None
        # 来源设备台面：unload 发给真实持有者 devA（而非 host），本地实例已移除
        assert tips_uuid not in node_a.resource_tracker.uuid_to_resources
        # 目标设备台面：load 投影已实例化并挂到 deckB
        tips_on_b = node_b.resource_tracker.uuid_to_resources[tips_uuid]
        assert tips_on_b.parent is deck_b_inst
        # 转移后归属跟随新根树（deckB -> devB）
        assert materials.owner_device_of(tips_uuid, gateway=gateway) == DEV_B

        # ---- manual_confirm：只读透传附加字段 ----
        confirm = host.call_action(
            "host_node",
            "manual_confirm",
            timeout_seconds=1,
            assignee_user_ids=[],
            note="ok",
        )
        assert confirm == {"note": "ok"}

        # ---- discard_resource：权威销毁 + 通知台面移除（设备自动推断为 devB） ----
        discarded = host.call_action(
            "host_node",
            "discard_resource",
            resource={"uuid": tips_uuid},
        )
        assert discarded == {"code": 0, "uuids": [tips_uuid], "device_id": DEV_B}
        assert tips_uuid not in node_b.resource_tracker.uuid_to_resources
        with pytest.raises(MaterialNotFoundError):
            service.get_material(tips_uuid)
    finally:
        host.stop()
        set_materials_gateway(None)
