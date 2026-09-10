"""Edge 仓储 domain/store/service 测试.

覆盖测试门槛：
- 不变量（非负 / available+reserved<=total / barcode active 唯一）
- (workflow_id,node_id,attempt) 幂等，重复预留/消费不重复扣减
- 两 workflow 并发 reserve 不超卖
- move 不改数量；transfer 后源端不残留；remove（discard）真正持久化（重开 DB 验证）
- Edge UUID 永久稳定，cloud UUID 仅作 legacy mapping
- 所有写操作同事务写 ledger + outbox
"""

import json
import sqlite3
import threading

import pytest

from unilabos.app.scheduler.inventory.domain import (
    DuplicateBarcode,
    InsufficientStock,
    InvariantViolation,
    MaterialRequirement,
    VersionConflict,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import (
    SCHEMA_VERSION,
    InventoryStore,
    _SCHEMA,
    _SCHEMA_V2,
    _SCHEMA_V3_ADD_PARENT,
)


@pytest.fixture()
def svc():
    return InventoryService(InventoryStore(":memory:"), edge_id="edge-t", lab_id="lab-t")


def _req(template="", lot="", qty=0.0, instance="", barcode=""):
    return MaterialRequirement(
        template_id=template, lot_id=lot, quantity=qty,
        instance_uuid=instance, barcode=barcode,
    )


class TestLotBasics:
    def test_inbound_and_invariants(self, svc):
        lot = svc.inbound_lot("tpl-water", 100.0, unit="mL", lot_id="lot-1")
        assert lot["quantity_total"] == 100.0
        assert lot["quantity_available"] == 100.0
        assert lot["quantity_reserved"] == 0.0

        with pytest.raises(InvariantViolation):
            svc.inbound_lot("tpl-water", -5.0)

    def test_reserve_consume_flow(self, svc):
        svc.inbound_lot("tpl-water", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_available"] == 70.0
        assert lot["quantity_reserved"] == 30.0

        svc.consume_reservation("wf1", "n1")
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_total"] == 70.0
        assert lot["quantity_reserved"] == 0.0
        assert lot["quantity_available"] == 70.0

    def test_insufficient_stock_all_or_nothing(self, svc):
        svc.inbound_lot("tpl-a", 10.0, lot_id="lot-a")
        svc.inbound_lot("tpl-b", 5.0, lot_id="lot-b")
        # n2 不足 → 整体回滚，n1 的扣减也不生效
        with pytest.raises(InsufficientStock):
            svc.reserve_workflow(
                "wf1",
                {
                    "n1": [_req(lot="lot-a", qty=5.0)],
                    "n2": [_req(lot="lot-b", qty=99.0)],
                },
            )
        assert svc.store.get_lot("lot-a")["quantity_reserved"] == 0.0
        assert svc.store.get_lot("lot-b")["quantity_reserved"] == 0.0
        assert svc.store.get_reservation("wf1", "n1", 1) is None

    def test_fifo_across_lots(self, svc):
        svc.inbound_lot("tpl-w", 10.0, lot_id="lot-old")
        svc.inbound_lot("tpl-w", 10.0, lot_id="lot-new")
        svc.reserve_workflow("wf1", {"n1": [_req(template="tpl-w", qty=15.0)]})
        rsv = svc.store.get_reservation("wf1", "n1", 1)
        amounts = json.loads(rsv["amounts_json"])
        assert amounts["lots"]["lot-old"] == 10.0
        assert amounts["lots"]["lot-new"] == 5.0

    def test_adjust_requires_reason_and_actor(self, svc):
        from unilabos.app.scheduler.inventory.domain import CommandRejected

        svc.inbound_lot("tpl-w", 10.0, lot_id="lot-1")
        with pytest.raises(CommandRejected):
            svc.adjust_lot("lot-1", 20.0, reason="", actor="alice")
        lot = svc.adjust_lot("lot-1", 20.0, reason="盘点补差", actor="alice")
        assert lot["quantity_total"] == 20.0
        # 审计落 ledger
        rows = svc.store.query_all(
            "SELECT * FROM inventory_ledger WHERE op_type = 'lot.adjusted'"
        )
        assert rows and rows[0]["actor"] == "alice" and rows[0]["reason"] == "盘点补差"

    def test_adjust_cannot_break_reservation(self, svc):
        svc.inbound_lot("tpl-w", 10.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=8.0)]})
        # total 调到低于已预留量 → 不变量拒绝
        with pytest.raises(InvariantViolation):
            svc.adjust_lot("lot-1", 5.0, reason="错误盘点", actor="alice")


