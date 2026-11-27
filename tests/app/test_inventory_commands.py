"""云端 command-to-edge 测试.

覆盖测试门槛：
- 重复 command 不重复扣减（processed_command 幂等，回放首次结果）
- expected_version 过期直接 rejected（禁止 Last-Write-Wins）
- ws_client inventory_command 消息链路（accepted/rejected/completed 回报）
"""

import asyncio
from queue import Queue
from typing import Any, Dict, List

from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor


def _svc() -> InventoryService:
    return InventoryService(InventoryStore(":memory:"))


def _cmd(cmd_id: str, cmd_type: str, payload: Dict[str, Any], **extra) -> Dict[str, Any]:
    return {"command_id": cmd_id, "type": cmd_type, "actor": "cloud-user",
            "warehouse_zone_id": "zone-1", "payload": payload, **extra}


class TestCommandExecution:
    def test_template_crud_commands_and_versioning(self):
        svc = _svc()
        created = execute_command(svc, _cmd(
            "tpl-c1",
            "inventory.template.upsert",
            {
                "template_id": "tpl-reagent",
                "name": "Reagent",
                "category": "chemical",
                "spec": {"storage_class": "ambient"},
            },
            expected_version=0,
        ))
        assert created["status"] == "completed"
        assert created["result"]["version"] == 1

        updated = execute_command(svc, _cmd(
            "tpl-c2",
            "inventory.template.upsert",
            {"template_id": "tpl-reagent", "name": "Reagent A"},
            expected_version=1,
        ))
        assert updated["status"] == "completed"
        assert updated["result"]["version"] == 2
        assert updated["result"]["category"] == "chemical"

        stale = execute_command(svc, _cmd(
            "tpl-c3",
            "inventory.template.upsert",
            {"template_id": "tpl-reagent", "name": "stale"},
            expected_version=1,
        ))
        assert stale["status"] == "rejected"
        assert stale["error_code"] == "version_conflict"

        deleted = execute_command(svc, _cmd(
            "tpl-c4",
            "inventory.template.delete",
            {"template_id": "tpl-reagent"},
            expected_version=2,
        ))
        assert deleted["status"] == "completed"
        assert svc.store.get_template("tpl-reagent") is None
        assert svc.store.query_one(
            "SELECT deleted_at FROM resource_template WHERE uuid = ?",
            ("tpl-reagent",),
        )["deleted_at"] is not None

    def test_template_delete_rejects_referenced_template(self):
        svc = _svc()
        execute_command(svc, _cmd(
            "tpl-ref-1",
            "inventory.template.upsert",
            {"template_id": "tpl-used", "name": "Used"},
        ))
        execute_command(svc, _cmd(
            "tpl-ref-2",
            "inventory.inbound",
            {"template_id": "tpl-used", "quantity": 1, "lot_id": "lot-used"},
        ))
        rejected = execute_command(svc, _cmd(
            "tpl-ref-3",
            "inventory.template.delete",
            {"template_id": "tpl-used"},
        ))
        assert rejected["status"] == "rejected"
        assert "referenced" in rejected["error"]

    def test_inbound_lot_completed(self):
        svc = _svc()
        resp = execute_command(svc, _cmd("c1", "inventory.inbound",
                                         {"template_id": "tpl-w", "quantity": 50,
                                          "lot_id": "lot-1"}))
        assert resp["status"] == "completed"
        assert svc.store.get_lot("lot-1")["quantity_total"] == 50.0

    def test_duplicate_command_no_double_deduct(self):
        """重复 command 幂等：第二次回放首次结果，不重复入库."""
        svc = _svc()
        cmd = _cmd("c-dup", "inventory.inbound",
                   {"template_id": "tpl-w", "quantity": 50, "lot_id": "lot-1"})
        r1 = execute_command(svc, cmd)
        r2 = execute_command(svc, cmd)
        assert r1["status"] == "completed"
        assert r2["status"] == "completed"
        assert r2.get("replayed") is True
        assert svc.store.get_lot("lot-1")["quantity_total"] == 50.0  # 只入一次

    def test_rejected_command_also_idempotent(self):
        svc = _svc()
        cmd = _cmd("c-bad", "material.discard", {"edge_uuid": "mi-nonexistent"})
        r1 = execute_command(svc, cmd)
        r2 = execute_command(svc, cmd)
        assert r1["status"] == "rejected"
        assert r2["status"] == "rejected" and r2.get("replayed") is True

    def test_version_conflict_rejected_not_lww(self):
        """expected_version 过期 → rejected，目标状态不被覆盖."""
        svc = _svc()
        svc.register_instance(edge_uuid="mi-1", parent_uuid="rack-A")
        svc.deploy_instance("mi-1", parent_uuid="rack-A")  # version → 2
        resp = execute_command(svc, _cmd(
            "c-stale", "material.move",
            {"edge_uuid": "mi-1", "parent_uuid": "rack-B"},
            expected_version=1,  # 过期版本
        ))
        assert resp["status"] == "rejected"
        assert resp["error_code"] == "version_conflict"
        assert svc.store.get_instance("mi-1")["parent_uuid"] == "rack-A"  # 未被覆盖

    def test_unknown_type_rejected(self):
        resp = execute_command(_svc(), _cmd("c-x", "inventory.explode", {}))
        assert resp["status"] == "rejected"

    def test_missing_command_id_rejected(self):
        resp = execute_command(_svc(), {"type": "inventory.inbound", "payload": {}})
        assert resp["status"] == "rejected"

    def test_reserve_release_roundtrip(self):
        svc = _svc()
        execute_command(svc, _cmd("c1", "inventory.inbound",
                                  {"template_id": "tpl-w", "quantity": 100,
                                   "lot_id": "lot-1"}))
        r = execute_command(svc, _cmd("c2", "inventory.reserve", {
            "workflow_id": "wf1",
            "node_requirements": {"n1": [{"lot_id": "lot-1", "quantity": 40}]},
        }))
        assert r["status"] == "completed"
        assert svc.store.get_lot("lot-1")["quantity_reserved"] == 40.0
        r = execute_command(svc, _cmd("c3", "inventory.release", {"workflow_id": "wf1"}))
        assert r["status"] == "completed"
        assert svc.store.get_lot("lot-1")["quantity_reserved"] == 0.0

    def test_adjust_requires_reason(self):
        svc = _svc()
        execute_command(svc, _cmd("c1", "inventory.inbound",
                                  {"template_id": "tpl-w", "quantity": 10,
                                   "lot_id": "lot-1"}))
        r = execute_command(svc, _cmd("c2", "material.adjust",
                                      {"lot_id": "lot-1", "new_total": 20}))  # 无 reason
        assert r["status"] == "rejected"

    def test_inbound_instance_with_cloud_uuid_mapping(self):
        """云端带 cloud_uuid 下发入库：只落 legacy mapping，edge_uuid 是 Edge 主键."""
        svc = _svc()
        r = execute_command(svc, _cmd("c1", "inventory.inbound", {
            "kind": "instance", "template_id": "tpl-p",
            "barcode": "BC-1", "cloud_uuid": "cloud-999",
        }))
        assert r["status"] == "completed"
        inst = svc.store.find_instance_by_legacy_cloud_id("cloud-999")
        assert inst is not None
        assert inst["edge_uuid"].startswith("mi-")
        assert inst["edge_uuid"] != "cloud-999"

    def test_relation_and_content_crud_commands(self):
        svc = _svc()
        svc.register_instance(
            edge_uuid="mi-crud", template_id="tpl-tube", parent_uuid="rack-a"
        )

        content = execute_command(svc, _cmd(
            "content-1",
            "material.content.set",
            {"edge_uuid": "mi-crud", "state": {"substance": "water", "volume_ml": 5}},
            expected_version=0,
        ))
        assert content["status"] == "completed"
        assert content["result"]["version"] == 1

        cleared = execute_command(svc, _cmd(
            "content-2",
            "material.content.clear",
            {"edge_uuid": "mi-crud"},
            expected_version=1,
        ))
        assert cleared["status"] == "completed"
        assert cleared["result"]["state"] == {}
        assert cleared["result"]["version"] == 2

        detached = execute_command(svc, _cmd(
            "detach-1",
            "material.detach",
            {"edge_uuid": "mi-crud"},
            expected_version=1,
        ))
        assert detached["status"] == "completed"
        assert detached["result"]["version"] == 2
        assert svc.store.get_relation("mi-crud") is None

    def test_reservation_consume_command(self):
        svc = _svc()
        svc.inbound_lot("tpl-water", 10, lot_id="lot-water")
        svc.reserve_workflow(
            "wf-consume",
            {"node-a": [MaterialRequirement(lot_id="lot-water", quantity=4)]},
        )
        result = execute_command(svc, _cmd(
            "consume-rsv-1",
            "inventory.consume",
            {"workflow_id": "wf-consume", "node_id": "node-a"},
        ))
        assert result["status"] == "completed"
        assert result["result"]["status"] == "consumed"
        assert svc.store.get_lot("lot-water")["quantity_total"] == 6


