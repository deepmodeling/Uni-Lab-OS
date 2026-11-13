from __future__ import annotations

import asyncio
from uuid import uuid4

from pylabrobot.resources import Coordinate
import pytest

from unilabos.backend.runtime import resource as resource_module
from unilabos.backend.runtime.resource import (
    AuthorityResourceService,
    MaterialSnapshotObserver,
)
from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import InventoryMutation, MaterialDelete
from unilabos.resources.adapters.plr_materials import SnapshotUuidMismatchError
from unilabos.server.services.materials import MaterialsService


def _container(name: str) -> RegularContainer:
    resource = RegularContainer(
        name=name,
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    resource.unilabos_extra = {
        "unilabos_resource_class": "authority-container"
    }
    return resource


def test_resource_service_create_get_and_partial_snapshot_update(tmp_path) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    parent = _container("parent")
    child = _container("child")
    parent.assign_child_resource(child, Coordinate(1, 2, 3))
    try:
        created = asyncio.run(
            service.create_resources("device-1", "device-uuid", parent)
        )
        assert not getattr(parent, "unilabos_uuid", "")
        authoritative_parent = created.resources[0]
        authoritative_child = authoritative_parent.children[0]
        parent_uuid = authoritative_parent.unilabos_uuid
        child_uuid = authoritative_child.unilabos_uuid

        authoritative_child.tracker.set_liquids(
            [("NaCl", 250.0, "ug")]
        )
        updated = asyncio.run(
            service.update_resources(
                "device-1",
                "device-uuid",
                authoritative_child,
            )
        )

        assert updated.all_nodes_uuid == [parent_uuid, child_uuid]
        stored_child = materials.get_material(child_uuid)
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in stored_child.data.substances
        ] == [("NaCl", 250.0, "ug")]

        downloaded = asyncio.run(
            service.get_resources(
                "device-1",
                [parent_uuid],
                with_children=False,
            )
        )
        assert downloaded.all_nodes_uuid == [parent_uuid]

        downloaded_by_id = service.get_resource_by_id_sync(
            "parent",
            with_children=True,
        )
        assert downloaded_by_id.all_nodes_uuid == [parent_uuid, child_uuid]

        deleted = service.delete_resources_sync(
            "device-1",
            "device-uuid",
            [parent_uuid],
        )
        assert set(deleted) == {parent_uuid, child_uuid}
        assert materials.list_materials() == []
    finally:
        materials.close()


def test_resource_service_has_no_implicit_runtime_store() -> None:
    assert not hasattr(resource_module, "ResourceStore")
    assert not hasattr(resource_module, "LocalResourceService")


def test_resource_service_accepts_internal_create_draft(tmp_path) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    draft = ResourceTreeSet.from_plr_resources(
        [_container("draft")], known_random_uuid=True
    )
    draft_uuid = draft.all_nodes_uuid[0]
    try:
        created = asyncio.run(
            service.create_resources("device-1", "device-uuid", draft)
        )
        assert created.tree.all_nodes_uuid != [draft_uuid]
        assert created.result.data.client_ref_map == {
            "node-0": created.tree.all_nodes_uuid[0]
        }
    finally:
        materials.close()