class TestIdempotency:
    """(workflow_id, node_id, attempt) 幂等：重复调用不重复扣减."""

    def test_duplicate_reserve_no_double_deduct(self, svc):
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        req = {"n1": [_req(lot="lot-1", qty=30.0)]}
        svc.reserve_workflow("wf1", req)
        svc.reserve_workflow("wf1", req)  # 重放
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_reserved"] == 30.0  # 只扣一次

    def test_duplicate_consume_no_double_deduct(self, svc):
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        r1 = svc.consume_reservation("wf1", "n1")
        r2 = svc.consume_reservation("wf1", "n1")  # 重放
        assert r1["status"] == "consumed"
        assert r2["status"] == "already_consumed"
        assert svc.store.get_lot("lot-1")["quantity_total"] == 70.0

    def test_duplicate_release_noop(self, svc):
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        svc.release_reservation("wf1", "n1")
        svc.release_reservation("wf1", "n1")  # 重放
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_available"] == 100.0
        assert lot["quantity_reserved"] == 0.0

    def test_different_attempt_is_new_reservation(self, svc):
        """retry：attempt+1 是新的幂等键（restart 后可重新预留）."""
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        svc.release_reservation("wf1", "n1", attempt=1, reason="node retry")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]}, attempt=2)
        assert svc.store.get_reservation("wf1", "n1", 2)["status"] == "active"
        assert svc.store.get_lot("lot-1")["quantity_reserved"] == 30.0


class TestConcurrency:
    def test_concurrent_reserve_no_oversell(self):
        """两 workflow 并发 reserve 不超卖：库存 100，两边各要 60，只允许一边成功."""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")

        results = {}
        barrier = threading.Barrier(2)

        def worker(wf_id):
            barrier.wait()
            try:
                svc.reserve_workflow(wf_id, {"n1": [_req(lot="lot-1", qty=60.0)]})
                results[wf_id] = "ok"
            except InsufficientStock:
                results[wf_id] = "short"

        threads = [threading.Thread(target=worker, args=(w,)) for w in ("wfA", "wfB")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results.values()) == ["ok", "short"]
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_reserved"] == 60.0
        assert lot["quantity_available"] == 40.0

    def test_concurrent_reserve_many_small(self):
        """10 线程各抢 20，库存 100：恰好 5 个成功，无超卖."""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i):
            barrier.wait()
            try:
                svc.reserve_workflow(f"wf{i}", {"n": [_req(lot="lot-1", qty=20.0)]})
                with lock:
                    results.append("ok")
            except InsufficientStock:
                with lock:
                    results.append("short")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == 5
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_reserved"] == 100.0
        assert lot["quantity_available"] == 0.0


