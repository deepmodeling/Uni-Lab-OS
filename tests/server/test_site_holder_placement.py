"""载架型 Site 的占用物料挂在该 Site 的 holder 下（FRAMEWORK_GAPS #30）。

PLR ``Carrier``（含 ``ItemizedCarrier`` / ``WareHouse``）的直接子节点只能是槽位 holder，
物料放在 holder 上、载架的 Site 记占用。``materials.create`` 本地整树建出来就是这个形态；
权威的 move / transfer 到这样的 Site 必须落成同一形态，否则下次启动反序列化台面时载架
拒收物料，事后快照也改不了它的父节点。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pylabrobot.resources import Coordinate, Resource

from tests.plr_fixtures import site_deck
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialMove,
    MaterialTransfer,
    MaterialTransferItem,
)
from unilabos.resources import materials
from unilabos.resources.presets.itemized_carrier import Bottle
from unilabos.resources.presets.warehouse import warehouse_factory
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.core import MaterialValidationError


def _mutation(operation: str) -> InventoryMutation:
    command = str(uuid4())
    return InventoryMutation(
        command_uuid=command,
        effect_key=f"{operation}:{command}",
        operation=operation,
        actor_type="test",
        actor_uuid="site-holder-test",
    )


def _bottle(name: str) -> Bottle:
    bottle = Bottle(name, diameter=20, height=50, max_volume=100)
    # 设备 load 后快照会写回物料在 holder 上的位置；纯权威用例直接给 holder 默认位置
    bottle.location = Coordinate.zero()
    return bottle


@pytest.fixture
def authority(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    service.set_resource_sync_dispatcher(lambda command: {"success": True})
    try:
        yield service, LocalMaterialsClient(service)
    finally:
        service.close()


def _warehouse(gateway, name: str = "wh"):
    """三个 Site 的仓库，A01 上本地预放一个瓶子后整树创建（权威里的参考形态）。"""

    draft = warehouse_factory(name, num_items_x=3, num_items_y=1, num_items_z=1)
    draft["A01"] = _bottle(f"{name}_local")
    return materials.create(draft, gateway=gateway)


def _shape(service: MaterialsService, warehouse_uuid: str) -> dict[str, tuple[str, str]]:
    """{Site label: (占用物料名, 占用物料的父物料名)}。"""

    tree = service.get_tree(warehouse_uuid)
    by_uuid = {node.material.material_uuid: node.material for node in tree.nodes}
    (owner,) = [node for node in tree.nodes if node.material.material_uuid == warehouse_uuid]
    return {
        site.label: (
            by_uuid[site.occupied_material_uuid].name,
            by_uuid[by_uuid[site.occupied_material_uuid].parent_material_uuid].name,
        )
        for site in owner.sites
        if site.occupied_material_uuid
    }


def _site(service: MaterialsService, owner_uuid: str, label: str):
    return next(site for site in service.get_material(owner_uuid).sites if site.label == label)


def test_transfer_onto_a_warehouse_site_matches_a_locally_built_tree(authority) -> None:
    service, gateway = authority
    warehouse = _warehouse(gateway)
    holders = {label: warehouse[label].name for label in ("A01", "A02", "A03")}
    # 本地建树的参考形态：物料挂在 holder 下
    assert _shape(service, warehouse.unilabos_uuid) == {"A01": ("wh_local", holders["A01"])}

    bottle = materials.create(_bottle("moved"), gateway=gateway)
    service.transfer_material(
        _mutation("transfer_material"),
        MaterialTransfer(
            source_device_id="bench",
            target_device_id="bench",
            items=[
                MaterialTransferItem(
                    material_uuid=bottle.unilabos_uuid,
                    target_material_uuid=warehouse.unilabos_uuid,
                    target_site="A02",
                )
            ],
        ),
    )

    assert _shape(service, warehouse.unilabos_uuid) == {
        "A01": ("wh_local", holders["A01"]),
        "A02": ("moved", holders["A02"]),
    }
    # 下次启动：整棵仓库树从权威反序列化，物料在 A02 的 holder 上
    restored = materials.get(warehouse.unilabos_uuid, gateway=gateway).to_plr_resources()[0]
    assert restored["A02"].resource.name == "moved"
    assert restored["A01"].resource.name == "wh_local"


def test_move_between_warehouse_sites_and_back_off_the_warehouse(authority) -> None:
    service, gateway = authority
    warehouse = _warehouse(gateway)
    local_uuid = warehouse["A01"].resource.unilabos_uuid

    service.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=local_uuid, destination_site_uuid=_site(service, warehouse.unilabos_uuid, "A03").site_uuid),
    )
    assert _shape(service, warehouse.unilabos_uuid) == {"A03": ("wh_local", warehouse["A03"].name)}

    # 离开仓库：父节点与 Site 占用一起释放
    bench = materials.create(Resource("bench", size_x=500, size_y=500, size_z=10, category="resource"), gateway=gateway)
    service.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=local_uuid, parent_material_uuid=bench.unilabos_uuid),
    )
    assert _shape(service, warehouse.unilabos_uuid) == {}
    assert service.get_material(local_uuid).material.parent_material_uuid == bench.unilabos_uuid


def test_material_cannot_hang_directly_under_a_holder_backed_carrier(authority) -> None:
    """不指定 Site 挂到仓库下，同样是设备反序列化不了的树：明确拒绝。"""

    service, gateway = authority
    warehouse = _warehouse(gateway)
    bottle = materials.create(_bottle("loose"), gateway=gateway)

    with pytest.raises(MaterialValidationError, match="Site"):
        service.move_material(
            _mutation("move_material"),
            MaterialMove(material_uuid=bottle.unilabos_uuid, parent_material_uuid=warehouse.unilabos_uuid),
        )
    # 指定 holder 本身作父节点（不经 Site）仍然允许
    service.move_material(
        _mutation("move_material"),
        MaterialMove(
            material_uuid=bottle.unilabos_uuid,
            parent_material_uuid=warehouse["A02"].unilabos_uuid,
        ),
    )
    assert service.get_material(bottle.unilabos_uuid).material.parent_material_uuid == warehouse["A02"].unilabos_uuid


def test_deck_sites_without_holders_keep_the_occupant_under_the_deck(authority) -> None:
    """台面型 Site（权威 ResourceSite，没有 holder 子物料）：物料仍直接挂在台面下。"""

    service, gateway = authority
    deck_uuid = str(uuid4())
    materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1", "T2")), gateway=gateway)
    bottle = materials.create(_bottle("on_deck"), gateway=gateway)

    service.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=bottle.unilabos_uuid, destination_site_uuid=_site(service, deck_uuid, "T2").site_uuid),
    )
    assert service.get_material(bottle.unilabos_uuid).material.parent_material_uuid == deck_uuid
    assert _site(service, deck_uuid, "T2").occupied_material_uuid == bottle.unilabos_uuid


def _break_like_the_old_transfer(service: MaterialsService, material_uuid: str, site) -> None:
    """复现旧版 transfer 写下的坏树：物料直接挂在仓库下，并占用仓库的 Site。"""

    with service.write():
        service.connection.execute(
            "UPDATE material SET parent_material_uuid=? WHERE material_uuid=?",
            (site.owner_material_uuid, material_uuid),
        )
        service.connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE site_uuid=?",
            (material_uuid, site.site_uuid),
        )


def test_existing_bad_trees_are_repaired_once_when_the_authority_opens(tmp_path) -> None:
    from unilabos.server.composition import ServerServices
    from unilabos.server.database import ServerDatabasePaths

    paths = ServerDatabasePaths.resolve(tmp_path)
    service = MaterialsService(paths.materials_db)
    gateway = LocalMaterialsClient(service)
    try:
        warehouse = _warehouse(gateway)
        deck_uuid = str(uuid4())
        materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1",)), gateway=gateway)
        bottle = materials.create(_bottle("broken"), gateway=gateway)
        on_deck = materials.create(_bottle("deck_bottle"), gateway=gateway)
        service.move_material(
            _mutation("move_material"),
            MaterialMove(material_uuid=on_deck.unilabos_uuid, destination_site_uuid=_site(service, deck_uuid, "T1").site_uuid),
        )
        _break_like_the_old_transfer(service, bottle.unilabos_uuid, _site(service, warehouse.unilabos_uuid, "A02"))
        with pytest.raises(TypeError, match="ResourceHolder"):
            materials.get(warehouse.unilabos_uuid, gateway=gateway).to_plr_resources()
        holder_name = warehouse["A02"].name
        deck_version = service.get_material(on_deck.unilabos_uuid).material.version
    finally:
        service.close()

    services = ServerServices.open(paths)
    try:
        service = services.materials
        assert _shape(service, warehouse.unilabos_uuid) == {
            "A01": ("wh_local", warehouse["A01"].name),
            "A02": ("broken", holder_name),
        }
        assert [change.operation for change in service.changes(limit=10_000)
                if change.aggregate_uuid == bottle.unilabos_uuid][-1] == "move"
        restored = materials.get(warehouse.unilabos_uuid, gateway=LocalMaterialsClient(service)).to_plr_resources()[0]
        assert restored["A02"].resource.name == "broken"
        # 台面型 Site 的占用物料不是坏树，不动
        assert service.get_material(on_deck.unilabos_uuid).material.version == deck_version
        assert service.get_material(on_deck.unilabos_uuid).material.parent_material_uuid == deck_uuid
        # 幂等：再修一次什么也不做
        assert service.repair_site_occupant_parents() == []
    finally:
        services.close()


def test_physical_move_off_a_warehouse_site_finds_the_site_through_the_holder(authority) -> None:
    """调度的物理搬运合同：来源库位在载架上、物料挂在 holder 下，也要找到来源并结算。"""

    import json

    from unilabos.protocol.material_execution import (
        MaterialExecutionPrepare,
        MaterialExecutionReceipt,
    )
    from unilabos.protocol.materials import (
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialTreeCreate,
        ResourceTemplateWrite,
        SiteCreate,
    )
    from unilabos.server.backend.scheduler.physical_contract import resolve_physical_contract

    service, gateway = authority
    source = _warehouse(gateway, "src")
    target = _warehouse(gateway, "dst")
    sample = source["A01"].resource.unilabos_uuid
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(name="arm", display_name="arm", resource_type="device", class_name="arm"),
    )
    service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref="arm",
                    identity=MaterialIdentityWrite(
                        resource_id="arm", name="arm", resource_type="device",
                        class_name="arm", template_name="arm",
                    ),
                    sites=[SiteCreate(template_name="arm", site_index=0, label="grip",
                                      meta_data={"role": "robot.gripper"})],
                )
            ]
        ),
    )
    schema = {
        "x-unilabos-action-contract": {
            "resource_contract": {
                "transfer": {
                    "material_param": "sample",
                    "target_owner_param": "target",
                    "target_site_name_param": "slot",
                    "gripper_site_role": "robot.gripper",
                }
            }
        }
    }
    values = {"sample": {"uuid": sample}, "target": {"uuid": target.unilabos_uuid}, "slot": "A02"}

    plan = resolve_physical_contract(schema, values, "arm", service)

    (move,) = plan["moves"]
    assert move.source_owner_uuid == source.unilabos_uuid
    assert move.source_site_uuid == _site(service, source.unilabos_uuid, "A01").site_uuid
    ids = set(plan["material_ids"]) | {move.material_uuid, move.source_owner_uuid, move.target_owner_uuid}
    record = service.executions.prepare(
        MaterialExecutionPrepare(
            job_uuid=str(uuid4()), task_uuid=str(uuid4()), claim_uuid=str(uuid4()), attempt_no=1,
            parameter_hash="sha256:test",
            fences={json.dumps(["material", item], separators=(",", ":")): 1 for item in ids},
            moves=[move],
        )
    )
    service.executions.settle(
        MaterialExecutionReceipt(
            job_uuid=record["job_uuid"], claim_uuid=record["claim_uuid"], plan_hash=record["plan_hash"],
            material_aliquot_receipts=[], material_transfer_receipts=[move],
        )
    )
    assert _shape(service, source.unilabos_uuid) == {}
    assert _shape(service, target.unilabos_uuid) == {
        "A01": ("dst_local", target["A01"].name),
        "A02": ("src_local", target["A02"].name),
    }
