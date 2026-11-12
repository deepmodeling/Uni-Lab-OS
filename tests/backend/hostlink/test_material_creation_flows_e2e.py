"""物料三种创建来源 + transfer 换位的端到端（host + slave，纯 HostLink）。

全部走 ``materials.*`` 门面，关键调用路径如下::

    [1] Syncer：本地接管的外部物料系统（事实源在设备侧），实时上报
        注册+接管 materials.create(plr, node=node, gateway=HostLink client)
                  -> 上行 MATERIAL_CREATE_TREE -> MaterialsService.create_tree
                     （权威发 uuid；返回带 uuid 的权威实例，输入草稿不修改）
                  -> node= 自动登记 tracker
                     -> MaterialSnapshotObserver.observe（递归监听 state/assign）
        实时上报 本地 PLR 状态变化：VolumeTracker.add_liquid/remove_liquid
                + commit() 触发 state callback -> observer 以根为键合并排队
                  -> AuthorityResourceService 快照（compare/apply_snapshot）
                  -> 全程不调用 update_resource，轮询权威即可见
        （bioyond 等工作站 syncer 即此路径：外部系统事件 -> 本地 PLR 状态 -> 自动快照）

    [2] 本地正常创建 + assign 上台面
        materials.create("类名", name=..., node=node, gateway=...)
          -> registry 实例化草稿 -> 权威发 uuid -> node= 自动登记 tracker
        materials.assign(node, tips, parent="Bench_Deck", slot="T2")
          -> node.append_resource：uuid 已在 tracker -> 复用本地实例
             （不重复触发 resource_tree_add）-> transfer_to_new_resource assign
          -> 权威 move（parent_material_uuid + Site 占用）-> update_resource 快照

    [3] 微后端仓储系统扣减创建（host_node.apply_deduct_resource 同语义）
        分支 A  库存已在权威：materials.ensure(带 uuid 实体) -> gw.get_material
                命中 -> 直接采用权威树（不重复创建，version 不变）
                挂载走 Host 下发 RESOURCE_APPEND（跨机 RPC 与 assign 同语义）
        分支 B  扣减产物权威缺失：materials.ensure -> resource_tree_to_create(
                adopt_uuid=True) -> create_tree（显式 uuid，带条件创建=adopt）
                挂载走 materials.assign(node, uuid 字符串, ...)：本地未命中
                实例 -> get_resource 按 uuid 从权威加载 -> resource_tree_add

    [4] materials.transfer 换位（同设备 source==target，跨设备编排一致）
        materials.transfer(uuid, DEVICE_ID, deck_uuid, "T4", source_device_id=...)
          -> gateway.transfer_material -> MaterialsService.transfer_material
             （权威先落位：parent 不变 + T2 释放 / T4 占用）
          -> dispatcher 按 unload -> load 投影回设备：
             unload = 旧实例卸载（resource_tree_remove）
             load   = 权威拉取重建实例挂 T4（resource_tree_transfer + add）
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from unilabos.client.materials import HostLinkMaterialsClient, LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.hostlink.protocol import ActionType
from unilabos.resources import materials
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService
from tests.plr_fixtures import TIP_RACK_MODULE, SiteDeck, plate, registry_entry, site_deck, tip_rack


DEVICE_ID = "bench_flow"
DEVICE_UUID = "bench-flow-device-uuid"


class _BenchDriver:
    """记录物料回调的最小设备驱动替身。"""

    def __init__(self, **_kwargs) -> None:
        self.added_batches: list[list] = []
        self.removed_batches: list[list] = []

    def resource_tree_add(self, resources) -> None:
        self.added_batches.append(list(resources))

    def resource_tree_remove(self, resources) -> None:
        self.removed_batches.append(list(resources))


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _build_deck(deck_uuid: str) -> SiteDeck:
    return site_deck(deck_uuid, "Bench_Deck", ("T1", "T2", "T3", "T4"))


def _append_via_hostlink(
    host: HostLinkBackend,
    resource_uuid: str,
    bind_parent_id: str,
    slot: str | None = None,
) -> dict:
    payload: dict = {
        "device_id": DEVICE_ID,
        "resource_uuid": [resource_uuid],
        "bind_parent_id": bind_parent_id,
        "bind_location": {"x": 0.0, "y": 0.0, "z": 0.0},
    }
    if slot is not None:
        payload["other_calling_param"] = {"slot": slot}
    assert host.server is not None
    return host.server.request_device(
        DEVICE_ID, ActionType.RESOURCE_APPEND, payload, timeout=10.0
    )


def _site_by_label(service: MaterialsService, root_uuid: str, label: str):
    for node in service.get_tree(root_uuid).nodes:
        for site in node.sites:
            if site.label == label:
                return site
    raise AssertionError(f"权威树 {root_uuid} 中找不到 Site {label!r}")


def _substances_of(service: MaterialsService, root_uuid: str, material_uuid: str):
    """权威树中某节点当前落库的 substances 三元组（节点缺失返回 None）。"""

    node = next(
        (
            item
            for item in service.get_tree(root_uuid).nodes
            if item.material.material_uuid == material_uuid
        ),
        None,
    )
    if node is None:
        return None
    return [
        (entry.name, entry.quantity, entry.quantity_unit)
        for entry in node.data.substances
    ]


def _normalize_liquids(liquids) -> list[tuple]:
    return [tuple(item) for item in liquids]


def test_material_creation_flows_host_plus_slave(tmp_path, monkeypatch) -> None:
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

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave_runtime = HostLinkLocalRuntime()
    node = slave_runtime.add_driver(
        HostLinkDriverSpec(DEVICE_ID, _BenchDriver, {}, resource_uuid=DEVICE_UUID)
    )
    slave = HostLinkBackend(slave_runtime, is_slave=True)

    try:
        host.start()
        assert host.server is not None
        HostLinkConfig.port = host.server.port
        monkeypatch.setattr(BasicConfig, "machine_name", "bench-flow-slave")
        slave.start()
        assert _wait_until(lambda: DEVICE_ID in host.devices(), timeout=3.0)
        assert slave.client is not None
        # Slave 侧物料上行链路（生产 _start_slave 同款构造）
        slave_gateway = HostLinkMaterialsClient(slave.client)
        # 生产由 @resource AST 扫描注册；测试注入最小 registry 条目，
        # 供 materials.create("类名", ...) 路径实例化
        from unilabos.registry.registry import lab_registry

        monkeypatch.setitem(
            lab_registry.resource_type_registry,
            "test_tip_rack_300ul",
            registry_entry(TIP_RACK_MODULE),
        )

        # ---- 台面：ensure 一块 deck（T1/T2/T3）并挂到 slave 设备 ----
        deck_uuid = str(uuid4())
        materials.ensure(_build_deck(deck_uuid), gateway=gateway)
        _append_via_hostlink(host, deck_uuid, bind_parent_id=DEVICE_ID)
        deck_inst = node.resource_tracker.uuid_to_resources[deck_uuid]
        assert len(node.driver.added_batches) == 1  # deck 一批
        # 设备在权威里没有物料行（本测试不登记设备图）：台面保持根树，不报错
        assert service.get_material(deck_uuid).material.parent_material_uuid is None

        # ================= 场景 1：Syncer（本地接管外部物料系统，实时上报） =================
        # 外部系统出现一块板（本地草稿无 uuid，A1 已有 40ul Water）
        syncer_draft = plate("syncer_plate")
        materials.set_substance_on_target(syncer_draft.get_well("A1"), "Water", 40.0)

        # 注册+接管：slave 侧经 HostLink 上行创建，权威发 uuid；输入草稿不被
        # 修改；node= 自动登记 tracker（observer 随即开始监听，无需手动 add）
        created_plate = materials.create(
            syncer_draft, node=node, gateway=slave_gateway
        )
        assert created_plate is not syncer_draft
        syncer_uuid = created_plate.unilabos_uuid
        assert syncer_uuid
        assert service.get_material(syncer_uuid).material.name == "syncer_plate"
        a1_uuid = created_plate.get_well("A1").unilabos_uuid
        assert _substances_of(service, syncer_uuid, a1_uuid) == [("Water", 40.0, "ul")]

        # node= 已把权威实例登记进 tracker
        assert node.resource_tracker.uuid_to_resources[syncer_uuid] is created_plate
        observer = node.resource_tracker._material_snapshot_observer
        assert observer is not None

        # 外部系统事件 1：A2 出现 25ul Buffer —— 只动本地状态 + commit，
        # 不调用 update_resource，由 observer 自动快照上行
        a2_well = created_plate.get_well("A2")
        a2_uuid = a2_well.unilabos_uuid
        a2_well.tracker.add_liquid("Buffer", 25.0)
        a2_well.tracker.commit()
        assert _wait_until(
            lambda: _substances_of(service, syncer_uuid, a2_uuid)
            == [("Buffer", 25.0, "ul")]
        ), "observer 自动快照（A2 加液）未在时限内到达权威"

        # 外部系统事件 2：A1 被吸走 15ul（40 -> 25）
        created_plate.get_well("A1").tracker.remove_liquid(15.0)
        created_plate.get_well("A1").tracker.commit()
        assert _wait_until(
            lambda: _substances_of(service, syncer_uuid, a1_uuid)
            == [("Water", 25.0, "ul")]
        ), "observer 自动快照（A1 减液）未在时限内到达权威"
        assert observer.errors == ()
        # syncer 物料不占台面 Site，也不触发设备 resource_tree_add 回调
        assert len(node.driver.added_batches) == 1

        # ================= 场景 2：本地正常创建 + assign 上台面 =================
        # 按 registry 类名创建（后端发号回来实例化）；node= 自动登记 tracker
        local_tips = materials.create(
            "test_tip_rack_300ul",
            name="local_tips",
            node=node,
            gateway=slave_gateway,
        )
        tips_uuid = local_tips.unilabos_uuid
        assert node.resource_tracker.uuid_to_resources[tips_uuid] is local_tips

        # 挂载门面：与 Host 下发 RESOURCE_APPEND 同语义（node.append_resource）
        materials.assign(node, local_tips, parent="Bench_Deck", slot="T2")
        # 本地已持有 -> append_resource 复用同一实例，不重复 resource_tree_add
        assert node.resource_tracker.uuid_to_resources[tips_uuid] is local_tips
        assert local_tips.parent is deck_inst
        assert len(node.driver.added_batches) == 1
        # 权威：move 落父子 + Site 占用
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid == deck_uuid
        )
        assert (
            _site_by_label(service, deck_uuid, "T2").occupied_material_uuid
            == tips_uuid
        )

        # update 唯一汇聚点·设备形态：首参传 node（身份/网关取自 node，
        # node.update_resource 即本调用的 async 包装）。挂载后的物料按其
        # 所在根树（deck）投影提交并返回；快照幂等，与 observer 并发冲突
        # 时自动重拉基线重试，重复提交无害。
        updated = materials.update(node, local_tips)
        assert {
            tree.root_node.res_content.uuid for tree in updated.trees
        } == {deck_uuid}
        assert tips_uuid in set(updated.all_nodes_uuid)

        # ================= 场景 3：微后端仓储系统扣减创建 =================
        # -- 分支 A：库存已在权威（服务端建库存 -> 扣减单下发同 uuid 实体） --
        stock_draft = plate("stock_plate")
        materials.set_substance_on_target(stock_draft.get_well("A1"), "Water", 80.0)
        stock_plate = materials.create(stock_draft, gateway=gateway)  # 仓储库存
        stock_uuid = stock_plate.unilabos_uuid
        # update 唯一汇聚点·脚本形态：不传 node，显式身份/网关
        updated_stock = materials.update(
            stock_plate, source_device_id="warehouse", gateway=gateway
        )
        assert {
            tree.root_node.res_content.uuid for tree in updated_stock.trees
        } == {stock_uuid}
        stock_version = service.get_material(stock_uuid).material.version

        # apply_deduct_resource 第一步：ensure 命中已存在 -> 直接采用权威（不重复创建）
        ensured = materials.ensure(stock_plate, gateway=gateway)
        assert ensured.trees[0].root_node.res_content.uuid == stock_uuid
        assert service.get_material(stock_uuid).material.version == stock_version

        # 第二步：RESOURCE_APPEND 挂载（本地未命中 -> 从权威加载实例）
        _append_via_hostlink(host, stock_uuid, bind_parent_id="Bench_Deck", slot="T3")
        stock_inst = node.resource_tracker.uuid_to_resources[stock_uuid]
        assert stock_inst.parent is deck_inst
        assert len(node.driver.added_batches) == 2  # 新实例 -> resource_tree_add
        # 库存液体随权威 round-trip 恢复到台面实例
        assert _normalize_liquids(stock_inst.get_well("A1").tracker.liquids) == [
            ("Water", 80.0, "ul")
        ]
        assert (
            service.get_material(stock_uuid).material.parent_material_uuid == deck_uuid
        )
        assert (
            _site_by_label(service, deck_uuid, "T3").occupied_material_uuid
            == stock_uuid
        )

        # -- 分支 B：扣减产物权威缺失（云端扣减单带全树 uuid） -> adopt 创建 --
        deducted_uuid = str(uuid4())
        deducted_draft = tip_rack("deducted_tips")
        deducted_draft.unilabos_uuid = deducted_uuid
        for descendant in deducted_draft.get_all_children():
            descendant.unilabos_uuid = str(uuid4())
        ensured_b = materials.ensure(deducted_draft, gateway=gateway)
        # adopt 语义：权威中的 uuid 与扣减单完全一致
        assert ensured_b.trees[0].root_node.res_content.uuid == deducted_uuid
        assert service.get_material(deducted_uuid).material.name == "deducted_tips"

        # 挂载门面接受裸 uuid 字符串，并在本地未命中时从权威加载实例。
        materials.assign(
            node, deducted_uuid, parent="Bench_Deck", slot="T1"
        )
        deducted_inst = node.resource_tracker.uuid_to_resources[deducted_uuid]
        assert deducted_inst.parent is deck_inst
        assert len(node.driver.added_batches) == 3
        assert (
            service.get_material(deducted_uuid).material.parent_material_uuid
            == deck_uuid
        )
        assert (
            _site_by_label(service, deck_uuid, "T1").occupied_material_uuid
            == deducted_uuid
        )

        # ================= 场景 4：materials.transfer 换位（T2 -> T4） =================
        # 同设备 source==target；跨设备只是两个 device_id 不同，编排一致。
        # 权威先落位（parent 不变 + Site 迁移），随后 unload -> load 投影回设备。
        transfer_result = asyncio.run(
            materials.transfer(
                tips_uuid,  # 门面接受裸 uuid / PLR 实例
                DEVICE_ID,
                deck_uuid,
                "T4",  # 目标 Site 选择器：uuid / label / 数字索引
                source_device_id=DEVICE_ID,
                gateway=gateway,
            )
        )
        assert transfer_result["success"] is True
        # 权威：T2 释放、T4 占用，parent 保持 deck
        assert _site_by_label(service, deck_uuid, "T2").occupied_material_uuid is None
        assert (
            _site_by_label(service, deck_uuid, "T4").occupied_material_uuid
            == tips_uuid
        )
        assert (
            service.get_material(tips_uuid).material.parent_material_uuid == deck_uuid
        )
        # 设备投影：unload 卸载旧实例（resource_tree_remove），load 权威拉取重建
        moved_tips = node.resource_tracker.uuid_to_resources[tips_uuid]
        assert moved_tips is not local_tips
        assert moved_tips.parent is deck_inst
        assert node.driver.removed_batches[-1] == [local_tips]
        assert node.driver.added_batches[-1] == [moved_tips]

        # ---- 终局：三种来源的物料在权威/台面上共存且互不串扰 ----
        assert _site_by_label(service, deck_uuid, "T1").occupied_material_uuid == deducted_uuid
        assert _site_by_label(service, deck_uuid, "T4").occupied_material_uuid == tips_uuid
        assert _site_by_label(service, deck_uuid, "T3").occupied_material_uuid == stock_uuid
        # syncer 物料保持权威 root（不挂 deck），实时状态仍是最后一次自动快照
        assert service.get_material(syncer_uuid).material.parent_material_uuid is None
        assert _substances_of(service, syncer_uuid, a1_uuid) == [("Water", 25.0, "ul")]
        assert _substances_of(service, syncer_uuid, a2_uuid) == [("Buffer", 25.0, "ul")]
        assert observer.errors == ()
    finally:
        slave.stop()
        host.stop()
        set_materials_gateway(None)
        service.close()


def test_deck_mounted_on_device_by_action_becomes_child_of_device_material(tmp_path, monkeypatch) -> None:
    """设备动作里 ensure 台面再 ``materials.assign(node, deck_uuid)``（挂到设备自身）：

    设备在权威里有物料行（开机图 / 受管进程图对齐后的常态）时，台面要真正成为设备物料的
    子节点——前端设备卡片下才看得到它的 sites，``get_tree(设备)`` 也能带出台面。之后在
    台面上 create+assign 耗材、快照、transfer 换位都照常工作；台面重复挂载幂等。
    """
    from unilabos.protocol.materials import (
        InventoryMutation,
        MaterialDataWrite,
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialTreeCreate,
        ResourceTemplateWrite,
    )

    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)

    def _mutation(operation: str) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=str(uuid4()), effect_key=operation, operation=operation,
            actor_type="graph", actor_uuid="test",
        )

    device_uuid = str(uuid4())
    # 开机图对齐会落的设备物料行
    service.put_template(_mutation("put_template"), ResourceTemplateWrite(
        name="bench_demo", display_name="bench", resource_type="device", class_name="bench_demo",
    ))
    service.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="dev", material_uuid=device_uuid,
        identity=MaterialIdentityWrite(
            resource_id="bench", name="物料工作台", resource_type="device",
            class_name="bench_demo", template_name="bench_demo",
        ),
        data=MaterialDataWrite(data={}, sites_initialized=True, state_status="ready"),
    )]))

    local = HostLinkLocalRuntime()
    node = local.add_driver(HostLinkDriverSpec("bench", _BenchDriver, {}, resource_uuid=device_uuid))
    backend = HostLinkBackend(local, is_slave=False)
    backend.start()
    try:
        deck_uuid = str(uuid4())
        materials.ensure(_build_deck(deck_uuid), gateway=gateway)
        materials.assign(node, deck_uuid)  # prepare_bench 同款：挂到设备自身

        assert service.get_material(deck_uuid).material.parent_material_uuid == device_uuid
        device_tree = service.get_tree(device_uuid)
        assert [n.material.name for n in device_tree.nodes] == ["物料工作台", "Bench_Deck"]
        assert [s.label for n in device_tree.nodes for s in n.sites] == ["T1", "T2", "T3", "T4"]
        assert [m.material.name for m in service.list_materials(roots_only=True)] == ["物料工作台"]

        # 幂等：再挂一次不报错、父不变
        materials.assign(node, deck_uuid)
        assert service.get_material(deck_uuid).material.parent_material_uuid == device_uuid

        # 台面上的耗材照常：create + assign 上 T1，快照按设备根树提交，transfer 换到 T3
        from unilabos.registry.registry import lab_registry

        monkeypatch.setitem(
            lab_registry.resource_type_registry, "test_tip_rack_300ul", registry_entry(TIP_RACK_MODULE)
        )
        tips = materials.create("test_tip_rack_300ul", name="tips_r1", node=node)
        materials.assign(node, tips, parent="Bench_Deck", slot="T1")
        assert service.get_material(tips.unilabos_uuid).material.parent_material_uuid == deck_uuid
        assert _site_by_label(service, deck_uuid, "T1").occupied_material_uuid == tips.unilabos_uuid

        # 快照按"设备下的那棵树"（台面）分组提交，而不是把设备连同所有台面当一棵树
        updated = materials.update(node, tips)
        assert {t.root_node.res_content.uuid for t in updated.trees} == {deck_uuid}
        observer = node.resource_tracker._material_snapshot_observer
        assert observer is not None and observer.errors == ()

        # 观察者自动快照（孔位加液同款：本地 state 变化 + commit -> 自动上报）在台面挂设备后
        # 照常到达权威：hydrate_well 之前卡死就是因为根被解析成设备、快照集合对不上
        plate_r1 = materials.create(plate("plate_r1"), node=node)
        materials.assign(node, plate_r1, parent="Bench_Deck", slot="T2")
        well = plate_r1.get_well("A1")
        well.tracker.add_liquid("Buffer", 25.0)
        well.tracker.commit()
        assert _wait_until(
            lambda: _substances_of(service, deck_uuid, well.unilabos_uuid) == [("Buffer", 25.0, "ul")]
        ), "台面挂到设备后，观察者自动快照未到达权威"
        assert observer.errors == ()

        result = asyncio.run(materials.transfer(
            tips.unilabos_uuid, "bench", deck_uuid, "T3", source_device_id="bench", gateway=gateway,
        ))
        assert result["success"] is True
        assert _site_by_label(service, deck_uuid, "T1").occupied_material_uuid is None
        assert _site_by_label(service, deck_uuid, "T3").occupied_material_uuid == tips.unilabos_uuid
    finally:
        backend.stop()
        set_materials_gateway(None)
        service.close()