class TestInstance:
    def test_barcode_unique_among_active(self, svc):
        svc.register_instance(template_id="tpl-p", barcode="BC-1", edge_uuid="mi-1")
        with pytest.raises(DuplicateBarcode):
            svc.register_instance(template_id="tpl-p", barcode="BC-1", edge_uuid="mi-2")
        # discard 后 barcode 释放，可再登记
        svc.discard_instance("mi-1", reason="broken")
        inst = svc.register_instance(template_id="tpl-p", barcode="BC-1", edge_uuid="mi-3")
        assert inst["edge_uuid"] == "mi-3"

    def test_edge_uuid_stable_cloud_id_is_legacy_mapping(self, svc):
        """Edge UUID 永久稳定：cloud UUID 只落 legacy_cloud_id，不覆盖主键."""
        inst = svc.register_instance(
            template_id="tpl-p", edge_uuid="mi-stable", legacy_cloud_id="cloud-111"
        )
        assert inst["edge_uuid"] == "mi-stable"
        assert inst["legacy_cloud_id"] == "cloud-111"

        # 云端重放同一实例（带不同 cloud id）：edge_uuid 不变，已有 mapping 不被覆盖
        replay = svc.register_instance(edge_uuid="mi-stable", legacy_cloud_id="cloud-222")
        assert replay["edge_uuid"] == "mi-stable"
        assert replay["legacy_cloud_id"] == "cloud-111"
        # legacy 反查可用
        assert svc.store.find_instance_by_legacy_cloud_id("cloud-111")["edge_uuid"] == "mi-stable"

    def test_instance_reserve_deploy_flow(self, svc):
        svc.register_instance(template_id="tpl-p", edge_uuid="mi-1", barcode="BC-9")
        svc.reserve_workflow("wf1", {"n1": [_req(instance="mi-1")]})
        assert svc.store.get_instance("mi-1")["status"] == "reserved"

        svc.consume_reservation("wf1", "n1", parent_uuid="deck-1", slot_id="A1")
        inst = svc.store.get_instance("mi-1")
        assert inst["status"] == "bench"
        rel = svc.store.get_relation("mi-1")
        assert rel["parent_uuid"] == "deck-1" and rel["slot_id"] == "A1"

    def test_reserved_instance_not_double_reservable(self, svc):
        svc.register_instance(edge_uuid="mi-1")
        svc.reserve_workflow("wf1", {"n1": [_req(instance="mi-1")]})
        with pytest.raises(InsufficientStock):
            svc.reserve_workflow("wf2", {"n1": [_req(instance="mi-1")]})

    def test_version_conflict_rejected(self, svc):
        svc.register_instance(edge_uuid="mi-1")
        with pytest.raises(VersionConflict):
            svc.deploy_instance("mi-1", expected_version=99)


class TestMoveAndPersistence:
    def test_move_changes_relation_not_quantity(self, svc):
        svc.inbound_lot("tpl-w", 50.0, lot_id="lot-1")
        svc.register_instance(edge_uuid="mi-1", lot_id="lot-1", parent_uuid="rack-A",
                              slot_id="1")
        before = svc.store.get_lot("lot-1")
        svc.deploy_instance("mi-1", parent_uuid="rack-A", slot_id="1")
        svc.move_instance("mi-1", parent_uuid="rack-B", slot_id="7")
        after = svc.store.get_lot("lot-1")
        # move 不改变任何库存数量
        assert (before["quantity_total"], before["quantity_available"]) == (
            after["quantity_total"], after["quantity_available"]
        )
        # transfer 后源端不残留：child 主键唯一，旧 parent 查不到
        assert svc.store.get_relation("mi-1")["parent_uuid"] == "rack-B"
        assert svc.store.children_of("rack-A") == []
        assert [r["child_uuid"] for r in svc.store.children_of("rack-B")] == ["mi-1"]

    def test_discard_persists_across_reopen(self, tmp_path):
        """remove 真正持久化：重开数据库后仍是 discarded 且关系已删."""
        db = str(tmp_path / "inv.db")
        svc = InventoryService(InventoryStore(db))
        svc.register_instance(edge_uuid="mi-1", parent_uuid="rack-A", barcode="BC-1")
        svc.deploy_instance("mi-1", parent_uuid="rack-A")
        svc.discard_instance("mi-1", reason="用完")
        svc.store.close()

        svc2 = InventoryService(InventoryStore(db))
        inst = svc2.store.get_instance("mi-1")
        assert inst["status"] == "discarded"
        assert svc2.store.get_relation("mi-1") is None
        assert svc2.store.children_of("rack-A") == []
        svc2.store.close()


