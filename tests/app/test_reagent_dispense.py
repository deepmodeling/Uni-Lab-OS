"""试剂分装（reagent.dispense）命令的核心合同。

覆盖设计文档《试剂分装实现技术文档》第 8 节的四条核心场景：
守恒、全有或全无、幂等重放、不侵占预留。其余边界场景在实现落地后补充。

夹具沿用 ``test_backend_reagent_api.py`` 的公共合同客户端造数据，
命令通过 ``execute_command`` 公共入口驱动，与其它库存命令测试一致。
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_router
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import (
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    active_workflow_reserved_quantity,
)

SOURCE_BARCODE = "LOCAL-ETHANOL-SRC"


def _client(tmp_path) -> tuple[TestClient, InventoryStore]:
    """创建绑定隔离 ``inventory.db`` 的公共合同客户端。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def _container_template(client: TestClient) -> str:
    """同步带 ``container`` 标签的试剂瓶模板并返回 UUID。"""

    template = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "local.reagent_bottle",
                    "display_name": "本地试剂瓶",
                    "registry_type": "material",
                    "category": ["container"],
                    "metadata": {"capacity": {"max_volume_ul": 1000000}},
                    "model": {},
                    "class": {},
                    "handles": [],
                    "config_info": [],
                    "scene": [],
                    "device_params": {},
                }
            ]
        },
    ).json()["data"]["templates"][0]
    return template["uuid"]


def _container(client: TestClient, template_uuid: str, barcode: str) -> str:
    """创建一个不携带内容物的空容器并返回物料 UUID。"""

    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": f"容器 {barcode}",
            "barcode": barcode,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["uuid"]


