"""核实后的分库纪元迁移保留库存保护，旧意图不会变成新执行。"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.app import test_station_handoff as handoff_fixture
from tests.app.center_client.test_recovery_mode import resume_request
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.handoff import HandoffBridge
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.persistence.center_epochs import synchronize_inventory_epoch
from unilabos.persistence.recovery_mode import EvidenceOnlyError, enter, resume, review


def reviewed_rotation(source):
    with source._db.transaction():
        mode = enter(source._db, "核对工作流与库存分库后保留旧执行隔离")
        checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                      "station_uuid": source.station_uuid, "instance_uuid": source.instance_uuid,
                      "event_epoch_uuid": source.event_epoch_uuid, "command_high_watermark": 0,
                      "event_high_watermark": 0, "invocations": [], "reason": "中心已隔离旧调用", "created_at_ms": 1}
        resume(source._db, resume_request(review(source._db), checkpoint["checkpoint_uuid"]), checkpoint=checkpoint)
    source.event_epoch_uuid = source._db.execute("SELECT event_epoch_uuid FROM center_runtime_state").fetchone()[0]


def test_inventory_epoch_requires_review_and_preserves_pending_protection(tmp_path):
    for setup in handoff_fixture.handoff_inventory_fixture(tmp_path):
        ids, inventory = setup["ids"], setup["stores"]["target"]
        source = DurableCenterStore(tmp_path / "center.db", station_uuid=ids["target"], instance_uuid=ids["target_instance"])
        try:
            with source._db.transaction():
                source._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (ids["target_epoch"],))
            source.event_epoch_uuid = ids["target_epoch"]
            handoff_fixture.prepared(setup)
            InventoryMaterialAuthority(inventory, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid,
                                       event_epoch_uuid=source.event_epoch_uuid)
            old = dict(inventory.query_one("SELECT * FROM station_handoff_outbox"))
            protection = inventory.query_all("SELECT * FROM station_handoff")
            with pytest.raises(EvidenceOnlyError, match="迁移链"), source._db.transaction(), inventory.transaction() as conn:
                synchronize_inventory_epoch(source._db, conn)
            reviewed_rotation(source)
            with pytest.raises(HandoffInventoryError, match="显式核实"):
                StationHandoffAuthority(inventory, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid,
                                        event_epoch_uuid=source.event_epoch_uuid, lab_uuid=ids["lab"])
            with source._db.transaction(), inventory.transaction() as conn:
                changed = synchronize_inventory_epoch(source._db, conn)
            assert len(changed["updated_authorities"]) == 2
            with source._db.transaction(), inventory.transaction() as conn:
                assert synchronize_inventory_epoch(source._db, conn)["updated_authorities"] == []
            assert len(inventory.query_all("SELECT * FROM station_inventory_epoch_history")) == 2
            authority = StationHandoffAuthority(inventory, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid,
                                                event_epoch_uuid=source.event_epoch_uuid, lab_uuid=ids["lab"])
            material = InventoryMaterialAuthority(inventory, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid,
                                                  event_epoch_uuid=source.event_epoch_uuid)
            # 旧库存事实尚无原分号信封，不能拿新纪元分号，也不能释放原保护。
            bridge = HandoffBridge(object(), source, authority)
            bridge.pump()
            assert authority.pending_intents() == []
            assert authority.recovery_pending_intents()[0]["event_uuid"] == old["event_uuid"]
            assert dict(inventory.query_one("SELECT * FROM station_handoff_outbox")) == old
            assert inventory.query_all("SELECT * FROM station_handoff") == protection
            assert source._db.execute("SELECT COUNT(*) FROM center_handoff_outbox").fetchone()[0] == 0
            # 新纪元独立边界仍可建立；没有把旧物料输出改写进新纪元。
            fresh = {"invocation_uuid": str(uuid4()), "local_task_uuid": str(uuid4()),
                     "station_uuid": source.station_uuid, "owner_instance_uuid": source.instance_uuid,
                     "event_epoch_uuid": source.event_epoch_uuid, "task_payload_digest": "sha256:" + "1" * 64,
                     "input_material_refs": [], "outputs": []}
            assert material.prepare(fresh)["source"] == fresh
        finally:
            source.close()


def test_inventory_migration_is_atomic_for_wrong_second_authority(tmp_path):
    for setup in handoff_fixture.handoff_inventory_fixture(tmp_path):
        ids, inventory = setup["ids"], setup["stores"]["target"]
        source = DurableCenterStore(tmp_path / "center.db", station_uuid=ids["target"], instance_uuid=ids["target_instance"])
        try:
            with source._db.transaction():
                source._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (ids["target_epoch"],))
            source.event_epoch_uuid = ids["target_epoch"]
            InventoryMaterialAuthority(inventory, station_uuid=str(uuid4()), instance_uuid=source.instance_uuid,
                                       event_epoch_uuid=source.event_epoch_uuid)
            original = inventory.query_one("SELECT identity_json FROM station_handoff_identity")["identity_json"]
            reviewed_rotation(source)
            with pytest.raises(EvidenceOnlyError, match="同一原工站"), source._db.transaction(), inventory.transaction() as conn:
                synchronize_inventory_epoch(source._db, conn)
            assert inventory.query_one("SELECT identity_json FROM station_handoff_identity")["identity_json"] == original
            assert json.loads(original)[2] == ids["target_epoch"]
        finally:
            source.close()