class TestLedgerOutboxAtomicity:
    def test_every_write_emits_ledger_and_outbox(self, svc):
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=10.0)]})
        svc.consume_reservation("wf1", "n1")
        ledger = svc.store.query_all("SELECT op_type FROM inventory_ledger ORDER BY ledger_id")
        outbox = svc.store.query_all("SELECT event_type FROM sync_outbox ORDER BY sequence")
        assert [r["op_type"] for r in ledger] == [r["event_type"] for r in outbox]
        assert "lot.created" in outbox[0]["event_type"]

    def test_failed_write_emits_nothing(self, svc):
        """业务失败回滚时 ledger/outbox 同事务回滚，不留半截记录."""
        svc.inbound_lot("tpl-w", 5.0, lot_id="lot-1")
        before = svc.store.max_outbox_sequence()
        with pytest.raises(InsufficientStock):
            svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=99.0)]})
        assert svc.store.max_outbox_sequence() == before
        assert svc.store.query_all(
            "SELECT * FROM inventory_ledger WHERE op_type = 'lot.reserved'"
        ) == []

    def test_content_update(self, svc):
        svc.register_instance(edge_uuid="mi-1")
        svc.update_content("mi-1", {"substance": "NaCl", "volume_ml": 3.5})
        row = svc.store.get_content("mi-1")
        assert json.loads(row["state_json"])["substance"] == "NaCl"


