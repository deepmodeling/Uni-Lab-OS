"""后端表记录沿用 resources/objects 的严格 Pydantic 约束。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.models.history import DecisionAuditRecord, PayloadObjectRecord
from unilabos.server.models.materials import (
    InventoryLotRecord,
    MaterialRecord,
)
from unilabos.server.models.runtime import (
    AdapterEventInboxRecord,
    BackendSessionRecord,
    DeviceActionAvailabilityRecord,
    TerminalDecisionRecord,
)


def test_server_records_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        BackendSessionRecord(
            session_uuid="session",
            edge_uuid="edge",
            backend_uri="https://backend",
            authority_epoch="epoch",
            state="active",
            last_seen_at_ms=1,
            legacy_field=True,
        )


def test_replace_result_requires_replacement_result_uuid() -> None:
    with pytest.raises(ValidationError, match="replacement_result_uuid"):
        TerminalDecisionRecord(
            decision_uuid="decision",
            gate_uuid="gate",
            job_uuid="job",
            command_uuid="command",
            action="replace_result",
            trusted_actor_type="backend",
            scheduler_revision=2,
            request_fingerprint="fingerprint",
            decided_at_ms=1,
        )


def test_material_cannot_parent_itself() -> None:
    with pytest.raises(ValidationError, match="own parent"):
        MaterialRecord(
            material_uuid="material",
            resource_id="resource",
            template_uuid="template",
            parent_material_uuid="material",
            name="name",
            resource_type="resource",
            class_name="Resource",
            template_name="template",
            lifecycle_status="active",
            created_at_ms=1,
            updated_at_ms=1,
        )


def test_inventory_lot_rejects_invalid_balance() -> None:
    with pytest.raises(ValidationError, match="exceed total"):
        InventoryLotRecord(
            lot_uuid="lot",
            template_uuid="template",
            unit="ml",
            quantity_total=10,
            quantity_available=8,
            quantity_reserved=3,
            created_at_ms=1,
            updated_at_ms=1,
        )


def test_payload_has_exactly_one_storage_location() -> None:
    common = {
        "payload_uuid": "payload",
        "payload_kind": "action_args",
        "media_type": "application/json",
        "codec": "identity",
        "storage_kind": "inline",
        "size_bytes": 2,
        "sha256": "0" * 64,
        "retention_class": "job",
        "created_at_ms": 1,
    }
    assert PayloadObjectRecord(**common, inline_data=b"{}").inline_data == b"{}"
    with pytest.raises(ValidationError, match="inline payload"):
        PayloadObjectRecord(**common)
    with pytest.raises(ValidationError, match="external payload"):
        PayloadObjectRecord(
            **{**common, "storage_kind": "external"},
            inline_data=b"{}",
            external_uri="file:///x",
        )


def test_free_action_cannot_reference_active_job() -> None:
    with pytest.raises(ValidationError, match="free action"):
        DeviceActionAvailabilityRecord(
            endpoint_uuid="endpoint",
            device_uuid="device",
            action_name="transfer",
            state="free",
            active_job_uuid="job",
            source="adapter_report",
            source_event_uuid="event",
            discovery_epoch="epoch",
            discovery_generation=1,
            observed_at_ms=1,
            received_at_ms=1,
        )


def test_endpoint_adapter_event_has_no_job_scope() -> None:
    event = AdapterEventInboxRecord(
        adapter_event_uuid="event",
        endpoint_uuid="endpoint",
        adapter_epoch="epoch",
        adapter_sequence=1,
        event_type="capability_snapshot",
        status="received",
        received_at_ms=1,
    )
    assert event.job_uuid is None
    with pytest.raises(ValidationError, match="job scope"):
        AdapterEventInboxRecord(
            adapter_event_uuid="event-2",
            endpoint_uuid="endpoint",
            adapter_epoch="epoch",
            adapter_sequence=2,
            event_type="running",
            status="received",
            received_at_ms=1,
        )


def test_manual_replacement_does_not_require_scheduler_revision() -> None:
    audit = DecisionAuditRecord(
        audit_uuid="audit",
        decision_uuid="decision",
        gate_uuid="gate",
        job_uuid="job",
        actor_type="user",
        action="replace_result",
        request_fingerprint="fingerprint",
        replacement_result_uuid="result",
        replacement_result_version=1,
        occurred_at_ms=1,
        recorded_at_ms=1,
    )
    assert audit.scheduler_revision is None

    with pytest.raises(ValidationError, match="scheduler_revision"):
        DecisionAuditRecord(
            audit_uuid="audit-failed",
            decision_uuid="decision-failed",
            gate_uuid="gate",
            job_uuid="job",
            actor_type="backend",
            action="release_failed",
            request_fingerprint="fingerprint",
            occurred_at_ms=1,
            recorded_at_ms=1,
        )