def test_snapshot_observer_diffs_the_complete_root_with_all_descendants(
    tmp_path,
) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    draft_root = _container("rack")
    draft_left = _container("left")
    draft_right = _container("right")
    draft_deep = _container("deep")
    draft_left.assign_child_resource(draft_deep, Coordinate(1, 1, 1))
    draft_root.assign_child_resource(draft_left, Coordinate(1, 2, 3))
    draft_root.assign_child_resource(draft_right, Coordinate(4, 5, 6))

    async def run() -> None:
        created = await service.create_resources(
            "device-1", "device-uuid", draft_root
        )
        root = created.resources[0]
        left, right = root.children
        deep = left.children[0]
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "device-1",
            device_uuid=lambda: "device-uuid",
            schedule=asyncio.create_task,
        )
        assert observer.observe(root) is True

        # 同一个 tick 修改两个不同深度的 child，只排一轮根树 snapshot。
        deep.tracker.set_liquids([("catalyst", 7.0, "ug")])
        right.tracker.set_liquids([("solvent", 12.0, "ul")])
        await observer.wait_idle()
        assert observer.errors == ()

        stored = materials.get_tree(root.unilabos_uuid)
        assert len(stored.nodes) == 4
        by_name = {node.material.name: node for node in stored.nodes}
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in by_name["deep"].data.substances
        ] == [("catalyst", 7.0, "ug")]
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in by_name["right"].data.substances
        ] == [("solvent", 12.0, "ul")]
        assert by_name["left"].material.parent_material_uuid == (
            by_name["rack"].material.material_uuid
        )
        assert by_name["deep"].material.parent_material_uuid == (
            by_name["left"].material.material_uuid
        )

        deep.rotate(z=15)
        await observer.wait_idle()
        rotated = materials.get_tree(root.unilabos_uuid)
        rotated_deep = next(
            node for node in rotated.nodes if node.material.name == "deep"
        )
        assert rotated_deep.position.rotation_z == 15.0
        assert "rotation" not in rotated_deep.data.data

        # 权威回灌期间的 PLR state 变化不允许反向形成 snapshot 回声。
        before_versions = {
            node.material.material_uuid: node.material.version
            for node in rotated.nodes
        }
        with observer.suppress_authority_projection():
            right.tracker.set_liquids([("authority", 1.0, "ul")])
        await asyncio.sleep(0)
        await observer.wait_idle()
        unchanged = materials.get_tree(root.unilabos_uuid)
        assert {
            node.material.material_uuid: node.material.version
            for node in unchanged.nodes
        } == before_versions

    try:
        asyncio.run(run())
    finally:
        materials.close()


