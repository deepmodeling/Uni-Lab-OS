"""人工补量 / 取走写库后，真实 HostLink 设备节点的台面跟着变（``sync_material_io_device`` → downlink → 设备协程）。

台面是挂到设备上的仓库（根树登记所属设备）；补量后设备上的容器载入权威的绝对数量，重放同一命令不会再加一次；
取走后物料从库位和 tracker 里消失，重放是空操作。调度器闭环见 ``tests/server/test_material_io_device_sync.py``。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.backend.hostlink.test_warehouse_site_placement import DEVICE_ID, _bottle, stocker  # noqa: F401 - fixture
from unilabos.backend.hostlink import downlink, main_hostlink_run
from unilabos.config.config import BasicConfig
from unilabos.resources import materials
from unilabos.server.backend.scheduler.material_io import sync_material_io_device
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.inventory_commands import InventoryCommands
from unilabos.server.services.materials.material_io import MaterialIO, material_io_command_id


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
