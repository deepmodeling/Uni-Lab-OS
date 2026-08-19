"""聚合数据模型的通用严格性测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.models.materials import MaterialRecord
from unilabos.server.models.runtime import DeviceActionCapability
from unilabos.server.models.telemetry import TelemetryEventRecord


def test_server_records_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MaterialRecord(
            material_uuid="material",
            resource_id="resource",
            template_uuid="template",
            name="name",
            resource_type="resource",
            class_name="Resource",
            template_name="template",
            lifecycle_status="active",
            created_at_ms=1,
            updated_at_ms=1,
            unexpected=True,
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


def test_free_action_cannot_reference_active_job() -> None:
    with pytest.raises(ValidationError, match="free action"):
        DeviceActionCapability(
            device_uuid="device",
            action_name="move",
            concurrency_mode="exclusive",
            availability="free",
            active_job_uuid="job",
            descriptor_hash="hash",
            observed_at_ms=1,
        )


def test_telemetry_event_payload_is_a_model_field() -> None:
    event = TelemetryEventRecord(
        event_uuid="event",
        endpoint_uuid="endpoint",
        device_uuid="device",
        source_epoch="epoch",
        source_generation=1,
        source_sequence=1,
        event_type="property_sample",
        event_key="temperature",
        payload={"value": 25},
        payload_hash="hash",
        observed_at_ms=1,
        received_at_ms=1,
    )
    assert event.payload == {"value": 25}