class TestInventoryCrudApi:
    def test_template_and_reservation_detail_routes(self):
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.inventory.api import create_app

        svc = _svc()
        svc.upsert_template("tpl-api", name="API template")
        svc.inbound_lot("tpl-api", 10, lot_id="lot-api")
        svc.reserve_workflow(
            "wf-api",
            {"node-api": [MaterialRequirement(lot_id="lot-api", quantity=2)]},
        )
        reservation = svc.store.get_reservation("wf-api", "node-api", 1)
        client = TestClient(create_app(svc))

        template = client.get("/api/v1/inventory/templates/tpl-api")
        assert template.status_code == 200
        assert template.json()["name"] == "API template"

        detail = client.get(
            f"/api/v1/inventory/reservations/{reservation['reservation_id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["workflow_id"] == "wf-api"

        assert client.get("/api/v1/inventory/templates/missing").status_code == 404
        assert client.get("/api/v1/inventory/reservations/missing").status_code == 404


class TestWsClientChannel:
    def _make_processor(self) -> MessageProcessor:
        return MessageProcessor("ws://test", Queue(maxsize=100), DeviceActionManager())

    def _drain(self, q: Queue) -> List[Dict[str, Any]]:
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    def test_command_executed_and_result_sent(self):
        mp = self._make_processor()
        mp.inventory_service = _svc()
        asyncio.run(mp._handle_inventory_command(_cmd(
            "c1", "inventory.inbound",
            {"template_id": "tpl-w", "quantity": 10, "lot_id": "lot-1"},
        )))
        results = [m for m in self._drain(mp.send_queue)
                   if m.get("action") == "inventory_command_result"]
        assert len(results) == 1
        assert results[0]["data"]["status"] == "completed"
        assert mp.inventory_service.store.get_lot("lot-1") is not None

    def test_no_service_attached_rejected(self):
        mp = self._make_processor()
        asyncio.run(mp._handle_inventory_command(_cmd("c1", "inventory.inbound", {})))
        results = [m for m in self._drain(mp.send_queue)
                   if m.get("action") == "inventory_command_result"]
        assert results[0]["data"]["status"] == "rejected"
        assert "not attached" in results[0]["data"]["error"]

    def test_ws_replay_same_command(self):
        mp = self._make_processor()
        mp.inventory_service = _svc()
        cmd = _cmd("c-dup", "inventory.inbound",
                   {"template_id": "tpl-w", "quantity": 10, "lot_id": "lot-1"})
        asyncio.run(mp._handle_inventory_command(cmd))
        asyncio.run(mp._handle_inventory_command(cmd))
        assert mp.inventory_service.store.get_lot("lot-1")["quantity_total"] == 10.0


class TestSetParentCommand:
    def test_set_parent_command_and_replay(self):
        svc = _svc()
        svc.register_instance(edge_uuid="mi-rack")
        svc.register_instance(edge_uuid="mi-tip")
        resp = execute_command(svc, _cmd(
            "sp-1", "material.set_parent",
            {"edge_uuid": "mi-tip", "parent_uuid": "mi-rack"},
        ))
        assert resp["status"] == "completed"
        assert resp["result"]["parent_uuid"] == "mi-rack"
        # 幂等重放：返回首次结果，不重复执行
        replay = execute_command(svc, _cmd(
            "sp-1", "material.set_parent",
            {"edge_uuid": "mi-tip", "parent_uuid": "mi-rack"},
        ))
        assert replay.get("replayed") is True
        # 组成父子成环被拒
        rejected = execute_command(svc, _cmd(
            "sp-2", "material.set_parent",
            {"edge_uuid": "mi-rack", "parent_uuid": "mi-tip"},
        ))
        assert rejected["status"] == "rejected"