class TestParentAndSite:
    """单一父不变量：parent_uuid 列（≡ 云端 parent_material_uuid ≡ 资源树父）是唯一
    父层级事实；relation 行仅在「父 + 具名位（PLR site 名）」时存在且父恒一致。"""

    def test_register_initial_parent_uses_shared_relation_helper(self, svc):
        inst = svc.register_instance(
            edge_uuid="mi-registered",
            parent_uuid="rack-register",
            slot_id="A1",
        )
        relation = svc.store.get_relation("mi-registered")
        assert inst["parent_uuid"] == "rack-register"
        assert relation["parent_uuid"] == inst["parent_uuid"]
        assert relation["slot_id"] == "A1"

    def test_set_and_clear_parent_without_slot(self, svc):
        svc.register_instance(edge_uuid="mi-rack")
        svc.register_instance(edge_uuid="mi-tip")
        inst = svc.set_instance_parent("mi-tip", "mi-rack", actor="op")
        assert inst["parent_uuid"] == "mi-rack"
        assert svc.store.get_relation("mi-tip") is None  # 有父无具名位：不占 site
        events = svc.store.pending_outbox(0, 100)
        assert any(e["event_type"] == "instance.parent_changed" for e in events)
        cleared = svc.set_instance_parent("mi-tip", "", actor="op")
        assert cleared["parent_uuid"] == ""
        again = svc.set_instance_parent("mi-tip", "", actor="op")
        assert again["version"] == cleared["version"]  # 幂等 no-op

    def test_deploy_and_move_sync_parent_column(self, svc):
        """deploy/move 写 relation 时父列同步（relation.parent ≡ parent_uuid 列）。"""
        svc.register_instance(edge_uuid="mi-deck")
        svc.register_instance(edge_uuid="mi-deck2")
        svc.register_instance(edge_uuid="mi-plate")
        svc.deploy_instance("mi-plate", parent_uuid="mi-deck", slot_id="A1")
        inst = svc.store.get_instance("mi-plate")
        rel = svc.store.get_relation("mi-plate")
        assert inst["parent_uuid"] == "mi-deck" == rel["parent_uuid"]
        assert rel["slot_id"] == "A1"
        svc.move_instance("mi-plate", parent_uuid="mi-deck2", slot_id="B2")
        inst = svc.store.get_instance("mi-plate")
        rel = svc.store.get_relation("mi-plate")
        assert inst["parent_uuid"] == "mi-deck2" == rel["parent_uuid"]
        assert rel["slot_id"] == "B2"

    def test_set_parent_with_slot_and_reparent(self, svc):
        svc.register_instance(edge_uuid="mi-station")
        svc.register_instance(edge_uuid="mi-deck")
        svc.register_instance(edge_uuid="mi-plate")
        svc.deploy_instance("mi-plate", parent_uuid="mi-deck", slot_id="A1")
        # 换父且不占具名位：父列改写、relation 删除
        svc.set_instance_parent("mi-plate", "mi-station")
        assert svc.store.get_instance("mi-plate")["parent_uuid"] == "mi-station"
        assert svc.store.get_relation("mi-plate") is None
        # 带具名位设父：relation 恢复且父一致
        svc.set_instance_parent("mi-plate", "mi-deck", slot_id="A1")
        rel = svc.store.get_relation("mi-plate")
        assert rel["parent_uuid"] == "mi-deck" and rel["slot_id"] == "A1"
        assert svc.store.get_instance("mi-plate")["parent_uuid"] == "mi-deck"
        assert [c["edge_uuid"] for c in svc.store.component_children_of("mi-deck")] == ["mi-plate"]

    def test_detach_clears_parent_column(self, svc):
        svc.register_instance(edge_uuid="mi-deck")
        svc.register_instance(edge_uuid="mi-plate")
        svc.deploy_instance("mi-plate", parent_uuid="mi-deck", slot_id="A1")
        svc.detach_instance("mi-plate")
        assert svc.store.get_relation("mi-plate") is None
        assert svc.store.get_instance("mi-plate")["parent_uuid"] == ""

    def test_cycle_and_self_rejected(self, svc):
        from unilabos.app.scheduler.inventory.domain import CommandRejected

        svc.register_instance(edge_uuid="mi-a")
        svc.register_instance(edge_uuid="mi-b")
        svc.register_instance(edge_uuid="mi-c")
        svc.set_instance_parent("mi-b", "mi-a")
        svc.set_instance_parent("mi-c", "mi-b")
        with pytest.raises(CommandRejected):
            svc.set_instance_parent("mi-a", "mi-c")  # a→c→b→a 成环
        with pytest.raises(CommandRejected):
            svc.set_instance_parent("mi-a", "mi-a")  # 自指

    def test_parent_must_be_active(self, svc):
        from unilabos.app.scheduler.inventory.domain import CommandRejected, NotFound

        svc.register_instance(edge_uuid="mi-child")
        svc.register_instance(edge_uuid="mi-gone")
        svc.discard_instance("mi-gone", reason="broken")
        with pytest.raises(CommandRejected):
            svc.set_instance_parent("mi-child", "mi-gone")
        with pytest.raises(NotFound):
            svc.set_instance_parent("mi-child", "mi-ghost")

    def test_terminal_op_clears_parent(self, svc):
        svc.register_instance(edge_uuid="mi-p")
        svc.register_instance(edge_uuid="mi-q")
        svc.set_instance_parent("mi-q", "mi-p")
        svc.deploy_instance("mi-q")  # warehouse → bench，才允许 consume
        inst = svc.consume_instance("mi-q")
        assert inst["parent_uuid"] == ""
        assert svc.store.get_instance("mi-q")["parent_uuid"] == ""

    def test_snapshot_includes_parent(self, svc):
        from unilabos.app.scheduler.inventory.sync import build_snapshot

        svc.register_instance(edge_uuid="mi-x")
        svc.register_instance(edge_uuid="mi-y")
        svc.set_instance_parent("mi-y", "mi-x")
        snap = build_snapshot(svc.store)
        by_uuid = {i["edge_uuid"]: i for i in snap["instances"]}
        assert by_uuid["mi-y"]["parent_uuid"] == "mi-x"


