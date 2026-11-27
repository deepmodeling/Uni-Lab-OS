"""旧状态迁移保留历史与阻断，新动作只走 OS 执行权威。"""
import json
import sqlite3

import pytest

from tests.app.test_observation_services import seed_legacy_hold, world
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.persistence.observations import LEGACY_TABLES
from unilabos.runtime.observations import StateError
from unilabos.testing.observations import observation_services


def test_migrates_every_legacy_table_without_losing_samples_or_pending(world):
    runtime, _, _, _, _, _, sample, path = world
    runtime.observations.publish(sample())
    seed_legacy_hold(runtime, "old-job")
    original = runtime.observations.records()
    runtime.store.close()
    with sqlite3.connect(path / "state.db") as db:
        for old, new in LEGACY_TABLES.items():
            db.execute(f"ALTER TABLE {new} RENAME TO {old}")
    restored = observation_services(runtime.inventory, str(path / "state.db"))
    try:
        assert restored.observations.records() == original
        assert restored.sources.legacy_action("old-job")["state"] == "pending"
        assert restored.observations.snapshot()["groups"]["group"]["recovery_required"]
        with pytest.raises(StateError, match="reconciliation_required"):
            restored.sources.recover("group", quiescent=lambda: True)
        with pytest.raises(StateError, match="canonical_inventory_receipt_missing"):
            restored.sources.reconcile_legacy_action(
                "old-job", evidence={"receipt_id": "invented"}, quiescent=lambda: True
            )
        assert all(not restored.store.db.table_exists(old) for old in LEGACY_TABLES)
        assert (
            restored.store.db.execute(
                "SELECT MAX(cursor) FROM device_observation_event"
            ).fetchone()[0]
            > original["cursor"]
        )
    finally:
        restored.store.close()


def test_legacy_migration_conflict_rolls_back_all_tables(tmp_path):
    path = str(tmp_path / "state.db")
    store = DeviceStateStore(path)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE twin_descriptor (material_uuid TEXT PRIMARY KEY, generation TEXT, body TEXT)"
        )
        db.execute("INSERT INTO twin_descriptor VALUES ('uuid','generation','{}')")
        db.execute("CREATE TABLE twin_group (name TEXT PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO observation_source_group VALUES ('existing','{}')")
    with pytest.raises(ValueError, match="迁移冲突"):
        DeviceStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM twin_descriptor").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM observation_descriptor").fetchone()[0] == 0
        )


def test_effect_intents_migrate_with_inventory_and_reject_conflicts(tmp_path):
    import hashlib

    from unilabos.app.scheduler.inventory.store import InventoryStore
    path = str(tmp_path/'inventory.db')
    inventory = InventoryStore(path)
    payload = json.dumps({'command': 'original', 'evidence': {'tick': 1}})
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with inventory.transaction() as db:
        db.execute('ALTER TABLE execution_effect_intent RENAME TO twin_effect_intent')
        db.execute('INSERT INTO twin_effect_intent VALUES (?,?,?)', ('effect', payload, digest))
    inventory.close()
    restored = InventoryStore(path)
    assert not restored.db.table_exists('twin_effect_intent')
    assert restored.query_one('SELECT payload FROM execution_effect_intent')['payload'] == payload
    restored.close()