def _reagent_info(client: TestClient) -> str:
    """登记乙醇身份并返回 UUID。"""

    response = client.post(
        "/api/v1/reagent-infos",
        json={
            "cas": "64-17-5",
            "name": "乙醇",
            "name_en": "Ethanol",
            "aliases": ["酒精"],
            "molecular_formula": "C2H6O",
            "density_g_per_ml": 0.789,
            "physical_state": "liquid",
            "meta_data": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["uuid"]


def _source_reagent(
    client: TestClient, material_uuid: str, quantity: float
) -> Dict[str, Any]:
    """在指定容器上登记一瓶 95% 乙醇并返回试剂记录。"""

    response = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": material_uuid,
            "cas": "64-17-5",
            "quantity": quantity,
            "quantity_unit": "mL",
            "concentration_value": 95,
            "concentration_unit": "%",
            "source": "test:dispense",
            "meta_data": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _reagent(client: TestClient, reagent_uuid: str) -> Dict[str, Any]:
    """读取一条试剂记录的当前状态。"""

    response = client.get(f"/api/v1/reagents/{reagent_uuid}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _reagents_on(client: TestClient, material_uuid: str) -> List[Dict[str, Any]]:
    """列出某个容器上的活动试剂记录。"""

    response = client.get("/api/v1/reagents?page=1&page_size=100")
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    return [item for item in items if item["material_uuid"] == material_uuid]


def _ledger(store: InventoryStore) -> List[Dict[str, Any]]:
    """按写入顺序读取全部试剂台账。"""

    return store.query_all(
        "SELECT * FROM inventory_ledger WHERE subject_type='reagent' ORDER BY ledger_id"
    )


def _dispense_command(
    command_id: str,
    source_reagent_uuid: str,
    expected_revision: int,
    targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """构造一条分装命令，形状与其它库存命令一致。"""

    return {
        "command_id": command_id,
        "type": "reagent.dispense",
        "actor": "operator-1",
        "warehouse_zone_id": "zone-1",
        "payload": {
            "source_reagent_uuid": source_reagent_uuid,
            "expected_revision": expected_revision,
            "quantity_unit": "mL",
            "targets": targets,
            "reason": "分装",
        },
    }


class _Scene:
    """一瓶 500 mL 乙醇加若干空容器的公共场景。"""

    def __init__(self, tmp_path, *, empty_containers: int) -> None:
        self.client, self.store = _client(tmp_path)
        self.service = InventoryService(self.store)
        self.template_uuid = _container_template(self.client)
        self.info_uuid = _reagent_info(self.client)
        self.source_material = _container(self.client, self.template_uuid, SOURCE_BARCODE)
        self.source = _source_reagent(self.client, self.source_material, 500)
        self.targets = [
            _container(self.client, self.template_uuid, f"LOCAL-TARGET-{index:02d}")
            for index in range(1, empty_containers + 1)
        ]

    def reserved_on_source(self) -> float:
        """用库存权威自己的读法汇总源瓶上的活动工作流预留。"""

        with self.store.transaction() as conn:
            return active_workflow_reserved_quantity(
                conn, inventory_type="reagent", inventory_uuid=self.source["uuid"]
            )


def test_dispense_conserves_quantity_and_records_lineage(tmp_path) -> None:
    """场景 1：500 mL 分到 4 个空容器各 100 mL，源瓶剩 100，台账成组。"""

    scene = _Scene(tmp_path, empty_containers=4)
    ledger_before = len(_ledger(scene.store))

    response = execute_command(
        scene.service,
        _dispense_command(
            "dispense-1",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": target, "quantity": 100} for target in scene.targets],
        ),
    )

    assert response["status"] == "completed", response
    source_after = _reagent(scene.client, scene.source["uuid"])
    assert source_after["quantity"] == 100
    assert source_after["revision"] == scene.source["revision"] + 1

    created = []
    for target in scene.targets:
        rows = _reagents_on(scene.client, target)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["reagent_info_uuid"] == scene.info_uuid
        assert row["quantity"] == 100
        assert row["quantity_unit"] == "mL"
        assert row["concentration_value"] == 95
        assert row["revision"] == 1
        assert row["meta_data"]["source_reagent_uuid"] == scene.source["uuid"]
        assert row["meta_data"]["dispense_command_id"] == "dispense-1"
        created.append(row)

    total = source_after["quantity"] + sum(row["quantity"] for row in created)
    assert total == 500

    ledger = _ledger(scene.store)[ledger_before:]
    ops = sorted(entry["op_type"] for entry in ledger)
    assert ops == ["reagent.dispense_source"] + ["reagent.dispense_target"] * 4
    assert {entry["causation_id"] for entry in ledger} == {"dispense-1"}
    source_entry = next(e for e in ledger if e["op_type"] == "reagent.dispense_source")
    assert source_entry["quantity_delta"] == -400
    assert source_entry["material_uuid"] == scene.source_material
    for entry in ledger:
        if entry["op_type"] == "reagent.dispense_target":
            assert entry["quantity_delta"] == 100
            assert entry["material_uuid"] in scene.targets


def test_dispense_is_all_or_nothing_when_one_target_is_occupied(tmp_path) -> None:
    """场景 4：第 2 个目标已承载试剂，整体拒绝，源瓶与其它目标均不变。"""

    scene = _Scene(tmp_path, empty_containers=3)
    occupied = scene.targets[1]
    _source_reagent(scene.client, occupied, 10)
    ledger_before = _ledger(scene.store)

    response = execute_command(
        scene.service,
        _dispense_command(
            "dispense-2",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": target, "quantity": 100} for target in scene.targets],
        ),
    )

    assert response["status"] == "rejected", response
    # 必须是业务层"容器已承载内容"的拒绝，而不是命令类型未注册的校验失败。
    # 命令响应合同要求 error_code 为字符串，数字码按字符串投影。
    assert response.get("error_code") == "4002", response
    source_after = _reagent(scene.client, scene.source["uuid"])
    assert source_after["quantity"] == 500
    assert source_after["revision"] == scene.source["revision"]
    assert _reagents_on(scene.client, scene.targets[0]) == []
    assert _reagents_on(scene.client, scene.targets[2]) == []
    assert len(_reagents_on(scene.client, occupied)) == 1
    assert _ledger(scene.store) == ledger_before


def test_dispense_replays_same_command_without_double_deduction(tmp_path) -> None:
    """场景 5：同一 command_id 提交两次，第二次原样返回，只扣一次。"""

    scene = _Scene(tmp_path, empty_containers=2)
    command = _dispense_command(
        "dispense-3",
        scene.source["uuid"],
        scene.source["revision"],
        [{"material_uuid": target, "quantity": 100} for target in scene.targets],
    )

    first = execute_command(scene.service, command)
    assert first["status"] == "completed", first
    ledger_after_first = _ledger(scene.store)

    second = execute_command(scene.service, command)

    # 重放响应额外带 replayed 标记，其余字段必须与首次结果逐项一致。
    assert second.get("replayed") is True, second
    assert {k: v for k, v in second.items() if k != "replayed"} == first
    assert _reagent(scene.client, scene.source["uuid"])["quantity"] == 300
    for target in scene.targets:
        assert len(_reagents_on(scene.client, target)) == 1
    assert _ledger(scene.store) == ledger_after_first


def test_dispense_cannot_take_quantity_reserved_by_workflow(tmp_path) -> None:
    """场景 15：源瓶已被任务预留 80 mL，分 450 拒绝，分 420 成功且预留完好。"""

    scene = _Scene(tmp_path, empty_containers=1)
    task_uuid = "72000000-0000-4000-8000-000000000001"
    # 与 test_inventory_site_allocation 同一条准入入口：来源实例 + 数量型分配。
    source = MaterialSourceAdmissionRequest(
        node_id="source-node",
        resource_template_uuid=scene.template_uuid,
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=scene.template_uuid,
            instance_uuid=scene.source_material,
        ),
    )
    allocation = {
        "uuid": "73000000-0000-4000-8000-000000000001",
        "workflow_task_uuid": task_uuid,
        "workflow_node_job_uuid": "74000000-0000-4000-8000-000000000001",
        "requirement_key": "ethanol",
        "inventory_type": "reagent",
        "inventory_uuid": scene.source["uuid"],
        "material_uuid": scene.source_material,
        "reserved_quantity": 80,
        "quantity_unit": "mL",
    }
    scene.service.admit_task_materials(task_uuid, [source], [allocation])
    assert scene.reserved_on_source() == 80
    # 预留量必须在公共试剂接口外露，控制台据此过滤可绑定的瓶。
    assert _reagent(scene.client, scene.source["uuid"])["active_workflow_reserved_quantity"] == 80
    ledger_before = _ledger(scene.store)

    too_much = execute_command(
        scene.service,
        _dispense_command(
            "dispense-4a",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": scene.targets[0], "quantity": 450}],
        ),
    )
    assert too_much["status"] == "rejected", too_much
    # 预留守卫抛 WorkflowQuantityReservationError，按 update_reagent 惯例映射为 4002。
    assert too_much.get("error_code") == "4002", too_much
    assert _reagent(scene.client, scene.source["uuid"])["quantity"] == 500
    assert _reagents_on(scene.client, scene.targets[0]) == []
    assert _ledger(scene.store) == ledger_before

    ok = execute_command(
        scene.service,
        _dispense_command(
            "dispense-4b",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": scene.targets[0], "quantity": 420}],
        ),
    )
    assert ok["status"] == "completed", ok
    assert _reagent(scene.client, scene.source["uuid"])["quantity"] == 80
    assert len(_reagents_on(scene.client, scene.targets[0])) == 1

    # 分装后源瓶剩 80，正好等于活动预留；预留本身未被触碰。
    assert scene.reserved_on_source() == 80


def test_dispensed_bottle_can_be_dispensed_again_with_a_lineage_chain(tmp_path) -> None:
    """二级分装：分装出来的瓶是普通试剂记录，可再作源瓶；血缘按"直接来源"逐级相连。"""

    scene = _Scene(tmp_path, empty_containers=2)
    first = execute_command(
        scene.service,
        _dispense_command(
            "dispense-l1",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": scene.targets[0], "quantity": 100}],
        ),
    )
    assert first["status"] == "completed", first
    middle = _reagents_on(scene.client, scene.targets[0])[0]

    second = execute_command(
        scene.service,
        _dispense_command(
            "dispense-l2",
            middle["uuid"],
            middle["revision"],
            [{"material_uuid": scene.targets[1], "quantity": 30}],
        ),
    )
    assert second["status"] == "completed", second

    middle_after = _reagent(scene.client, middle["uuid"])
    leaf = _reagents_on(scene.client, scene.targets[1])[0]
    root_after = _reagent(scene.client, scene.source["uuid"])
    # 守恒：根 400 + 中间 70 + 叶 30 = 500。
    assert (root_after["quantity"], middle_after["quantity"], leaf["quantity"]) == (400, 70, 30)
    assert middle_after["revision"] == middle["revision"] + 1
    # 叶的直接来源是中间瓶，中间瓶的直接来源是根；身份与浓度沿链继承。
    assert leaf["meta_data"]["source_reagent_uuid"] == middle["uuid"]
    assert middle_after["meta_data"]["source_reagent_uuid"] == scene.source["uuid"]
    assert leaf["reagent_info_uuid"] == scene.info_uuid
    assert leaf["concentration_value"] == 95
    ledger = _ledger(scene.store)
    level_two = [entry for entry in ledger if entry["causation_id"] == "dispense-l2"]
    assert sorted(entry["op_type"] for entry in level_two) == [
        "reagent.dispense_source",
        "reagent.dispense_target",
    ]
    assert {entry["material_uuid"] for entry in level_two} == {
        scene.targets[0],
        scene.targets[1],
    }


def test_emptied_bottle_with_a_record_is_not_an_empty_container(tmp_path) -> None:
    """分到 0 mL 的瓶仍占着容器：记录未移除前不能再作分装目标，避免两条记录叠在一个瓶里。"""

    scene = _Scene(tmp_path, empty_containers=2)
    drained = execute_command(
        scene.service,
        _dispense_command(
            "dispense-all",
            scene.source["uuid"],
            scene.source["revision"],
            [{"material_uuid": scene.targets[0], "quantity": 500}],
        ),
    )
    assert drained["status"] == "completed", drained
    assert _reagent(scene.client, scene.source["uuid"])["quantity"] == 0
    child = _reagents_on(scene.client, scene.targets[0])[0]

    back_into_source = execute_command(
        scene.service,
        _dispense_command(
            "dispense-back",
            child["uuid"],
            child["revision"],
            [{"material_uuid": scene.source_material, "quantity": 10}],
        ),
    )
    assert back_into_source["status"] == "rejected", back_into_source
    assert back_into_source["error_code"] == "4002"
    assert _reagent(scene.client, child["uuid"])["quantity"] == 500


@pytest.mark.parametrize("quantity,parts", [
    (0.3, [0.1, 0.2]),
    (0.6, [0.1, 0.2, 0.3]),
    (1.0, [0.1] * 10),
    (0.000003, [0.000001, 0.000002]),
])
def test_decimal_dispense_drains_source_without_500_and_replays_once(tmp_path, quantity, parts) -> None:
    """小数全量分装归零且守恒，HTTP 重放不二次扣量或新增目标。"""

    scene = _Scene(tmp_path, empty_containers=len(parts))
    try:
        scene.client.app.include_router(create_router(scene.service))
        client = TestClient(scene.client.app, raise_server_exceptions=False)
        source = client.put(f"/api/v1/reagents/{scene.source['uuid']}", json={
            "quantity": quantity, "quantity_unit": "mL",
        }).json()["data"]
        command = _dispense_command(
            "decimal-drain", source["uuid"], source["revision"],
            [{"material_uuid": target, "quantity": amount} for target, amount in zip(scene.targets, parts)],
        )
        before = len(_ledger(scene.store))
        response = client.post("/api/v1/inventory/commands", json=command)
        assert response.status_code == 200, response.text
        completed = response.json()
        assert completed["status"] == "completed", completed
        assert completed["result"]["source"]["quantity"] == 0
        assert _reagent(scene.client, source["uuid"])["quantity"] == 0
        targets = completed["result"]["targets"]
        assert math.fsum(target["quantity"] for target in targets) == pytest.approx(quantity)
        events = _ledger(scene.store)[before:]
        assert len(events) == len(parts) + 1
        source_event = next(event for event in events if event["op_type"] == "reagent.dispense_source")
        assert source_event["quantity_delta"] == -quantity
        assert source_event["revision"] == source["revision"] + 1
        replay = client.post("/api/v1/inventory/commands", json=command).json()
        assert replay["status"] == "completed"
        assert replay["replayed"] is True
        assert replay["result"] == completed["result"]
        assert len(_ledger(scene.store)) == before + len(parts) + 1
    finally:
        scene.store.close()


def test_decimal_dispense_rejects_real_overdraw_without_partial_changes(tmp_path) -> None:
    """真实超量即使小于旧绝对容差也拒绝，源瓶、目标和台账全部保持原样。"""

    scene = _Scene(tmp_path, empty_containers=2)
    try:
        source = scene.client.put(f"/api/v1/reagents/{scene.source['uuid']}", json={
            "quantity": 0.3, "quantity_unit": "mL",
        }).json()["data"]
        before = _ledger(scene.store)
        response = execute_command(scene.service, _dispense_command(
            "decimal-overdraw", source["uuid"], source["revision"],
            [{"material_uuid": scene.targets[0], "quantity": 0.1},
             {"material_uuid": scene.targets[1], "quantity": 0.2000000001}],
        ))
        assert response["status"] == "rejected", response
        assert response["error_code"] == "1000"
        assert _reagent(scene.client, source["uuid"]) == source
        assert all(_reagents_on(scene.client, target) == [] for target in scene.targets)
        assert _ledger(scene.store) == before
    finally:
        scene.store.close()


def test_concurrent_decimal_dispense_preserves_source_revision_guard(tmp_path) -> None:
    """同一源瓶修订发起两笔分装只能成功一笔，不会丢失扣量或覆盖来源。"""

    scene = _Scene(tmp_path, empty_containers=2)
    try:
        source = scene.client.put(f"/api/v1/reagents/{scene.source['uuid']}", json={
            "quantity": 0.3, "quantity_unit": "mL",
        }).json()["data"]
        commands = [
            _dispense_command(f"decimal-race-{index}", source["uuid"], source["revision"],
                              [{"material_uuid": target, "quantity": 0.2}])
            for index, target in enumerate(scene.targets)
        ]
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda command: execute_command(scene.service, command), commands))
        assert sorted(result["status"] for result in results) == ["completed", "rejected"]
        assert next(result for result in results if result["status"] == "rejected")["error_code"] == "4002"
        final_source = _reagent(scene.client, source["uuid"])
        assert final_source["quantity"] == pytest.approx(0.1)
        assert final_source["revision"] == source["revision"] + 1
        assert sum(len(_reagents_on(scene.client, target)) for target in scene.targets) == 1
        events = [event for event in _ledger(scene.store) if event["op_type"].startswith("reagent.dispense_")]
        assert len(events) == 2
    finally:
        scene.store.close()
