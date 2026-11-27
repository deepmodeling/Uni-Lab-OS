"""SQLite 与真实 PostgreSQL 双站交接合同；每个工站独立库存库。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from tests.app import test_station_handoff as contract
from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.config.config import BasicConfig
from unilabos.persistence.config import DatabaseSettings, use_database_settings


@pytest.fixture
def handoff_stations(database_mode, tmp_path):
    target_database = None
    target_url = ""
    if database_mode == "postgresql":
        import psycopg
        from psycopg import sql

        target_database = "unilab_handoff_" + uuid4().hex
        with psycopg.connect(BasicConfig.database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_database)))
        target_url = urlunsplit(urlsplit(BasicConfig.database_url)._replace(path="/" + target_database))

    def store_factory(path):
        target = database_mode == "postgresql" and Path(path).name == "target.db"
        settings = DatabaseSettings(database_mode, target_url if target else BasicConfig.database_url)
        with use_database_settings(settings):
            return InventoryStore(path)

    try:
        for stations in contract.handoff_inventory_fixture(tmp_path, store_factory):
            stations["open_inventory"] = store_factory
            stations["database_settings"] = {side: DatabaseSettings(database_mode, target_url if side == "target" else BasicConfig.database_url)
                                             for side in ("source", "target")}
            yield stations
    finally:
        if target_database is not None:
            import psycopg
            from psycopg import sql

            with psycopg.connect(BasicConfig.database_url, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target_database)))


def test_manual_transfer_real_inventory(handoff_stations):
    contract.test_manual_loop_preserves_local_identity_and_only_releases_after_center_receipt(handoff_stations)


def test_before_permit_cancel(handoff_stations):
    contract.test_cancel_before_permit_releases_source_and_entry_but_never_incoming_material(handoff_stations)


@pytest.mark.parametrize("source_frozen", [False, True])
def test_expired_prepared_cancel_releases_and_reuses_entry(handoff_stations, source_frozen):
    contract.test_expired_prepared_cancel_releases_durable_protection_and_reuses_entry(handoff_stations, source_frozen)


def test_movement_review_blocks_cancel(handoff_stations):
    contract.test_cancel_with_movement_review_keeps_protection_even_without_attention(handoff_stations)


def test_local_receipt_blocks_expired_cancel(handoff_stations):
    contract.test_expired_cancel_snapshot_cannot_erase_local_receipt(handoff_stations)


def test_after_permit_cancel_preserves_quarantine(handoff_stations):
    contract.test_cancel_after_permit_and_attention_never_release(handoff_stations)


def test_before_permit_attention_cancel_preserves_quarantine(handoff_stations):
    contract.test_cancel_before_permit_with_attention_preserves_all_protection(handoff_stations)


def test_receipt_atomic_rollback(handoff_stations, monkeypatch):
    contract.test_receipt_and_outbox_rollback_together(handoff_stations, monkeypatch)


def test_bridge_exact_ack_replay(handoff_stations, monkeypatch):
    contract.test_boundary_envelope_and_exact_ack_survive_cross_database_crash(handoff_stations, monkeypatch)


@pytest.mark.parametrize("side", ["source", "target"])
def test_unseen_cancel_tombstone(handoff_stations, side):
    contract.test_cancel_unseen_handoff_persists_ack_and_rejects_late_prepare(handoff_stations, side)


def test_unknown_unsafe_cancel(handoff_stations):
    contract.test_unknown_unsafe_cancel_stays_pending_without_false_release(handoff_stations)


def test_replay_and_epoch_change(handoff_stations):
    contract.test_prepare_replay_restart_and_expiry_preserve_one_identity(handoff_stations)


def test_physical_guards(handoff_stations):
    contract.test_incoming_and_source_freeze_block_public_writes_and_direct_dispatch(handoff_stations)


def test_reopen_inventory_preserves_original_prepared(handoff_stations):
    s = handoff_stations
    original = contract.prepared(s)
    old = s["stores"]["target"]
    old.close()
    new_store = s["open_inventory"](old.path)
    s["stores"]["target"] = new_store
    restored = StationHandoffAuthority(
        new_store,
        station_uuid=s["ids"]["target"],
        instance_uuid=s["ids"]["target_instance"],
        event_epoch_uuid=s["ids"]["target_epoch"],
        lab_uuid=s["ids"]["lab"],
        now_ms=lambda: s["clock"][0],
    )
    assert restored.pending_intents() == [original]
    assert restored.prepare_incoming(contract.package(s["payload"]), container_details=s["details"], plans=s["plans"]) == original
    assert len(new_store.query_all("SELECT * FROM station_handoff_item")) == 1


def test_conflict_ack_restores_quarantine_after_crash(handoff_stations, monkeypatch):
    contract.test_conflicting_cancel_ack_requarantines_released_source_even_after_crash(handoff_stations, monkeypatch)


def test_multilineage_roundtrip(handoff_stations):
    contract.test_multilineage_carrier_can_roundtrip_without_historical_children(handoff_stations)


def test_canceled_incoming_retry_and_return(handoff_stations):
    contract.test_canceled_incoming_children_do_not_block_retry_and_return(handoff_stations)


@pytest.mark.parametrize("kind", ["sample", "reagent", "historical_sample", "empty_reagent", "substance"])
def test_complex_inventory_transfer(handoff_stations, kind):
    from tests.app.test_station_handoff_adapter import test_complex_transfer_keeps_live_structure_and_content

    test_complex_transfer_keeps_live_structure_and_content(handoff_stations, kind)


@pytest.mark.parametrize("kind", ["sample", "reagent", "historical_sample", "empty_reagent", "substance"])
def test_complex_inventory_roundtrip(handoff_stations, kind):
    from tests.app.test_station_handoff_adapter import test_complex_tree_roundtrip_keeps_identity_and_content

    test_complex_tree_roundtrip_keeps_identity_and_content(handoff_stations, kind)


@pytest.mark.parametrize("change", ["missing_site", "quantity_over_capacity", "chemical_identity"])
def test_complex_adapter_atomic_rollback(handoff_stations, change):
    from tests.app.test_station_handoff_adapter import test_invalid_adapter_is_atomic

    test_invalid_adapter_is_atomic(handoff_stations, change)


def test_complex_content_changed_before_freeze(handoff_stations):
    from tests.app.test_station_handoff_adapter import test_content_change_without_material_revision_rejects_freeze

    test_content_change_without_material_revision_rejects_freeze(handoff_stations)


@pytest.mark.parametrize("kind", ["sample", "reagent"])
def test_complex_freeze_serializes_content_edit(handoff_stations, monkeypatch, kind):
    from tests.app.test_station_handoff_adapter import test_freeze_serializes_against_other_connection_content_edit

    test_freeze_serializes_against_other_connection_content_edit(handoff_stations, monkeypatch, kind)


def test_complex_quantity_guard(handoff_stations):
    from tests.app.test_station_handoff_adapter import test_quantity_reservation_cannot_bypass_handoff_freeze

    test_quantity_reservation_cannot_bypass_handoff_freeze(handoff_stations)


def test_verified_source_return(handoff_stations):
    contract.test_verified_source_return_releases_only_original_inventory_and_cancels_incoming(handoff_stations)


def test_settlement_proof_and_receipt_guards(handoff_stations):
    contract.test_source_settlement_requires_exact_local_proof_and_never_erases_target_receipt(handoff_stations)


def test_verified_late_receipt(handoff_stations):
    contract.test_verified_late_receipt_settles_exact_target_identity_even_other_items_need_attention(handoff_stations)


def test_source_verification_guards(handoff_stations):
    contract.test_source_verification_rejects_no_scan_and_conflicting_replay(handoff_stations)


def test_early_receipt_departure_recovery(handoff_stations):
    contract.test_early_receipt_can_recover_through_source_departure_verification_without_fabricating_release(handoff_stations)


def test_partial_settlement_replay_after_inventory_change(handoff_stations):
    contract.test_partial_settlement_replay_survives_inventory_change_restart_and_next_transport(handoff_stations)


def test_before_permit_reconciliation_no_cancel_ack(handoff_stations):
    contract.test_before_permit_reconciled_source_return_does_not_emit_cancel_ack(handoff_stations)


def test_partial_early_receipt_mixed_settlement(handoff_stations):
    contract.test_partial_early_receipt_and_unfrozen_source_remainder_can_both_settle(handoff_stations)


def test_unprepared_target_source_settlement_tombstone(handoff_stations):
    contract.test_unprepared_target_persists_source_settlement_tombstone(handoff_stations)


def test_durable_container_registration(handoff_stations):
    from tests.app.center_client.test_container_registration import (
        test_registration_persists_original_request_before_network_and_reuses_after_timeout,
    )
    test_registration_persists_original_request_before_network_and_reuses_after_timeout(handoff_stations)


def test_outbound_task_to_handoff_protection(handoff_stations):
    from tests.app.test_outbound_protection import (
        test_outbound_protection_survives_task_release_then_transfers_to_handoff,
    )
    test_outbound_protection_survives_task_release_then_transfers_to_handoff(handoff_stations)


def test_shared_source_not_outbound(handoff_stations):
    from tests.app.test_outbound_protection import test_production_hook_cannot_promote_shared_source_to_outbound
    test_production_hook_cannot_promote_shared_source_to_outbound(handoff_stations)


def test_local_history_current_inventory_attestation(handoff_stations):
    from tests.app.center_client.test_container_registration import (
        test_local_history_attestation_exports_current_known_lineage_without_new_identity,
    )
    test_local_history_attestation_exports_current_known_lineage_without_new_identity(handoff_stations)
