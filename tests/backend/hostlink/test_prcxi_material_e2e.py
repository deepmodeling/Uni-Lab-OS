"""PRCXI 9300 物料端到端：微后端权威创建 + HostLink 下行挂载（host + slave 双端）。

场景：服务端（微后端物料权威）依次创建 PRCXI 9300 deck（最小 2 槽）、
300ul tip rack、96 孔板（A1 预置 100ul Water），逐个经 HostLink 下行
RESOURCE_APPEND 挂载到 Slave 上的 prcxi 设备，双端断言。

物料通信与同步的关键调用路径::

    [A1] 已有定义的 deck：materials.ensure（adopt uuid，即开机图对齐语义）
        materials.ensure(deck, gateway=...)                    resources/materials.py
          -> gateway.get_material(root_uuid) 未命中
          -> resource_tree_to_create(tree, adopt_uuid=True)    resources/adapters/plr_materials.py
          -> gateway.create_tree -> MaterialsService.create_tree
             · 显式 uuid（adopt）：uuid 已占用即冲突（带条件创建）
             · deck 携带 canonical ResourceSite（T1/T2）直接落库
        => 权威中 deck uuid 与图/定义完全一致

    [A2] 新物料 tips / plate：materials.create（权威发 uuid）
        materials.create(plr, gateway=...)                     resources/materials.py
          -> create_plr_materials(gateway, mutation, [plr])    resources/adapters/plr_materials.py
             -> plr_resources_to_create: 校验草稿无 uuid -> MaterialTreeCreate
             -> gateway.create_tree -> MaterialsService.create_tree（权威发 uuid，
                模板缺失时按节点身份自动登记隐式模板 source=material_create）
             -> material_tree_to_resource_tree -> tree.to_plr_resources()
          => 返回带权威 uuid 的 PLR 根对象（输入草稿不被修改）

    [B] 下行挂载（Host -> Slave，纯 HostLink，无 ROS service）
        host.server.request_device(device_id, RESOURCE_APPEND, payload)   hostlink/server.py
          == TCP(JSON 行协议) ==>
        HostLinkBackend._handle_resource_append(data)          hostlink/backend.py（_start_slave 注册）
          -> run_node_coroutine(node, node.append_resource(payload))      runtime/async_utils.py

    [C] Slave 设备侧挂载  DeviceNode.append_resource            runtime/node.py
        1. self.get_resource(resources_uuid=[...])
             -> AuthorityResourceService.get_resources          runtime/resource.py
             -> HostLinkMaterialsClient.get_tree（上行 MATERIAL_GET_TREE）client/materials/core.py
             -> Host 侧 handler -> get_materials_gateway() -> MaterialsService.get_tree
        2. tree_set.to_plr_resources() 实例化 + resource_tracker.add_resource
           transfer_to_new_resource(plr, tree, params)
             -> _site_spot 解析目标位 -> parent.assign_child_resource(spot=...)
                · site 参数只承载权威 Site uuid（机器路径 / SiteSlot）
                · slot 参数是字符串：优先按 label 匹配（"A1"），纯数字
                  （isdigit）按 0-based 索引找对应 Site
           驱动回调 _invoke_resource_hook("resource_tree_add", ...)
        3. 物料挂物料（父不是设备本体）先落权威 move：
             -> AuthorityResourceService.move_resource           runtime/resource.py
             -> gateway.move_material（上行 MATERIAL_MOVE）
                destination_site_uuid：site 参数直传，slot/未指定时从挂载
                快照反查（_occupied_site_uuid）
             -> MaterialsService._apply_material_move：原子改 parent_material_uuid
                + 目标 Site occupied_material_uuid（满足 owner-descendant 约束），
                tips/plate 树并入 deck 权威树
        4. self.update_resource(挂载后的父树)（严禁 add，防与创建上报冲突）
             -> AuthorityResourceService.update_resources -> _update_sync
                -> 按权威 root 分组（move 后 tips/plate 归入 deck 根）
                -> gateway.get_tree(root) 基线
                -> resource_tree_to_snapshot(partial, base)     resources/adapters/plr_materials.py
                     · 设备 parent 被剥离；物料父子与权威 move 后一致
                     · position / data.substances / sites 按运行时覆盖
                -> gateway.compare_snapshot / apply_snapshot -> MaterialsService.apply_snapshot
                -> 乐观锁版本冲突（观察者快照并发）时重拉基线重试（_push_root_snapshot）

    [D] 换位（重复挂载同一 uuid，目标位变更）
        再次 RESOURCE_APPEND(tips, slot="2" 即 T3)：DeviceNode.append_resource
        发现 uuid 已在 resource_tracker -> 复用现有 PLR 实例（不重新实例化、
        跳过 resource_tree_add），transfer_to_new_resource 先
        old_parent.unassign_child_resource 再 assign 到新 spot；
        权威 move（MaterialsService._apply_material_move）原子完成：
        vacate 旧 Site(T1) -> parent 保持 deck -> occupy 新 Site(T3)。

    [E] 跨实例 transfer（deck1 -> deck2）
        再 ensure 一块 PRCXI_Deck_2 挂到同一设备，对 plate 再次
        RESOURCE_APPEND(bind=PRCXI_Deck_2, slot="S1")：同一 PLR 实例跨 deck
        迁移（液体状态随实例保留）；权威 move 跨权威树迁移 ——
        vacate deck1.T2 -> parent=deck2 -> occupy deck2.S1，
        plate 子树整体并入 deck2 权威树。

    [F] 台面状态演化 -> 显式同步（DeviceNode.update_resource）
        · materials.set_substance_on_target(plate.A2, Buffer 60ul)
        · 移液：TipSpot.get_tip + empty 拿起 tips.A1 枪头，
          plate.A1 tracker.remove_liquid(50) -> plate.A2 add_liquid(Water 50)，
          枪头不放回（废弃）
        · node.update_resource([deck1, deck2]) -> _update_sync 快照落库；
          枪头是 TipSpot 的结构孩子（独立 Tip 物料，带权威 uuid），不属于
          substances，也不再是孔位 data_json 里的 tip/pending_tip

    [G] 跨设备 transfer（slave->slave / slave->host / host->host 三形态）
        拓扑：host 进程自带两台本地设备（prcxi_host_a/b），另有两台独立
        slave（prcxi / prcxi_2，各自 TCP 连接）。Host 编排两步：
        1. 源设备 RESOURCE_TREE_SYNC(remove)
             -> DeviceNode.apply_resource_tree_update._handle_remove：
                unassign + tracker 移除（纯台面卸载，不动权威）；
             本进程设备直调节点协程，跨机经 HostLink RPC（同一分发语义）
        2. 目标设备 RESOURCE_APPEND（append_resource 四步）：
             get_resource 按 uuid 重新实例化（液体/tip 状态随权威恢复）
             -> assign -> 权威 move 原子迁移（vacate 源 Site + parent/occupy
             目标 Site，跨权威树归组）-> update_resource 快照
        plate 一路 deck2.S1 -> deck3.U1(slave2) -> deck4.H1(host_a)
        -> deck5.G1(host_b)，每跳双端断言 + 液体状态不丢。
        目标位三种形态全覆盖：slot="0"（数字字符串按 0-based 索引）、
        slot="G1"（label）、site=<ResourceSite.uuid>（权威 uuid / SiteSlot，
        _site_spot 解析本地 spot、move 直传 destination_site_uuid 免反查）；
        末段再做一次同设备指定 site uuid 换位（G1 -> G2）。

    [H] 权威侧最终事实（resource tracker / site 管理总核对）
        · 各端 tracker：顶级/归属与三轮 transfer 后物理事实一致，
          uuid_to_resources 与实例一一对应；源端 uuid 映射随 remove 清理
        · site：T1/T2 空、T3 == tips；S1/U1/H1/G1 均已释放，G2 == plate
        · plate.A1 == Water 50ul，A2 == Buffer 60 + Water 50（双端一致）；
          tips.A1 spot 下没有 Tip 物料（已废弃），其余 spot 各保留一支
        · slave 上行 get_tree 回读的 ResourceDict.substances 与权威一致
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.runtime.async_utils import run_node_coroutine
from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Deck
from unilabos.devices.liquid_handling.prcxi.prcxi_labware import (
    PRCXI_300ul_Tips,
    PRCXI_BioER_96_wellplate,
)
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.hostlink.protocol import ActionType
from unilabos.resources import materials
from unilabos.resources.objects.site import ResourceSite
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


DEVICE_ID = "prcxi"
DEVICE_UUID = "prcxi-device-uuid"
DEVICE2_ID = "prcxi_2"
DEVICE2_UUID = "prcxi-device-2-uuid"
HOST_DEV_A_ID = "prcxi_host_a"
HOST_DEV_A_UUID = "prcxi-host-a-uuid"
HOST_DEV_B_ID = "prcxi_host_b"
HOST_DEV_B_UUID = "prcxi-host-b-uuid"
LIQUID = ("Water", 100.0, "ul")


class _PrcxiDriver:
    """记录物料回调的最小 prcxi 设备驱动替身。"""

    def __init__(self, **_kwargs) -> None:
        self.added_batches: list[list] = []

    def resource_tree_add(self, resources) -> None:
        self.added_batches.append(list(resources))


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _build_deck(
    deck_uuid: str,
    name: str = "PRCXI_Deck",
    labels: tuple = ("T1", "T2", "T3"),
) -> PRCXI9300Deck:
    """最小化 PRCXI 9300 deck（图/定义形态，带 uuid 与横排槽位）。"""

    def _site(index: int, label: str, x: float) -> ResourceSite:
        return ResourceSite(
            uuid=str(uuid4()),
            template_name="PRCXI9300Deck",
            material_uuid=deck_uuid,
            index=index,
            label=label,
            pose={
                "position": {"x": x, "y": 0.0, "z": 0.0},
                "position3d": {"x": x, "y": 0.0, "z": 0.0},
                "size": {"width": 128.0, "height": 86.0, "depth": 0.0},
            },
            allowed_resource_categories=["plate", "tip_rack", "container"],
        )

    deck = PRCXI9300Deck(
        name=name,
        size_x=542.0,
        size_y=374.0,
        size_z=0.0,
        sites=[_site(i, label, 138.0 * i) for i, label in enumerate(labels)],
    )
    deck.unilabos_uuid = deck_uuid
    return deck


def _dispatch_device(
    host: HostLinkBackend, device_id: str, action_type: str, payload: dict
) -> dict:
    """Host 侧统一下行：本进程设备直调节点协程，跨机设备走 HostLink RPC。

    与生产分发（hostlink.downlink 的 notify_resource_tree_update 与
    *_to_device helpers）同语义。
    """

    node = host.local.devices.get(device_id)
    if node is None:
        assert host.server is not None
        return host.server.request_device(device_id, action_type, payload, timeout=10.0)
    data = {key: value for key, value in payload.items() if key != "device_id"}
    if action_type == ActionType.RESOURCE_APPEND:
        return run_node_coroutine(node, node.append_resource(data))
    if action_type == ActionType.RESOURCE_TREE_SYNC:
        return run_node_coroutine(
            node, node.apply_resource_tree_update(data["operations"])
        )
    raise AssertionError(f"未支持的下行类型: {action_type}")


def _append_via_hostlink(
    host: HostLinkBackend,
    resource_uuid: str,
    bind_parent_id: str,
    slot: str | None = None,
    site: str | None = None,
    device_id: str = DEVICE_ID,
) -> dict:
    """挂载下行。``slot`` 是字符串：优先按 label 匹配（如 "A1"），纯数字
    （isdigit）按 0-based 索引；``site`` 只承载权威 ResourceSite 的 uuid
    （二者互斥）。"""

    payload: dict = {
        "device_id": device_id,
        "resource_uuid": [resource_uuid],
        "bind_parent_id": bind_parent_id,
        "bind_location": {"x": 0.0, "y": 0.0, "z": 0.0},
    }
    assert not (slot is not None and site is not None)
    if slot is not None:
        payload["other_calling_param"] = {"slot": slot}
    elif site is not None:
        payload["other_calling_param"] = {"site": site}
    return _dispatch_device(host, device_id, ActionType.RESOURCE_APPEND, payload)


def _transfer_via_host(
    host: HostLinkBackend,
    resource_uuid: str,
    src_device_id: str,
    dst_device_id: str,
    bind_parent_id: str,
    slot: str | None = None,
    site: str | None = None,
) -> dict:
    """跨设备 transfer 编排（Host 视角）：

    1. 源设备 RESOURCE_TREE_SYNC(remove)：本地 unassign + tracker 移除
       （纯台面卸载，不动权威——权威迁移由目标挂载的 move 一步完成）；
    2. 目标设备 RESOURCE_APPEND：按 uuid 从权威拉取实例化 -> assign ->
       权威 move（vacate 源 Site + parent/occupy 目标 Site 原子迁移）-> 快照。
    """

    _dispatch_device(
        host,
        src_device_id,
        ActionType.RESOURCE_TREE_SYNC,
        {
            "device_id": src_device_id,
            "operations": [{"action": "remove", "data": [resource_uuid]}],
        },
    )
    return _append_via_hostlink(
        host, resource_uuid, bind_parent_id, slot=slot, site=site, device_id=dst_device_id
    )


def _site_by_label(service: MaterialsService, root_uuid: str, label: str):
    for node in service.get_tree(root_uuid).nodes:
        for site in node.sites:
            if site.label == label:
                return site
    raise AssertionError(f"权威树 {root_uuid} 中找不到 Site {label!r}")


def _normalize_liquids(liquids) -> list[tuple]:
    return [tuple(item) for item in liquids]


def test_prcxi_deck_tiprack_plate_e2e_host_plus_slave(tmp_path, monkeypatch) -> None:
    # ---- 服务端权威（微后端）：Host 进程持有 materials gateway ----
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)

    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 5.0)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    # host 进程：自带两个本地设备（host 2 host transfer 的两端）
    host_runtime = HostLinkLocalRuntime()
    node_host_a = host_runtime.add_driver(
        HostLinkDriverSpec(HOST_DEV_A_ID, _PrcxiDriver, {}, resource_uuid=HOST_DEV_A_UUID)
    )
    node_host_b = host_runtime.add_driver(
        HostLinkDriverSpec(HOST_DEV_B_ID, _PrcxiDriver, {}, resource_uuid=HOST_DEV_B_UUID)
    )
    host = HostLinkBackend(host_runtime, is_slave=False)
    # slave 1 / slave 2：两台独立“边缘机”（各自 TCP 连接）
    slave_runtime = HostLinkLocalRuntime()
    node = slave_runtime.add_driver(
        HostLinkDriverSpec(DEVICE_ID, _PrcxiDriver, {}, resource_uuid=DEVICE_UUID)
    )
    slave = HostLinkBackend(slave_runtime, is_slave=True)
    slave2_runtime = HostLinkLocalRuntime()
    node2 = slave2_runtime.add_driver(
        HostLinkDriverSpec(DEVICE2_ID, _PrcxiDriver, {}, resource_uuid=DEVICE2_UUID)
    )
    slave2 = HostLinkBackend(slave2_runtime, is_slave=True)

    try:
        host.start()
        assert host.server is not None
        HostLinkConfig.port = host.server.port
        monkeypatch.setattr(BasicConfig, "machine_name", "prcxi-slave")
        slave.start()
        monkeypatch.setattr(BasicConfig, "machine_name", "prcxi-slave-2")
        slave2.start()
        assert _wait_until(lambda: DEVICE_ID in host.devices())
        assert _wait_until(lambda: DEVICE2_ID in host.devices())

        # ================= 阶段 1：服务端创建已有定义的 deck（ensure/adopt） =================
        deck_uuid = str(uuid4())
        ensured = materials.ensure(_build_deck(deck_uuid), gateway=gateway)
        # adopt 语义：权威中的 uuid 与定义完全一致
        assert ensured.trees[0].root_node.res_content.uuid == deck_uuid
        deck_record = service.get_material(deck_uuid).material
        assert deck_record.parent_material_uuid is None
        assert deck_record.name == "PRCXI_Deck"
        for label in ("T1", "T2", "T3"):
            site = _site_by_label(service, deck_uuid, label)
            assert site.occupied_material_uuid is None

        # ================= 阶段 2：下行挂 deck 到 slave 设备 =================
        deck_result = _append_via_hostlink(host, deck_uuid, bind_parent_id=DEVICE_ID)
        assert deck_result["created_resource_tree"]

        deck_inst = node.resource_tracker.uuid_to_resources[deck_uuid]
        assert isinstance(deck_inst, PRCXI9300Deck)
        assert [site.label for site in deck_inst.sites] == ["T1", "T2", "T3"]
        # 驱动回调（resource_tree_add）收到 deck
        assert len(node.driver.added_batches) == 1
        assert deck_inst in node.driver.added_batches[0]
        # 设备挂载关系不写入权威物料父子字段：deck 仍是权威 root
        assert service.get_material(deck_uuid).material.parent_material_uuid is None

        # ================= 阶段 3：服务端创建 tip rack，下行挂 T1 =================
        tips_draft = PRCXI_300ul_Tips(name="tips_t1")
        authority_tips = materials.create(tips_draft, gateway=gateway)
        tips_uuid = authority_tips.unilabos_uuid
        assert service.get_material(tips_uuid).material.name == "tips_t1"

        tips_result = _append_via_hostlink(
            host, tips_uuid, bind_parent_id="PRCXI_Deck", slot="0"  # 数字字符串 -> 0-based -> T1
        )
        assert tips_result["created_resource_tree"]

        tips_inst = node.resource_tracker.uuid_to_resources[tips_uuid]
        assert tips_inst.parent is deck_inst
        # slot=1（1-based）等价 spot=0，即 T1
        assert deck_inst._ordering.get("T1") is tips_inst or (
            tips_inst in deck_inst.children
        )
        assert len(node.driver.added_batches) == 2
        # 权威侧：物料挂物料经 move 落库为真实父子 + Site 占用
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid
            == deck_uuid
        )
        assert (
            _site_by_label(service, deck_uuid, "T1").occupied_material_uuid
            == tips_uuid
        )

        # ================= 阶段 4：服务端创建 96 孔板（A1 预置液体），下行挂 T2 =================
        plate_draft = PRCXI_BioER_96_wellplate(name="plate_t2")
        well_draft = plate_draft.get_well("A1")
        materials.set_substance_on_target(well_draft, LIQUID[0], LIQUID[1])
        assert _normalize_liquids(well_draft.tracker.liquids) == [LIQUID]

        authority_plate = materials.create(plate_draft, gateway=gateway)
        plate_uuid = authority_plate.unilabos_uuid
        # 创建回执（权威 PLR 实例）中 A1 液体已随 data.substances 回读
        assert _normalize_liquids(
            authority_plate.get_well("A1").tracker.liquids
        ) == [LIQUID]
        # 权威库中 A1 节点的 substances 落库
        a1_uuid = authority_plate.get_well("A1").unilabos_uuid
        a1_node = next(
            item
            for item in service.get_tree(plate_uuid).nodes
            if item.material.material_uuid == a1_uuid
        )
        assert [
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in a1_node.data.substances
        ] == [LIQUID]
        # data_json（PLR 序列化的容器运行态：容量/当前体积等）随创建落库
        assert a1_node.data.data.get("max_volume") == 2200
        assert a1_node.data.data.get("volume") == 100.0
        # well 是 plate 树内子节点：物料树内父子走 parent_material_uuid
        assert a1_node.material.parent_material_uuid is not None

        plate_result = _append_via_hostlink(
            host, plate_uuid, bind_parent_id="PRCXI_Deck", slot="T2"  # label 形态
        )
        assert plate_result["created_resource_tree"]

        plate_inst = node.resource_tracker.uuid_to_resources[plate_uuid]
        assert plate_inst.parent is deck_inst
        assert len(node.driver.added_batches) == 3
        # slave 实例上的 A1 液体经 get_tree -> to_plr_resources 恢复
        assert _normalize_liquids(
            plate_inst.get_well("A1").tracker.liquids
        ) == [LIQUID]
        assert (
            service.get_material(plate_uuid).material.parent_material_uuid
            == deck_uuid
        )
        assert (
            _site_by_label(service, deck_uuid, "T2").occupied_material_uuid
            == plate_uuid
        )

        # ================= 阶段 5：tips 从 T1 换位到 T3（重复挂载=换位） =================
        relocate_result = _append_via_hostlink(
            host, tips_uuid, bind_parent_id="PRCXI_Deck", slot="2"  # 数字字符串 -> T3
        )
        assert relocate_result["created_resource_tree"]

        # slave：同一物理实例换位（不重新实例化）
        assert node.resource_tracker.uuid_to_resources[tips_uuid] is tips_inst
        assert tips_inst.parent is deck_inst
        # T3 site 的 x=276（T1 x=0），位置反映换位确实发生
        assert tips_inst.location is not None and tips_inst.location.x == 276.0
        # 换位不触发 resource_tree_add 回调（仍是 3 批）
        assert len(node.driver.added_batches) == 3
        # 权威：move 原子迁移 —— 释放 T1、占用 T3、parent 保持 deck
        assert _site_by_label(service, deck_uuid, "T1").occupied_material_uuid is None
        assert (
            _site_by_label(service, deck_uuid, "T3").occupied_material_uuid
            == tips_uuid
        )
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid
            == deck_uuid
        )

        # ================= 阶段 6：第二个 deck（跨实例 transfer 的目标） =================
        deck2_uuid = str(uuid4())
        materials.ensure(
            _build_deck(deck2_uuid, name="PRCXI_Deck_2", labels=("S1", "S2")),
            gateway=gateway,
        )
        deck2_result = _append_via_hostlink(host, deck2_uuid, bind_parent_id=DEVICE_ID)
        assert deck2_result["created_resource_tree"]
        deck2_inst = node.resource_tracker.uuid_to_resources[deck2_uuid]
        assert isinstance(deck2_inst, PRCXI9300Deck)
        assert [site.label for site in deck2_inst.sites] == ["S1", "S2"]

        # ================= 阶段 7：plate 跨实例 transfer（deck1.T2 -> deck2.S1） =================
        transfer_result = _append_via_hostlink(
            host, plate_uuid, bind_parent_id="PRCXI_Deck_2", slot="S1"  # label 形态
        )
        assert transfer_result["created_resource_tree"]
        # slave：同一实例跨 deck 迁移，deck1 侧脱离、deck2 侧就位
        assert node.resource_tracker.uuid_to_resources[plate_uuid] is plate_inst
        assert plate_inst.parent is deck2_inst
        assert plate_inst not in deck_inst.children
        assert plate_inst in deck2_inst.children
        # 液体随实例迁移不丢
        assert _normalize_liquids(plate_inst.get_well("A1").tracker.liquids) == [LIQUID]
        # 权威（site 管理）：deck1.T2 释放，deck2.S1 占用，parent 跨树迁到 deck2
        assert _site_by_label(service, deck_uuid, "T2").occupied_material_uuid is None
        assert (
            _site_by_label(service, deck2_uuid, "S1").occupied_material_uuid
            == plate_uuid
        )
        assert (
            service.get_material(plate_uuid).material.parent_material_uuid
            == deck2_uuid
        )

        # ================= 阶段 8：对已挂载物料 set substance 并显式同步 =================
        materials.set_substance_on_target(plate_inst.get_well("A2"), "Buffer", 60.0)
        asyncio.run(node.update_resource([deck2_inst]))
        a2_uuid = plate_inst.get_well("A2").unilabos_uuid
        a2_node = next(
            item
            for item in service.get_tree(deck2_uuid).nodes
            if item.material.material_uuid == a2_uuid
        )
        assert [
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in a2_node.data.substances
        ] == [("Buffer", 60.0, "ul")]

        # ================= 阶段 9：移液 A1 -> A2，废弃一个枪头 =================
        tip_spot = tips_inst.get_item("A1")
        # tip 初始状态（with_tips 填满）经权威 Tip 子物料 round-trip 保留到 slave
        assert tip_spot.has_tip()
        picked_tip = tip_spot.get_tip()  # 物理拿起枪头
        assert picked_tip is not None
        tip_spot.empty()  # spot 置空（pending 事务）
        tip_spot.tracker.commit()  # 物理动作完成后提交（LiquidHandler 同款语义）
        removed = plate_inst.get_well("A1").tracker.remove_liquid(50.0)  # 吸液
        assert removed == [("Water", 50.0, "ul")]
        plate_inst.get_well("A2").tracker.add_liquid("Water", 50.0)  # 分液
        # 枪头不放回（废弃），同步两块台面
        asyncio.run(node.update_resource([deck_inst, deck2_inst]))

        # slave 终态
        assert not tip_spot.has_tip()
        assert _normalize_liquids(plate_inst.get_well("A1").tracker.liquids) == [
            ("Water", 50.0, "ul")
        ]
        assert sorted(
            _normalize_liquids(plate_inst.get_well("A2").tracker.liquids)
        ) == [("Buffer", 60.0, "ul"), ("Water", 50.0, "ul")]
        # 权威终态：液体转移落库
        a1_after = next(
            item
            for item in service.get_tree(deck2_uuid).nodes
            if item.material.material_uuid == a1_uuid
        )
        assert [
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in a1_after.data.substances
        ] == [("Water", 50.0, "ul")]
        a2_after = next(
            item
            for item in service.get_tree(deck2_uuid).nodes
            if item.material.material_uuid == a2_uuid
        )
        assert sorted(
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in a2_after.data.substances
        ) == [("Buffer", 60.0, "ul"), ("Water", 50.0, "ul")]
        # 权威终态：枪头是孔位的结构孩子（独立 Tip 物料，维护版 PLR 起不再是孔位
        # data_json 里的 tip 状态）。其余孔位不受影响（抽查 A2），废弃枪头的孔位
        # 下不再有 Tip
        deck_nodes = service.get_tree(deck_uuid).nodes

        def _tip_children(spot) -> list[str]:
            return [
                item.material.material_uuid
                for item in deck_nodes
                if item.material.parent_material_uuid == spot.unilabos_uuid
                and item.material.resource_type == "tip"
            ]

        other_spot = tips_inst.get_item("A2")
        assert _tip_children(other_spot) == [other_spot.get_tip().unilabos_uuid]
        assert _tip_children(tip_spot) == [], (
            "废弃的枪头仍挂在权威 A1 孔位下：本地移出 PLR 树的 Tip 没有同步到权威"
            "（快照只能更新既有物料、不能删除），从权威恢复会多出一支枪头"
        )

        # ================= 阶段 10：各端再各备一块 deck（三种 transfer 的目标） =================
        deck3_uuid = str(uuid4())
        materials.ensure(
            _build_deck(deck3_uuid, name="PRCXI_Deck_3", labels=("U1", "U2")),
            gateway=gateway,
        )
        _append_via_hostlink(host, deck3_uuid, bind_parent_id=DEVICE2_ID, device_id=DEVICE2_ID)
        deck3_inst = node2.resource_tracker.uuid_to_resources[deck3_uuid]

        deck4_uuid = str(uuid4())
        materials.ensure(
            _build_deck(deck4_uuid, name="PRCXI_Deck_4", labels=("H1", "H2")),
            gateway=gateway,
        )
        _append_via_hostlink(host, deck4_uuid, bind_parent_id=HOST_DEV_A_ID, device_id=HOST_DEV_A_ID)
        deck4_inst = node_host_a.resource_tracker.uuid_to_resources[deck4_uuid]

        deck5_uuid = str(uuid4())
        materials.ensure(
            _build_deck(deck5_uuid, name="PRCXI_Deck_5", labels=("G1", "G2")),
            gateway=gateway,
        )
        _append_via_hostlink(host, deck5_uuid, bind_parent_id=HOST_DEV_B_ID, device_id=HOST_DEV_B_ID)
        deck5_inst = node_host_b.resource_tracker.uuid_to_resources[deck5_uuid]

        expected_liquids = {
            "A1": [("Water", 50.0, "ul")],
            "A2": [("Buffer", 60.0, "ul"), ("Water", 50.0, "ul")],
        }

        def _assert_plate_landed(dst_node, dst_deck_inst, dst_deck_uuid: str, site_label: str):
            """transfer 落点三方核对：目标 tracker/实例状态/权威 site+parent。"""
            landed = dst_node.resource_tracker.uuid_to_resources[plate_uuid]
            assert landed.parent is dst_deck_inst
            for well_label, liquids in expected_liquids.items():
                assert sorted(
                    _normalize_liquids(landed.get_well(well_label).tracker.liquids)
                ) == liquids
            assert (
                _site_by_label(service, dst_deck_uuid, site_label).occupied_material_uuid
                == plate_uuid
            )
            assert (
                service.get_material(plate_uuid).material.parent_material_uuid
                == dst_deck_uuid
            )
            return landed

        # ================= 阶段 11：slave -> slave transfer（deck2.S1 -> deck3.U1） =================
        _transfer_via_host(
            host, plate_uuid, src_device_id=DEVICE_ID, dst_device_id=DEVICE2_ID,
            bind_parent_id="PRCXI_Deck_3", slot="0",  # 数字字符串 -> U1
        )
        # 源端（slave1）：实例与 uuid 映射清理，deck2.S1 物理释放
        assert plate_uuid not in node.resource_tracker.uuid_to_resources
        assert plate_inst not in deck2_inst.children
        # 目标端（slave2）：新实例落位，液体状态经权威完整恢复
        plate_on_slave2 = _assert_plate_landed(node2, deck3_inst, deck3_uuid, "U1")
        # 权威：源 Site 释放
        assert _site_by_label(service, deck2_uuid, "S1").occupied_material_uuid is None

        # ================= 阶段 12：slave -> host transfer（deck3.U1 -> deck4.H1，指定 site uuid） =================
        h1_site_uuid = _site_by_label(service, deck4_uuid, "H1").site_uuid
        _transfer_via_host(
            host, plate_uuid, src_device_id=DEVICE2_ID, dst_device_id=HOST_DEV_A_ID,
            bind_parent_id="PRCXI_Deck_4", site=h1_site_uuid,
        )
        assert plate_uuid not in node2.resource_tracker.uuid_to_resources
        assert plate_on_slave2 not in deck3_inst.children
        plate_on_host_a = _assert_plate_landed(node_host_a, deck4_inst, deck4_uuid, "H1")
        assert _site_by_label(service, deck3_uuid, "U1").occupied_material_uuid is None

        # ================= 阶段 13：host -> host transfer（deck4.H1 -> deck5.G1） =================
        _transfer_via_host(
            host, plate_uuid, src_device_id=HOST_DEV_A_ID, dst_device_id=HOST_DEV_B_ID,
            bind_parent_id="PRCXI_Deck_5", slot="G1",  # label 形态
        )
        assert plate_uuid not in node_host_a.resource_tracker.uuid_to_resources
        assert plate_on_host_a not in deck4_inst.children
        plate_final = _assert_plate_landed(node_host_b, deck5_inst, deck5_uuid, "G1")
        assert _site_by_label(service, deck4_uuid, "H1").occupied_material_uuid is None

        # ================= 阶段 14：指定 site uuid 的同设备换位（deck5.G1 -> G2） =================
        g2_site_uuid = _site_by_label(service, deck5_uuid, "G2").site_uuid
        _append_via_hostlink(
            host, plate_uuid, bind_parent_id="PRCXI_Deck_5",
            site=g2_site_uuid, device_id=HOST_DEV_B_ID,
        )
        # 复用同一实例，落到 G2（x=138 反映 site uuid 确实解析到了第二槽位）
        assert node_host_b.resource_tracker.uuid_to_resources[plate_uuid] is plate_final
        assert plate_final.parent is deck5_inst
        assert plate_final.location is not None and plate_final.location.x == 138.0
        assert _site_by_label(service, deck5_uuid, "G1").occupied_material_uuid is None
        assert (
            _site_by_label(service, deck5_uuid, "G2").occupied_material_uuid
            == plate_uuid
        )

        # ================= 终局：resource tracker / site 管理双端总核对 =================
        # 各端 tracker：顶级/归属与三轮 transfer 后的物理事实一致
        top_level = {id(res) for res in node.resource_tracker.resources}
        assert {id(deck_inst), id(deck2_inst)} <= top_level
        assert id(tips_inst) not in top_level
        assert node.resource_tracker.uuid_to_resources[deck_uuid] is deck_inst
        assert node.resource_tracker.uuid_to_resources[deck2_uuid] is deck2_inst
        assert node.resource_tracker.uuid_to_resources[tips_uuid] is tips_inst
        assert tips_inst in deck_inst.children
        assert node2.resource_tracker.uuid_to_resources[deck3_uuid] is deck3_inst
        assert node_host_a.resource_tracker.uuid_to_resources[deck4_uuid] is deck4_inst
        assert node_host_b.resource_tracker.uuid_to_resources[plate_uuid] is plate_final
        assert plate_final in deck5_inst.children
        # site 管理：权威占用与各端实际排布逐位一致（plate 一路 S1->U1->H1->G1->G2）
        for owner_uuid, label, occupant in (
            (deck_uuid, "T1", None),
            (deck_uuid, "T2", None),
            (deck_uuid, "T3", tips_uuid),
            (deck2_uuid, "S1", None),
            (deck2_uuid, "S2", None),
            (deck3_uuid, "U1", None),
            (deck4_uuid, "H1", None),
            (deck5_uuid, "G1", None),
            (deck5_uuid, "G2", plate_uuid),
        ):
            assert (
                _site_by_label(service, owner_uuid, label).occupied_material_uuid
                == occupant
            )
        # 权威库 A1 的 data_json（容器运行态）经多轮快照/迁移后仍完整
        final_a1 = next(
            item
            for item in service.get_tree(deck5_uuid).nodes
            if item.material.material_uuid == a1_uuid
        )
        assert final_a1.data.data.get("max_volume") == 2200
        assert [
            (entry.name, entry.quantity, entry.quantity_unit)
            for entry in final_a1.data.substances
        ] == [("Water", 50.0, "ul")]
        # Slave 上行读回权威（HostLinkMaterialsClient 全链路）与本地一致
        remote_tree = asyncio.run(
            node.get_resource(resources_uuid=[plate_uuid], with_children=True)
        )
        remote_a1 = next(
            n
            for n in remote_tree.all_nodes
            if n.res_content.uuid == a1_uuid
        )
        # substances 是 ResourceDict 根字段（validator 从 data 提升规范化）
        assert [
            tuple(entry) for entry in (remote_a1.res_content.substances or [])
        ] == [("Water", 50.0, "ul")]
    finally:
        slave2.stop()
        slave.stop()
        host.stop()
        set_materials_gateway(None)
        service.close()
