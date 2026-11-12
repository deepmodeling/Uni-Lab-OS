"""Materials authority aggregation and protocol tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.protocol.materials import AggregatePrecondition, InventoryMutation
from unilabos.protocol.materials import (
    MaterialDataWrite,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialsService,
)


def _mutation(operation: str, *, command_uuid: str | None = None, **values):
    return InventoryMutation(
        command_uuid=command_uuid or str(uuid4()),
        effect_key=operation,
        operation=operation,
        **values,
    )


def _template(
    service: MaterialsService,
    template_uuid: str,
    name: str,
    *,
    with_site: bool = False,
):
    return service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid=template_uuid,
            name=name,
            display_name=name.title(),
            resource_type="container",
            class_name="Container",
            available_sites=(
                [{"index": 0, "label": "A1", "content_type": ["container"]}]
                if with_site
                else []
            ),
        ),
    )


def _node(
    ref: str,
    template_name: str,
    *,
    parent: str | None = None,
):
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        identity=MaterialIdentityWrite(
            resource_id=f"resource-{ref}",
            name=ref,
            resource_type="container",
            class_name="Container",
            template_name=template_name,
        ),
        data=MaterialDataWrite(
            substances=[
                MaterialSubstance(
                    name="NaCl",
                    quantity=2,
                    quantity_unit="ug",
                    physical_state="solid",
                )
            ]
        ),
    )


def test_template_and_material_tree_roundtrip_is_authoritative(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")
        request = MaterialTreeCreate(
            nodes=[
                _node("random-root", "deck"),
                _node("random-child", "tube", parent="random-root"),
            ],
        )
        command_uuid = str(uuid4())
        mutation = _mutation("create_material_tree", command_uuid=command_uuid)
        result = service.create_tree(mutation, request)

        assert result.replayed is False
        assert result.data.client_ref_map.keys() == {
            "random-root",
            "random-child",
        }
        assert result.data.root_material_uuid != "random-root"
        assert len(result.data.nodes) == 2
        assert result.data.nodes[1].material.parent_material_uuid == (
            result.data.root_material_uuid
        )
        assert result.data.nodes[1].data.substances[0].physical_state == "solid"
        assert result.data.nodes[0].sites[0].label == "A1"

        replay = service.create_tree(mutation, request)
        assert replay.replayed is True
        assert replay.data == result.data
    finally:
        service.close()


def test_list_templates_filters_by_name_and_skips_definitions_on_the_server(tmp_path) -> None:
    """存在性检查 / 按名取 uuid 由权威按 name 精确筛选，目录模式不带 definition。

    回归：Slave 为了确认一个模板存在，把全注册表 130 个模板连 definition（近 10MB）
    一起拉过 HostLink。
    """

    service = MaterialsService(tmp_path / "materials.db")
    try:
        service.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                template_uuid="deck-template",
                name="deck",
                class_name="Deck",
                definition={"config_info": [{"config": {"sites": [{"index": 0}]}}]},
            ),
        )
        _template(service, "tube-template", "tube")

        assert [item.name for item in service.list_templates()] == ["deck", "tube"]

        # 默认目录模式：name / uuid / hash 齐全，definition 不下发
        catalog = service.list_templates(name="deck")
        assert [item.template_uuid for item in catalog] == ["deck-template"]
        assert catalog[0].definition == {}
        assert catalog[0].definition_hash  # 变更判定仍可用

        full = service.list_templates(name="deck", include_definition=True)
        assert full[0].definition["config_info"][0]["config"]["sites"] == [{"index": 0}]
        assert full[0].definition_hash == catalog[0].definition_hash

        assert service.list_templates(name="Deck") == []  # 精确匹配，不做大小写折叠
        assert service.list_templates(name="missing") == []
    finally:
        service.close()


def test_material_display_name_defaults_and_patch(tmp_path) -> None:
    """display_name 根字段：缺省回退 name（与 device 的 id/display_name 约定一致），
    显式值保留，且可经 patch 单独修改。"""
    from unilabos.protocol.materials import MaterialPatch

    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")
        named = _node("child", "tube", parent="root")
        named.identity.display_name = "试管 A"
        request = MaterialTreeCreate(nodes=[_node("root", "deck"), named])
        result = service.create_tree(_mutation("create_material_tree"), request)

        root_node, child_node = result.data.nodes
        assert root_node.material.display_name == root_node.material.name == "root"
        assert child_node.material.display_name == "试管 A"

        patched = service.patch_material(
            _mutation("patch_material"),
            root_node.material.material_uuid,
            MaterialPatch(display_name="主甲板"),
        )
        assert patched.data.material.display_name == "主甲板"
        assert patched.data.material.name == "root"
    finally:
        service.close()


def test_create_tree_adopts_explicit_material_uuid(tmp_path) -> None:
    """显式 material_uuid 是「带条件的创建」：以调用方 uuid 落库，uuid 已占用即冲突。

    这是开机图物料对齐（materials.ensure）与出库扣减产物落库的服务端契约。
    """
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")
        root_uuid, child_uuid = str(uuid4()), str(uuid4())
        request = MaterialTreeCreate(
            nodes=[
                _node("root", "deck").model_copy(
                    update={"material_uuid": root_uuid}
                ),
                _node("child", "tube", parent="root").model_copy(
                    update={"material_uuid": child_uuid}
                ),
            ],
        )
        result = service.create_tree(_mutation("create_material_tree"), request)

        assert result.data.root_material_uuid == root_uuid
        assert result.data.client_ref_map == {
            "root": root_uuid,
            "child": child_uuid,
        }

        # 同 uuid 再次创建（新 mutation，非幂等重放）→ 冲突
        conflict = MaterialTreeCreate(
            nodes=[
                _node("root-again", "deck").model_copy(
                    update={"material_uuid": root_uuid}
                )
            ],
        )
        with pytest.raises(MaterialConflictError, match="already exists"):
            service.create_tree(_mutation("create_material_tree"), conflict)
    finally:
        service.close()


def test_create_tree_rejects_duplicate_explicit_uuid_in_one_tree() -> None:
    """同一棵创建树中显式 material_uuid 重复在协议层即被拒绝。"""
    duplicated = str(uuid4())
    with pytest.raises(ValueError, match="material_uuid"):
        MaterialTreeCreate(
            nodes=[
                _node("root", "deck").model_copy(
                    update={"material_uuid": duplicated}
                ),
                _node("child", "tube", parent="root").model_copy(
                    update={"material_uuid": duplicated}
                ),
            ],
        )


def test_position_update_checks_material_version(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "tube-template", "tube")
        created = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[_node("tube", "tube")]
            ),
        )
        material = created.data.nodes[0]
        updated = service.put_position(
            _mutation(
                "put_position",
                preconditions=[
                    AggregatePrecondition(
                        aggregate_type="material",
                        aggregate_uuid=material.material.material_uuid,
                        expected_version=1,
                    )
                ],
            ),
            material.material.material_uuid,
            MaterialPosition(position_x=1, position_y=2, position_z=3),
        )
        assert updated.data.material.version == 2
        assert updated.data.position.position_x == 1

        with pytest.raises(MaterialConflictError, match="expected 1"):
            service.put_position(
                _mutation(
                    "put_position",
                    preconditions=[
                        AggregatePrecondition(
                            aggregate_type="material",
                            aggregate_uuid=material.material.material_uuid,
                            expected_version=1,
                        )
                    ],
                ),
                material.material.material_uuid,
                MaterialPosition(position_x=4, position_y=5, position_z=6),
            )
    finally:
        service.close()


def test_move_clears_source_and_sets_destination_atomically(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")
        first = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    _node("deck-1", "deck"),
                    _node("tube", "tube", parent="deck-1"),
                ]
            ),
        )
        second = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[_node("deck-2", "deck")]
            ),
        )
        child_uuid = first.data.client_ref_map["tube"]
        source_site_uuid = first.data.nodes[0].sites[0].site_uuid
        destination_site_uuid = second.data.nodes[0].sites[0].site_uuid

        # 初次放入 source。
        service.move_material(
            _mutation("move_material"),
            MaterialMove(
                material_uuid=child_uuid,
                destination_site_uuid=source_site_uuid,
            ),
        )
        moved = service.move_material(
            _mutation("move_material"),
            MaterialMove(
                material_uuid=child_uuid,
                destination_site_uuid=destination_site_uuid,
            ),
        )

        assert moved.data.material.parent_material_uuid == (
            second.data.root_material_uuid
        )
        assert service.get_site(source_site_uuid).occupied_material_uuid is None
        assert (
            service.get_site(destination_site_uuid).occupied_material_uuid
            == child_uuid
        )
    finally:
        service.close()


def test_snapshot_diff_and_apply_increment_material_once(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "tube-template", "tube")
        created = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[_node("tube", "tube")]
            ),
        )
        node = created.data.nodes[0]
        changed_data = node.data.model_copy(
            update={
                "substances": [
                    MaterialSubstance(
                        substance_uuid=node.data.substances[0].substance_uuid,
                        name="NaCl",
                        quantity=5,
                        quantity_unit="ug",
                        physical_state="solid",
                    )
                ]
            }
        )
        observed_node = node.model_copy(
            update={
                "position": MaterialPosition(
                    position_x=10, position_y=20, position_z=30
                ),
                "data": changed_data,
            }
        )
        snapshot = MaterialSnapshot(
            root_material_uuid=created.data.root_material_uuid,
            nodes=[observed_node],
        )

        diff = service.compare_snapshot(snapshot)
        assert [(change.section, change.changed_fields) for change in diff.changes] == [
            ("position", ["position_x", "position_y", "position_z"]),
            ("data", ["substances"]),
        ]

        result = service.apply_snapshot(
            _mutation("apply_material_snapshot"), snapshot
        )
        updated = result.data.nodes[0]
        assert updated.material.version == 2
        assert updated.position_version == 2
        assert updated.data.version == 2
        assert updated.data.content_version == 2
        assert updated.data.substances[0].quantity == 5
        assert {
            item.aggregate_uuid for item in result.affected
        } == {updated.material.material_uuid}
    finally:
        service.close()


def test_recursive_delete_and_change_feed_are_aggregate_based(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")
        created = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    _node("deck", "deck"),
                    _node("tube", "tube", parent="deck"),
                ]
            ),
        )
        result = service.delete_material(
            _mutation("delete_material"),
            MaterialDelete(
                material_uuid=created.data.root_material_uuid,
                recursive=True,
            ),
        )

        assert set(result.data.deleted_material_uuids) == set(
            created.data.client_ref_map.values()
        )
        assert service.list_materials() == []
        changes = service.changes()
        assert changes[-1].delta["deleted"] is True
        assert not hasattr(changes[-1], "delta_json")
        assert service.acknowledge_changes(changes[-1].sequence) == len(changes)
        assert all(item.delivery_status == "acknowledged" for item in service.changes())

        # 软删除不该让 resource_id 永久占坑：同名（同 resource_id / 同根名）的物料可以重建。
        # 设备 provision 一类动作重试、演示脚本重跑都会这样重建同名耗材。
        recreated = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(nodes=[_node("deck", "deck"), _node("tube", "tube", parent="deck")]),
        )
        assert recreated.data.root_material_uuid != created.data.root_material_uuid
        assert service.get_material_by_resource_id("resource-deck").material.material_uuid == (
            recreated.data.root_material_uuid
        )
        assert [m.material.name for m in service.list_materials(roots_only=True)] == ["deck"]
    finally:
        service.close()


def test_template_sync_records_create_and_update_but_never_a_no_op(tmp_path) -> None:
    """模板登记是物料变更：新建 / 定义变更各落一条账本；内容相同的重复写入被拒绝，
    不产生账本行（前端"物料变更"以账本为源，重启重放不能刷屏）。"""
    from unilabos.protocol.materials import ACTOR_REGISTRY
    from unilabos.server.services.materials import MaterialNoChangeError

    service = MaterialsService(tmp_path / "materials.db")
    try:
        write = ResourceTemplateWrite(
            template_uuid=None, name="plate", resource_type="container", class_name="Plate"
        )
        synced = service.put_template(
            _mutation("sync_template", actor_type=ACTOR_REGISTRY, actor_uuid="plate"), write
        )
        same = write.model_copy(update={"template_uuid": synced.data.template_uuid})
        with pytest.raises(MaterialNoChangeError):
            service.put_template(
                _mutation("sync_template", actor_type=ACTOR_REGISTRY, actor_uuid="plate"), same
            )
        changed = same.model_copy(update={"display_name": "Plate v2"})
        service.put_template(
            _mutation("sync_template", actor_type=ACTOR_REGISTRY, actor_uuid="plate"), changed
        )

        rows = [(row.operation, row.actor_type, row.actor_uuid) for row in service.changes()]
        assert rows == [("create", "registry", "plate"), ("update", "registry", "plate")]
        assert service.latest_ledger_sequence() == 2
    finally:
        service.close()


def test_snapshot_moves_between_sites_in_one_transaction(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "root-template", "root")
        _template(service, "carrier-template", "carrier", with_site=True)
        _template(service, "tube-template", "tube")
        created = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    _node("root", "root"),
                    _node("carrier-1", "carrier", parent="root"),
                    _node("carrier-2", "carrier", parent="root"),
                    _node("tube", "tube", parent="carrier-1"),
                ]
            ),
        )
        identities = created.data.client_ref_map
        base = service.get_tree(created.data.root_material_uuid)
        carrier_1 = next(
            node
            for node in base.nodes
            if node.material.material_uuid == identities["carrier-1"]
        )
        carrier_2 = next(
            node
            for node in base.nodes
            if node.material.material_uuid == identities["carrier-2"]
        )
        service.move_material(
            _mutation("move_material"),
            MaterialMove(
                material_uuid=identities["tube"],
                destination_site_uuid=carrier_1.sites[0].site_uuid,
            ),
        )
        base = service.get_tree(created.data.root_material_uuid)
        changed_nodes = []
        for node in base.nodes:
            if node.material.material_uuid == identities["carrier-1"]:
                node = node.model_copy(
                    update={
                        "sites": [
                            node.sites[0].model_copy(
                                update={"occupied_material_uuid": None}
                            )
                        ]
                    }
                )
            elif node.material.material_uuid == identities["carrier-2"]:
                node = node.model_copy(
                    update={
                        "sites": [
                            node.sites[0].model_copy(
                                update={
                                    "occupied_material_uuid": identities["tube"]
                                }
                            )
                        ]
                    }
                )
            elif node.material.material_uuid == identities["tube"]:
                node = node.model_copy(
                    update={
                        "material": node.material.model_copy(
                            update={
                                "parent_material_uuid": identities["carrier-2"]
                            }
                        )
                    }
                )
            changed_nodes.append(node)

        result = service.apply_snapshot(
            _mutation("apply_material_snapshot"),
            MaterialSnapshot(
                root_material_uuid=base.root_material_uuid,
                nodes=changed_nodes,
            ),
        )

        tube = next(
            node
            for node in result.data.nodes
            if node.material.material_uuid == identities["tube"]
        )
        assert tube.material.parent_material_uuid == identities["carrier-2"]
        assert service.get_site(
            carrier_1.sites[0].site_uuid
        ).occupied_material_uuid is None
        assert service.get_site(
            carrier_2.sites[0].site_uuid
        ).occupied_material_uuid == identities["tube"]
    finally:
        service.close()


def test_search_materials_by_name_returns_list(tmp_path) -> None:
    """search 按 name 精确匹配返回列表；未命中返回 [] 而不是抛错。

    根物料 name 有唯一索引（ux_material_root_name_active），但子物料可以与
    其他根同名，search 需要把根与子节点的命中都返回。
    """
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "deck-template", "deck", with_site=True)
        _template(service, "tube-template", "tube")

        def _renamed(node, name):
            return node.model_copy(
                update={"identity": node.identity.model_copy(update={"name": name})}
            )

        # 树 1：根本身叫 shared-name
        service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(nodes=[_renamed(_node("tube-a", "tube"), "shared-name")]),
        )
        # 树 2：根叫 other-root，其子节点叫 shared-name
        service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    _renamed(_node("deck-b", "deck"), "other-root"),
                    _renamed(_node("tube-b", "tube", parent="deck-b"), "shared-name"),
                ]
            ),
        )

        hits = service.search_materials("shared-name")
        assert [item.material.name for item in hits] == ["shared-name", "shared-name"]
        assert {item.material.resource_id for item in hits} == {
            "resource-tube-a",
            "resource-tube-b",
        }
        assert service.search_materials("missing-name") == []
    finally:
        service.close()


def _typed_node(ref: str, resource_type: str, *, parent: str | None = None):
    """按业务类型建节点；模板名随类型，由建树自动登记。"""

    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        identity=MaterialIdentityWrite(
            resource_id=f"resource-{ref}",
            name=ref,
            resource_type=resource_type,
            class_name=resource_type.title().replace("_", ""),
            template_name=f"{resource_type}-template",
        ),
        data=MaterialDataWrite(),
    )


def _tip_rack(service: MaterialsService):
    """rack -> spot-1/spot-2 -> tip-1/tip-2，另挂一个非枪头的 tube。"""

    created = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                _typed_node("rack", "tip_rack"),
                _typed_node("spot-1", "tip_spot", parent="rack"),
                _typed_node("tip-1", "tip", parent="spot-1"),
                _typed_node("spot-2", "tip_spot", parent="rack"),
                _typed_node("tip-2", "tip", parent="spot-2"),
                _typed_node("tube", "container", parent="rack"),
            ]
        ),
    )
    return created.data.root_material_uuid, created.data.client_ref_map


def _snapshot_with(base, updates):
    nodes = []
    for node in base.nodes:
        update = updates.get(node.material.material_uuid)
        nodes.append(update(node) if update is not None else node)
    return MaterialSnapshot(root_material_uuid=base.root_material_uuid, nodes=nodes)


def _retired(node):
    return node.model_copy(
        update={
            "material": node.material.model_copy(
                update={"lifecycle_status": "retired", "parent_material_uuid": None}
            )
        }
    )


def test_snapshot_retires_discarded_tip_with_the_rest_of_the_snapshot(tmp_path) -> None:
    """被丢弃的枪头与同一快照里的其他变化在同一个写事务里落库：退役 = retired + 软删除。"""

    from unilabos.server.services.materials import MaterialNotFoundError

    service = MaterialsService(tmp_path / "materials.db")
    try:
        root_uuid, ids = _tip_rack(service)
        base = service.get_tree(root_uuid)
        snapshot = _snapshot_with(
            base,
            {
                ids["tip-1"]: _retired,
                ids["spot-1"]: lambda node: node.model_copy(
                    update={"data": node.data.model_copy(update={"data": {"emptied": True}})}
                ),
            },
        )

        diff = service.compare_snapshot(snapshot)
        assert {(c.aggregate_uuid, c.section, tuple(c.changed_fields)) for c in diff.changes} == {
            (ids["tip-1"], "identity", ("lifecycle_status", "parent_material_uuid")),
            (ids["spot-1"], "data", ("data",)),
        }

        result = service.apply_snapshot(_mutation("apply_material_snapshot"), snapshot)

        # 同一事务：两处变化共用一次 mutation 的账本区间
        assert {item.aggregate_uuid for item in result.affected} == {ids["tip-1"], ids["spot-1"]}
        tree_uuids = {node.material.material_uuid for node in result.data.nodes}
        assert ids["tip-1"] not in tree_uuids
        assert ids["tip-2"] in tree_uuids
        spot_1 = next(n for n in result.data.nodes if n.material.material_uuid == ids["spot-1"])
        assert spot_1.data.data == {"emptied": True}
        with pytest.raises(MaterialNotFoundError):
            service.get_material(ids["tip-1"])
        assert [m.material.material_uuid for m in service.list_materials(roots_only=True)] == [root_uuid]

        retire = [c for c in service.changes(limit=1000) if c.aggregate_uuid == ids["tip-1"]][-1]
        assert retire.operation == "retire"
        assert result.ledger_sequence_start <= retire.sequence <= result.ledger_sequence_end
        assert retire.delta["lifecycle_status"] == "retired"
        assert retire.delta["deleted"] is True
        assert retire.delta["reason"] == "tip_discarded"
        assert retire.delta["previous_lifecycle_status"] == "active"
        assert retire.delta["parent_material_uuid"] == ids["spot-1"]

        # 软删除不占 resource_id / 根名唯一索引：同名枪头可以再登记
        again = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(nodes=[_typed_node("tip-1", "tip")]),
        )
        assert again.data.root_material_uuid != ids["tip-1"]
    finally:
        service.close()


def test_precondition_on_retired_tip_is_a_conflict_not_a_missing_reference(tmp_path) -> None:
    """基线读取后枪头被另一次快照退役：旧基线的前置条件是版本冲突（调用方重读），不是找不到。"""

    service = MaterialsService(tmp_path / "materials.db")
    try:
        root_uuid, ids = _tip_rack(service)
        stale = service.get_tree(root_uuid)
        service.apply_snapshot(
            _mutation("apply_material_snapshot"), _snapshot_with(stale, {ids["tip-1"]: _retired})
        )
        preconditions = [
            AggregatePrecondition(
                aggregate_type="material",
                aggregate_uuid=node.material.material_uuid,
                expected_version=node.material.version,
            )
            for node in stale.nodes
        ]
        with pytest.raises(MaterialConflictError, match="was deleted"):
            service.apply_snapshot(
                _mutation("apply_material_snapshot", preconditions=preconditions),
                _snapshot_with(stale, {ids["tip-1"]: _retired}),
            )
    finally:
        service.close()


def test_snapshot_still_cannot_delete_other_materials_or_claimed_tips(tmp_path) -> None:
    """快照退役只对被丢弃的枪头开放；其他类型照旧不能借快照删除。"""

    from unilabos.protocol.materials import MaterialPatch
    from unilabos.server.services.materials import MaterialValidationError

    service = MaterialsService(tmp_path / "materials.db")
    try:
        root_uuid, ids = _tip_rack(service)
        base = service.get_tree(root_uuid)

        with pytest.raises(MaterialValidationError, match="cannot create or delete"):
            service.apply_snapshot(
                _mutation("apply_material_snapshot"),
                _snapshot_with(base, {ids["tube"]: _retired}),
            )
        with pytest.raises(MaterialValidationError, match="cannot change"):
            service.apply_snapshot(
                _mutation("apply_material_snapshot"),
                _snapshot_with(
                    base,
                    {
                        ids["tip-1"]: lambda node: _retired(node).model_copy(
                            update={"position": MaterialPosition(position_x=1, position_y=2, position_z=3)}
                        )
                    },
                ),
            )

        service.patch_material(
            _mutation("patch_material"), ids["tip-2"], MaterialPatch(lifecycle_status="reserved")
        )
        base = service.get_tree(root_uuid)
        with pytest.raises(MaterialConflictError, match="reserved"):
            service.apply_snapshot(
                _mutation("apply_material_snapshot"),
                _snapshot_with(base, {ids["tip-2"]: _retired}),
            )

        after = service.get_tree(root_uuid)
        assert {n.material.material_uuid for n in after.nodes} == set(ids.values())
        assert all(
            n.material.lifecycle_status != "retired" for n in after.nodes
        )
    finally:
        service.close()