class _CountingGateway:
    """记录每个网关方法被调用的次数：快照上行到底向服务器发了什么。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: dict[str, int] = {}

    def reset(self) -> None:
        self.calls.clear()

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return attr(*args, **kwargs)

        return counted


def _deck_with_wells(materials: MaterialsService, service: AuthorityResourceService):
    deck = _container("deck")
    for name in ("w1", "w2", "w3"):
        deck.assign_child_resource(_container(name), Coordinate.zero())
    created = asyncio.run(service.create_resources("device-1", "device-uuid", deck))
    return created.resources[0]


def test_snapshot_flush_uses_cached_baseline_and_local_diff(tmp_path) -> None:
    """设备实例是工作真相：flush 不为算归属 / 算 diff 读服务器。

    首次 flush 读一次基线；之后无变化的 flush 零请求，有变化的 flush 只有一次
    apply_snapshot，基线由返回值刷新。
    """
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    try:
        root = _deck_with_wells(materials, service)
        gateway.reset()

        # 首次：一次 get_tree 建基线，无变化不 apply；绝不逐节点 get_material
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))
        assert gateway.calls == {"get_tree": 1}

        # 无变化再 flush：零服务器调用
        gateway.reset()
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))
        assert gateway.calls == {}

        # 改一个孔：只有一次 apply_snapshot，不读基线、不 compare_snapshot
        gateway.reset()
        root.children[1].tracker.set_liquids([("water", 5.0, "ul")])
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))
        assert gateway.calls == {"apply_snapshot": 1}
        stored = materials.get_material(root.children[1].unilabos_uuid)
        assert [(s.name, s.quantity) for s in stored.data.substances] == [("water", 5.0)]

        # 基线已由 apply 结果刷新：紧接着的无变化 flush 仍是零请求
        gateway.reset()
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))
        assert gateway.calls == {}
    finally:
        materials.close()


def test_partial_update_derives_roots_locally(tmp_path) -> None:
    """materials.update(某个孔) 的根归属沿本地 .parent 推导，不再逐节点问权威。"""
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    try:
        root = _deck_with_wells(materials, service)
        gateway.reset()
        well = root.children[0]
        well.tracker.set_liquids([("buffer", 9.0, "ul")])

        service.update_resources_sync("device-1", "device-uuid", well)

        assert "get_material" not in gateway.calls
        assert gateway.calls["get_tree"] == 1 and gateway.calls["apply_snapshot"] == 1
        assert materials.get_material(well.unilabos_uuid).data.substances[0].name == "buffer"
    finally:
        materials.close()


def test_device_held_tree_set_derives_roots_locally(tmp_path) -> None:
    """append_resource 回报的台面树（根 parent 投影为本设备）：归属本地即可确定。

    设备之下那棵树就是快照根（与 ``_root_material_uuid`` 停在设备物料之下同一规则），
    不能逐节点 get_material 反查——带枪头的整台面有数百节点，经 HostLink 逐个往返
    会把一次挂载拖过下行超时。
    """
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    try:
        root = _deck_with_wells(materials, service)
        root.children[1].tracker.set_liquids([("water", 5.0, "ul")])
        tree_set = ResourceTreeSet.from_plr_resources([root])
        tree_set.trees[0].root_node.res_content.parent_uuid = "device-uuid"
        gateway.reset()

        service.update_resources_sync("device-1", "device-uuid", tree_set)

        assert gateway.calls == {"get_tree": 1, "apply_snapshot": 1}
        stored = materials.get_material(root.children[1].unilabos_uuid)
        assert [(s.name, s.quantity) for s in stored.data.substances] == [("water", 5.0)]
        assert materials.get_material(root.unilabos_uuid).material.parent_material_uuid is None
    finally:
        materials.close()


def test_stale_baseline_is_refetched_on_conflict_and_after_authority_moves(tmp_path) -> None:
    """外部改了权威（版本前进 / 结构变化）：靠 precondition 冲突和下行作废基线各重拉一次。"""
    from unilabos.protocol.materials import MaterialMove

    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    other = AuthorityResourceService(LocalMaterialsClient(materials))
    try:
        root = _deck_with_wells(materials, service)
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))

        # 另一方（前端 / 其它设备）推进了权威版本：本地基线过期 → 冲突 → 重拉 → 成功
        other_view = asyncio.run(other.get_resources("x", [root.unilabos_uuid], True))
        other_well = other_view.to_plr_resources()[0].children[2]
        other_well.tracker.set_liquids([("acid", 1.0, "ul")])
        other.update_resources_sync("other", "other-uuid", other_well)

        gateway.reset()
        root.children[0].tracker.set_liquids([("base", 2.0, "ul")])
        asyncio.run(
            service.update_resources(  # 默认重试路径（观察者路径把冲突交给上层重新冻结）
                "device-1", "device-uuid", root
            )
        )
        assert gateway.calls["apply_snapshot"] == 2 and gateway.calls["get_tree"] == 1
        stored = {n.material.name: n for n in materials.get_tree(root.unilabos_uuid).nodes}
        assert stored["w1"].data.substances[0].name == "base"
        # 设备持有的实例是这棵树的工作真相：绕过设备直写权威的 w3 被设备的整树快照
        # 覆盖回设备所见（外部改动应经下行投影到设备实例，而不是直写权威）
        assert stored["w3"].data.substances == []

        # 结构变化：一个孔被 move 走 → 本 service 的 move 作废基线；下一次 flush 重拉一次
        gateway.reset()
        moved = root.children[2]
        service.move_resource_sync(
            "device-1", "device-uuid", moved.unilabos_uuid, parent_material_uuid=None
        )
        root.unassign_child_resource(moved)
        asyncio.run(service.snapshot_resource_tree("device-1", "device-uuid", root))
        assert gateway.calls.get("get_tree") == 1
        assert {n.material.name for n in materials.get_tree(root.unilabos_uuid).nodes} == {
            "deck", "w1", "w2",
        }
    finally:
        materials.close()


def test_observer_reports_state_changes_as_node_deltas_and_structure_as_snapshot(
    tmp_path,
) -> None:
    """状态变化只上报变了的节点（增量）；assign / unassign 走整树快照。"""
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    root = _deck_with_wells(materials, service)

    async def run() -> None:
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "device-1",
            device_uuid=lambda: "device-uuid",
            schedule=asyncio.create_task,
        )
        observer.observe(root)
        gateway.reset()

        # 首次：缺版本表 → 拉一次根树；然后只发一个节点的增量
        root.children[1].tracker.set_liquids([("water", 5.0, "ul")])
        await observer.wait_idle()
        assert observer.errors == ()
        assert gateway.calls == {"get_tree": 1, "apply_delta": 1}
        stored = materials.get_material(root.children[1].unilabos_uuid)
        assert [(s.name, s.quantity) for s in stored.data.substances] == [("water", 5.0)]

        # 再改两个孔：版本表已就位，一次增量、零读取；台面等其它节点版本不动
        gateway.reset()
        root.children[0].tracker.set_liquids([("acid", 1.0, "ul")])
        root.children[2].tracker.set_liquids([("base", 2.0, "ul")])
        await observer.wait_idle()
        assert observer.errors == ()
        assert gateway.calls == {"apply_delta": 1}
        assert materials.get_material(root.unilabos_uuid).material.version == 1

        # 结构变化（挂一个新孔）：退回严格整树快照 —— 新节点不在权威树里，快照按漂移拒绝
        gateway.reset()
        stray = _container("stray")
        stray.unilabos_uuid = str(uuid4())
        root.assign_child_resource(stray, Coordinate.zero())
        await observer.wait_idle()
        assert "apply_delta" not in gateway.calls
        assert "apply_snapshot" not in gateway.calls  # 权威没有这个节点：整树快照按漂移拒绝，不落库
        # 增量之后整树基线已丢 → 重拉一次；发现漂移再重拉一次确认 → 共两次读
        assert gateway.calls.get("get_tree") == 2

    try:
        asyncio.run(run())
    finally:
        materials.close()


def test_delta_conflict_refreshes_versions_and_retries(tmp_path) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    other = AuthorityResourceService(LocalMaterialsClient(materials))
    try:
        root = _deck_with_wells(materials, service)
        well = root.children[0]
        well.tracker.set_liquids([("a", 1.0, "ul")])
        assert service.apply_node_deltas_sync("device-1", "device-uuid", root, [well]) is True

        # 另一方推进了同一个孔的版本
        view = asyncio.run(other.get_resources("x", [root.unilabos_uuid], True)).to_plr_resources()[0]
        view.children[0].tracker.set_liquids([("b", 2.0, "ul")])
        other.update_resources_sync("other", "other-uuid", view.children[0])

        gateway.reset()
        well.tracker.set_liquids([("c", 3.0, "ul")])
        assert service.apply_node_deltas_sync("device-1", "device-uuid", root, [well]) is True
        # 冲突 → 重拉一次根树刷新版本 → 第二次增量成功
        assert gateway.calls == {"apply_delta": 2, "get_tree": 1}
        assert materials.get_material(well.unilabos_uuid).data.substances[0].name == "c"

        # 权威已是该状态：no_change 不是错误
        gateway.reset()
        assert service.apply_node_deltas_sync("device-1", "device-uuid", root, [well]) is False
        assert gateway.calls == {"apply_delta": 1}
    finally:
        materials.close()


def test_strict_snapshot_rejects_a_child_only_partial_tree(tmp_path) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    parent = _container("parent")
    parent.assign_child_resource(_container("child"), Coordinate.zero())

    async def run() -> None:
        created = await service.create_resources(
            "device-1", "device-uuid", parent
        )
        with pytest.raises(
            ValueError,
            match="does not match downloaded material UUID set",
        ):
            await service.snapshot_resource_tree(
                "device-1",
                "device-uuid",
                created.resources[0].children[0],
            )

    try:
        asyncio.run(run())
    finally:
        materials.close()


def _deck_under_liquid_handler(service: AuthorityResourceService):
    """PLR 1.0 运行时形态：台面是权威物料根，但它的 PLR 父节点是没有 uuid 的 LiquidHandler。"""

    from pylabrobot.legacy.liquid_handling import LiquidHandler
    from pylabrobot.legacy.liquid_handling.backends.chatterbox import (
        LiquidHandlerChatterboxBackend,
    )
    from pylabrobot.resources import Deck

    draft = Deck(name="lh_deck", size_x=300, size_y=300, size_z=10)
    draft.unilabos_extra = {"unilabos_resource_class": "authority-deck"}
    for name in ("w1", "w2"):
        draft.assign_child_resource(_container(name), Coordinate.zero())
    deck = asyncio.run(
        service.create_resources("lh-1", "lh-device-uuid", draft)
    ).resources[0]
    handler = LiquidHandler(backend=LiquidHandlerChatterboxBackend(), deck=deck)
    assert deck.parent is handler and not getattr(handler, "unilabos_uuid", "")
    return handler, deck


def test_liquid_handler_deck_reports_state_deltas_to_the_authority(tmp_path) -> None:
    """FRAMEWORK_GAPS #27：台面挂在 LiquidHandler 之下时，增量上报的根是台面本身。

    根推导沿 ``.parent`` 停在最高的、带权威 uuid 的祖先；处理器不是物料，越过它会让
    每次增量都因“根物料缺少权威 uuid”被丢弃，液体变化永远到不了权威。
    """

    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    _handler, deck = _deck_under_liquid_handler(service)
    well = deck.children[1]

    async def run() -> None:
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "lh-1",
            device_uuid=lambda: "lh-device-uuid",
            schedule=asyncio.create_task,
        )
        assert observer.observe(deck) is True
        gateway.reset()
        well.tracker.set_liquids([("water", 42.0, "ul")])
        await observer.wait_idle()
        assert observer.errors == ()
        assert gateway.calls == {"get_tree": 1, "apply_delta": 1}

    try:
        asyncio.run(run())
        stored = materials.get_material(well.unilabos_uuid)
        assert [(s.name, s.quantity) for s in stored.data.substances] == [("water", 42.0)]
    finally:
        materials.close()


def test_liquid_handler_deck_partial_update_derives_root_locally(tmp_path) -> None:
    """同一规则用于 materials.update(孔位)：根归属本地即可确定，不逐节点反查权威。"""

    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    try:
        _handler, deck = _deck_under_liquid_handler(service)
        well = deck.children[0]
        well.tracker.set_liquids([("buffer", 9.0, "ul")])
        gateway.reset()

        service.update_resources_sync("lh-1", "lh-device-uuid", well)

        assert "get_material" not in gateway.calls
        assert gateway.calls == {"get_tree": 1, "apply_snapshot": 1}
        assert materials.get_material(well.unilabos_uuid).data.substances[0].name == "buffer"
    finally:
        materials.close()


def test_queued_state_delta_for_a_material_deleted_meanwhile_is_dropped(tmp_path) -> None:
    """排队中的状态上报晚于权威删除：按结构漂移放弃，不记为观察者错误（仓库库位测试曾因此偶发失败）。"""
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = _CountingGateway(LocalMaterialsClient(materials))
    service = AuthorityResourceService(gateway)
    try:
        root = _deck_with_wells(materials, service)
        well = root.children[1]
        # 先建版本表，再由别处在权威里删掉这个孔（本地实例还挂在台面上）
        well.tracker.set_liquids([("a", 1.0, "ul")])
        assert service.apply_node_deltas_sync("device-1", "device-uuid", root, [well]) is True
        materials.delete_material(
            InventoryMutation(command_uuid=str(uuid4()), effect_key="delete", operation="delete_material"),
            MaterialDelete(material_uuid=well.unilabos_uuid, recursive=True),
        )
        service._forget_tree(root.unilabos_uuid)

        well.tracker.set_liquids([("b", 2.0, "ul")])
        with pytest.raises(SnapshotUuidMismatchError):
            service.apply_node_deltas_sync("device-1", "device-uuid", root, [well])

        async def run() -> None:
            observer = MaterialSnapshotObserver(
                service,
                device_id=lambda: "device-1",
                device_uuid=lambda: "device-uuid",
                schedule=asyncio.create_task,
            )
            observer.observe(root)
            well.tracker.set_liquids([("c", 3.0, "ul")])
            await observer.wait_idle()
            assert observer.errors == ()

        asyncio.run(run())
    finally:
        materials.close()


def test_snapshot_freeze_torn_by_a_concurrent_structural_change_is_retried(tmp_path, monkeypatch) -> None:
    """冻结整树时另一线程正在移除 / 转移物料，Site 占用与子节点读到不同时刻：重新冻结，
    不记观察者错误，也不丢掉这次变更（仓库库位测试曾在满载时因此偶发失败）。"""
    materials = MaterialsService(tmp_path / "materials.db")
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    draft_root = _container("rack")
    draft_root.assign_child_resource(_container("left"), Coordinate(1, 2, 3))
    draft_root.assign_child_resource(_container("right"), Coordinate(4, 5, 6))
    real_freeze = ResourceTreeSet.from_plr_resources
    freezes: list[bool] = []

    def freeze_torn_once(resources, *args, **kwargs):
        freezes.append(True)
        if len(freezes) == 1:
            raise ValueError("Site s 引用的 occupied_material_uuid=m 不在物料树中")
        return real_freeze(resources, *args, **kwargs)

    async def run() -> None:
        created = await service.create_resources("device-1", "device-uuid", draft_root)
        root = created.resources[0]
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "device-1",
            device_uuid=lambda: "device-uuid",
            schedule=asyncio.create_task,
        )
        assert observer.observe(root) is True
        submitted = []
        real_submit = service.snapshot_resource_tree

        async def counting_submit(*args, **kwargs):
            submitted.append(len(freezes))
            return await real_submit(*args, **kwargs)

        monkeypatch.setattr(service, "snapshot_resource_tree", counting_submit)
        monkeypatch.setattr(resource_module.ResourceTreeSet, "from_plr_resources", staticmethod(freeze_torn_once))
        root.unassign_child_resource(root.children[1])
        await observer.wait_idle()
        assert observer.errors == ()
        assert submitted and submitted[0] >= 2  # 撕裂的那次没有提交，重新冻结后提交成功

    try:
        asyncio.run(run())
    finally:
        materials.close()
