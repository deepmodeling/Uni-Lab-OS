"""人工装入 / 补量 / 取走写库后，真实 HostLink 设备节点的台面跟着变（``sync_material_io_device`` → downlink → 设备协程）。

台面挂在设备上（根树登记所属设备）。装入一盒枪头：库存按注册表资源类建出整棵 PLR 树，设备从权威拉取后
台面与 resource_tracker 里有枪头盒和它的孔位、枪头，重放同一命令不建第二份、台面也不再挂一次。
补量后设备上的容器载入权威的绝对数量，重放同一命令不会再加一次；取走后物料从库位和 tracker 里消失，重放是空操作。
调度器闭环见 ``tests/server/test_material_io_device_sync.py``。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.backend.hostlink.test_warehouse_site_placement import DEVICE_ID, _bottle, stocker  # noqa: F401 - fixture
from tests.plr_fixtures import site_deck
from tests.server.test_material_io_inventory import TIP_RACK, registry_template
from unilabos.client.host.hostlink import downlink, main_hostlink_run
from unilabos.client.host.hostlink.backend import HostLinkBackend
from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.resources import materials
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.scheduler.material_io import sync_material_io_device
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.inventory_commands import InventoryCommands
from unilabos.server.services.materials.material_io import MaterialIO, material_io_command_id


HANDLER_ID = "bench_handler"


class _HandlerDriver:
    """记录物料回调的设备驱动替身（移液站形态：台面上放枪头盒）。"""

    def __init__(self, **_kwargs) -> None:
        self.added: list[list] = []
        self.transfers: list[tuple] = []

    def resource_tree_add(self, resources) -> None:
        self.added.append(list(resources))

    def resource_tree_transfer(self, old_parent, resource, new_parent) -> None:
        self.transfers.append((old_parent, resource, new_parent))


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """真实 HostLink 设备节点，台面（T1 / T2 两个库位）挂在设备自身上：台面根树登记所属设备。"""

    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    local = HostLinkLocalRuntime()
    node = local.add_driver(HostLinkDriverSpec(HANDLER_ID, _HandlerDriver, {}, resource_uuid=str(uuid4())))
    backend = HostLinkBackend(local, is_slave=False)
    backend.start()
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(main_hostlink_run, "get_runtime", lambda: SimpleNamespace(local=local))
    monkeypatch.setattr(downlink, "_remote_device_relay", False)
    try:
        deck_uuid = str(uuid4())
        materials.ensure(site_deck(deck_uuid, "handler_deck", ("T1", "T2")), gateway=gateway)
        materials.assign(node, deck_uuid)
        yield service, gateway, node, node.resource_tracker.uuid_to_resources[deck_uuid]
    finally:
        backend.stop()
        set_materials_gateway(None)
        service.close()


def _apply(service, gateway, plan: dict, job_uuid: str) -> tuple[dict, dict]:
    """调度器批准后的那一步：按冻结计划、以同一命令身份提交 ``material.io``，再做台面同步。"""

    command_id = material_io_command_id(job_uuid)
    response = InventoryCommands(service).typed_command(
        {"command_id": command_id, "type": "material.io", "payload": plan, "actor": "scheduler"}, lambda _exc: 1)
    assert response["status"] == "completed", response
    return response["result"], sync_material_io_device(gateway, plan, response["result"], command_id=command_id)


def test_refill_and_unload_reach_a_real_device_deck(stocker, monkeypatch) -> None:
    service, gateway, node, warehouse = stocker
    runtime = SimpleNamespace(local=SimpleNamespace(get_device=lambda identity: node if identity == DEVICE_ID else None))
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(main_hostlink_run, "get_runtime", lambda: runtime)
    monkeypatch.setattr(downlink, "_remote_device_relay", False)
    bottle = materials.create(_bottle("solvent"), node=node)
    asyncio.run(materials.transfer(bottle, DEVICE_ID, warehouse, "A01", source_device_id=DEVICE_ID))
    contents = ContentContract(service, compound_source=None)
    water = contents.create_reagent_info({"name": "水", "cas": "7732-18-5"})["uuid"]
    contents.create_reagent({"material_uuid": bottle.unilabos_uuid, "reagent_info_uuid": water, "quantity": 20,
                             "quantity_unit": "uL"})
    mount = {"uuid": warehouse.unilabos_uuid}
    refill = {"mode": "refill", "mount": mount, "site": "A01", "material": {"uuid": bottle.unilabos_uuid},
              "reagent": {"quantity": 5, "quantity_unit": "uL"}, "assignee_user_ids": []}
    unload = {"mode": "unload", "mount": mount, "site": "A01", "material": {"uuid": bottle.unilabos_uuid},
              "disposition": "soft_delete", "scope": "material_with_contents", "assignee_user_ids": []}
    expected = {"status": "ok", "device_id": DEVICE_ID, "material_uuids": [bottle.unilabos_uuid]}

    refill = MaterialIO(service).plan(refill, task_uuid="task", job_uuid="job-refill")
    for _attempt in range(2):  # 第二次 = 重启后按冻结计划重放同一命令
        result, sync = _apply(service, gateway, refill, "job-refill")
        assert result["quantity"] == 25 and sync == {**expected, "action": "update"}
        assert warehouse["A01"].resource.tracker.liquids == [("水", 25.0, "ul")]

    unload = MaterialIO(service).plan(unload, task_uuid="task", job_uuid="job-unload")
    for _attempt in range(2):
        _result, sync = _apply(service, gateway, unload, "job-unload")
        assert sync == {**expected, "action": "unload"}
        assert warehouse["A01"].resource is None
        assert bottle.unilabos_uuid not in node.resource_tracker.uuid_to_resources


def test_load_puts_a_tip_rack_with_its_tips_on_a_real_device_deck(handler, monkeypatch) -> None:
    service, gateway, node, deck = handler
    template = registry_template(service, monkeypatch)
    param = {"mode": "load", "mount": {"uuid": deck.unilabos_uuid}, "site": "T2", "resource_template": template,
             "material_name": "新吸头盒", "assignee_user_ids": []}
    plan = MaterialIO(service).plan(param, task_uuid="task", job_uuid="job-load")
    tracker = node.resource_tracker.uuid_to_resources
    placed = None
    for _attempt in range(2):  # 第二次 = 重启后按冻结计划重放同一命令
        result, sync = _apply(service, gateway, plan, "job-load")
        new = result["material_uuid"]
        assert sync == {"status": "ok", "device_id": HANDLER_ID, "action": "load", "material_uuids": [new]}
        rack = tracker[new]
        placed = rack if placed is None else placed
        assert rack is placed  # 重放：设备按同一 transfer_uuid 认出已应用过，不再拉一份挂上去
        # 台面上：枪头盒挂在 T2，96 个孔位都有枪头，全部带权威 UUID 登记在 tracker 里
        assert rack.parent is deck and rack.name == "新吸头盒"
        assert deck.sites[1].occupied_material_uuid == new and deck.sites[0].occupied_material_uuid is None
        spots = rack.children
        assert len(spots) == 96 and all(spot.has_tip() for spot in spots)
        authority = {item.material.material_uuid for item in service.get_tree(new).nodes}
        assert len(authority) == 1 + 96 + 96
        local = {new, *(item.unilabos_uuid for item in rack.get_all_children())}
        assert local == authority and all(uuid in tracker for uuid in authority)
    # 库存里也只有一盒（一棵树），台面回调只挂过一次
    roots = [item for item in service.list_materials() if item.material.template_name == TIP_RACK]
    assert [item.material.material_uuid for item in roots] == [placed.unilabos_uuid]
    assert [moved for _old, moved, _new in node.driver.transfers] == [placed]