class TestStoreMigration:
    def test_v2_database_upgrades_in_place(self, tmp_path):
        """真实 v2 老库（无 parent_uuid 列）原地升级到当前规范。"""
        db = str(tmp_path / "inv.db")
        connection = sqlite3.connect(db)
        connection.executescript(_SCHEMA)
        connection.executescript(_SCHEMA_V2)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()
        reopened = InventoryStore(db)
        cols = [r["name"] for r in reopened.query_all("PRAGMA table_info(material_instance)")]
        assert "parent_uuid" in cols
        assert reopened.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
        reopened.close()

    def test_v1_database_upgrades_to_current_and_backfills_parent(self, tmp_path):
        """临时 v1 库只从既有 relation 确定性补 parent，不触碰用户实库。"""

        db = str(tmp_path / "inventory-v1.db")
        conn = sqlite3.connect(db)
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO material_instance("
            "edge_uuid, legacy_cloud_id, lot_id, template_id, barcode, status, version"
            ") VALUES ('mi-v1', '', '', '', '', 'warehouse', 1)"
        )
        conn.execute(
            "INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version) "
            "VALUES ('rack-v1', 'A1', 'mi-v1', 1)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        reopened = InventoryStore(db)
        assert reopened.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
        assert reopened.get_instance("mi-v1")["parent_uuid"] == "rack-v1"
        assert reopened.parent_consistency_issues() == []
        reopened.close()

    def test_v3_to_current_parent_audit_and_only_safe_repair(self, tmp_path):
        db = str(tmp_path / "inventory-v3.db")
        conn = sqlite3.connect(db)
        conn.executescript(_SCHEMA)
        conn.executescript(_SCHEMA_V2)
        conn.execute(_SCHEMA_V3_ADD_PARENT)
        conn.execute(
            "INSERT INTO material_instance(edge_uuid,parent_uuid) VALUES ('mi-v3','')"
        )
        conn.execute(
            "INSERT INTO resource_relation(parent_uuid,slot_id,child_uuid,version) "
            "VALUES ('rack-relation','A1','mi-v3',1)"
        )
        for table, column in (
            ("inventory_ledger", "trace_id"),
            ("inventory_ledger", "span_id"),
            ("sync_outbox", "traceparent"),
            ("sync_outbox", "tracestate"),
            ("sync_outbox", "trace_id"),
            ("sync_outbox", "span_id"),
        ):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        reopened = InventoryStore(db)
        assert reopened.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
        assert {
            row["name"]
            for row in reopened.query_all("PRAGMA table_info(sync_outbox)")
        }.issuperset({"traceparent", "tracestate", "trace_id", "span_id"})
        service = InventoryService(reopened)
        issues = service.check_parent_consistency()
        assert issues == [
            {
                "kind": "parent_mismatch",
                "child_uuid": "mi-v3",
                "instance_parent_uuid": "",
                "relation_parent_uuid": "rack-relation",
            }
        ]

        result = service.repair_parent_consistency(
            actor="operator:audit",
            reason="v3 register parent backfill",
            causation_id="repair-v3",
        )
        assert result == {"repaired": ["mi-v3"], "unresolved": []}
        assert reopened.get_instance("mi-v3")["parent_uuid"] == "rack-relation"
        audit = reopened.query_one(
            "SELECT * FROM inventory_ledger "
            "WHERE op_type = 'instance.parent_repaired'"
        )
        assert audit["actor"] == "operator:audit"
        assert audit["reason"] == "v3 register parent backfill"

        # 双方非空但冲突时不猜、不覆盖，只返回待人工裁决项。
        service.register_instance(edge_uuid="rack-other")
        with reopened.transaction() as tx:
            tx.execute(
                "UPDATE material_instance SET parent_uuid = 'rack-other' "
                "WHERE edge_uuid = 'mi-v3'"
            )
        unresolved = service.repair_parent_consistency(
            actor="operator:audit",
            reason="conflict check",
        )
        assert unresolved["repaired"] == []
        assert unresolved["unresolved"][0]["kind"] == "parent_mismatch"
        assert reopened.get_instance("mi-v3")["parent_uuid"] == "rack-other"
        reopened.close()

    def test_file_database_enables_wal(self, tmp_path):
        db = str(tmp_path / "inventory-wal.db")
        store = InventoryStore(db)
        assert store.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"
        store.close()
