"""Inventory wire schemas for REST and Cloud synchronization boundaries.

Domain objects remain dataclasses/enums in ``domain.py`` and SQLite rows remain
plain mappings in ``store.py``.  This module is the single Pydantic v2 boundary
for untrusted command input and serialized API/Cloud output.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Dict, List, Literal, Optional, TypeAlias, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue as PydanticJsonValue,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from unilabos.app.scheduler.inventory.domain import InstanceState, ReservationState


JsonValue: TypeAlias = PydanticJsonValue
JsonObject: TypeAlias = Dict[str, JsonValue]

NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
UnixMillis = NonNegativeInt
PositiveQuantity = Annotated[
    float,
    Field(strict=True, gt=0, allow_inf_nan=False),
]
NonNegativeQuantity = Annotated[
    float,
    Field(strict=True, ge=0, allow_inf_nan=False),
]


class WireModel(BaseModel):
    """Strict-by-shape JSON DTO base."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class CloudResponseError(WireModel):
    """Go ``common.Error`` business-error payload."""

    msg: str
    info: List[str] = Field(default_factory=list)


class CloudResponse(WireModel):
    """Go ``common.Resp`` envelope; HTTP 200 can still carry ``code != 0``."""

    code: Annotated[int, Field(strict=True)]
    error: Optional[CloudResponseError] = None
    data: Optional[JsonValue] = None
    # Go HTTP responses currently omit it; WS common.Resp uses Unix seconds.
    timestamp: Optional[NonNegativeInt] = None


# ---------------------------------------------------------------------------
# Command request models
# ---------------------------------------------------------------------------


class MaterialRequirementPayload(WireModel):
    template_id: Optional[NonEmptyString] = None
    lot_id: Optional[NonEmptyString] = None
    quantity: NonNegativeQuantity = 0
    unit: str = ""
    instance_uuid: Optional[NonEmptyString] = None
    barcode: Optional[NonEmptyString] = None

    @model_validator(mode="after")
    def validate_selector(self) -> "MaterialRequirementPayload":
        instance_selectors = [self.instance_uuid, self.barcode]
        lot_selectors = [self.template_id, self.lot_id]
        if any(instance_selectors):
            if sum(value is not None for value in instance_selectors) != 1:
                raise ValueError("exactly one of instance_uuid or barcode is required")
            if self.quantity != 0:
                raise ValueError("instance requirements cannot include quantity")
            return self
        if sum(value is not None for value in lot_selectors) != 1:
            raise ValueError("exactly one of template_id or lot_id is required")
        if self.quantity <= 0:
            raise ValueError("lot requirements require quantity > 0")
        return self


class TemplateUpsertPayload(WireModel):
    template_id: NonEmptyString
    name: Optional[str] = None
    category: Optional[str] = None
    spec: Optional[JsonObject] = None


class TemplateDeletePayload(WireModel):
    template_id: NonEmptyString


class InboundPayload(WireModel):
    """Compatible lot/instance inbound payload.

    ``kind`` is optional only for the legacy/default lot form.  Branch-specific
    fields are rejected instead of being silently ignored.
    """

    kind: Literal["lot", "instance"] = "lot"
    template_id: Optional[NonEmptyString] = None
    lot_id: Optional[NonEmptyString] = None
    quantity: Optional[PositiveQuantity] = None
    unit: Optional[str] = None
    batch_no: Optional[str] = None
    expiry: Optional[str] = None
    barcode: Optional[NonEmptyString] = None
    edge_uuid: Optional[NonEmptyString] = None
    legacy_cloud_id: Optional[NonEmptyString] = None
    cloud_uuid: Optional[NonEmptyString] = None
    parent_uuid: Optional[NonEmptyString] = None
    slot_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "InboundPayload":
        if self.kind == "lot":
            if self.template_id is None:
                raise ValueError("lot inbound requires template_id")
            if self.quantity is None:
                raise ValueError("lot inbound requires quantity > 0")
            instance_only = (
                self.barcode,
                self.edge_uuid,
                self.legacy_cloud_id,
                self.cloud_uuid,
                self.parent_uuid,
                self.slot_id,
            )
            if any(value is not None for value in instance_only):
                raise ValueError("lot inbound contains instance-only fields")
            return self

        lot_only = (self.quantity, self.unit, self.batch_no, self.expiry)
        if any(value is not None for value in lot_only):
            raise ValueError("instance inbound contains lot-only fields")
        identity = (
            self.template_id,
            self.lot_id,
            self.barcode,
            self.edge_uuid,
            self.legacy_cloud_id,
            self.cloud_uuid,
        )
        if not any(identity):
            raise ValueError("instance inbound requires a material or identity field")
        if (
            self.legacy_cloud_id is not None
            and self.cloud_uuid is not None
            and self.legacy_cloud_id != self.cloud_uuid
        ):
            raise ValueError("legacy_cloud_id and cloud_uuid conflict")
        return self


RequirementList = Annotated[List[MaterialRequirementPayload], Field(min_length=1)]


class ReservePayload(WireModel):
    workflow_id: NonEmptyString
    node_requirements: Annotated[
        Dict[NonEmptyString, RequirementList],
        Field(min_length=1),
    ]
    attempt: PositiveInt = 1


class ReleasePayload(WireModel):
    workflow_id: NonEmptyString
    node_id: Optional[NonEmptyString] = None
    attempt: PositiveInt = 1
    reason: Optional[str] = None


class ReservationTransitionPayload(WireModel):
    workflow_id: NonEmptyString
    node_id: NonEmptyString
    attempt: PositiveInt = 1
    parent_uuid: Optional[NonEmptyString] = None
    slot_id: Optional[str] = None
    reason: Optional[str] = None


class MaterialMovePayload(WireModel):
    edge_uuid: NonEmptyString
    parent_uuid: NonEmptyString
    slot_id: str = ""


class MaterialDetachPayload(WireModel):
    edge_uuid: NonEmptyString


class MaterialSetParentPayload(WireModel):
    edge_uuid: NonEmptyString
    parent_uuid: str = ""
    slot_id: Optional[str] = None


class MaterialContentSetPayload(WireModel):
    edge_uuid: NonEmptyString
    state: JsonObject


class MaterialContentClearPayload(WireModel):
    edge_uuid: NonEmptyString


class MaterialTerminalPayload(WireModel):
    edge_uuid: NonEmptyString
    reason: Optional[str] = None


class MaterialAdjustPayload(WireModel):
    lot_id: NonEmptyString
    new_total: NonNegativeQuantity
    reason: NonEmptyString


class DeductSelectionPayload(WireModel):
    """人工扣减的批次/模板选择与审计字段。"""

    lot_id: Optional[NonEmptyString] = None
    template_id: Optional[NonEmptyString] = None
    quantity: PositiveQuantity
    unit: str = ""
    operator: NonEmptyString
    reason: str = ""

    @model_validator(mode="after")
    def validate_selector(self) -> "DeductSelectionPayload":
        if (self.lot_id is None) == (self.template_id is None):
            raise ValueError("exactly one of lot_id or template_id is required")
        return self


class DeductResourcePayload(DeductSelectionPayload):
    """扣减库存并实例化一个可交给 HostNode 的物料。"""

    edge_uuid: Optional[NonEmptyString] = None
    barcode: str = ""


class DeductReagentPayload(DeductSelectionPayload):
    """仅扣减数量；允许按 FIFO 跨批次完成。"""


class DeductRevertPayload(WireModel):
    """按原始 command_id 完整补偿一次人工扣减。"""

    deduct_command_id: NonEmptyString
    operator: NonEmptyString
    reason: NonEmptyString


class InventoryCommandBase(WireModel):
    command_id: NonEmptyString
    type: str
    payload: WireModel
    expected_version: Optional[NonNegativeInt] = None
    warehouse_zone_id: str = ""
    actor: str = ""


class TemplateUpsertCommand(InventoryCommandBase):
    type: Literal["inventory.template.upsert"]
    payload: TemplateUpsertPayload


class TemplateDeleteCommand(InventoryCommandBase):
    type: Literal["inventory.template.delete"]
    payload: TemplateDeletePayload


class InboundCommand(InventoryCommandBase):
    type: Literal["inventory.inbound"]
    payload: InboundPayload


class ReserveCommand(InventoryCommandBase):
    type: Literal["inventory.reserve"]
    payload: ReservePayload


class ReleaseCommand(InventoryCommandBase):
    type: Literal["inventory.release"]
    payload: ReleasePayload


class ConsumeReservationCommand(InventoryCommandBase):
    type: Literal["inventory.consume"]
    payload: ReservationTransitionPayload


class QuarantineReservationCommand(InventoryCommandBase):
    type: Literal["inventory.quarantine"]
    payload: ReservationTransitionPayload


class DeployCommand(InventoryCommandBase):
    type: Literal["material.deploy"]
    payload: MaterialMovePayload


class MoveCommand(InventoryCommandBase):
    type: Literal["material.move"]
    payload: MaterialMovePayload


class DetachCommand(InventoryCommandBase):
    type: Literal["material.detach"]
    payload: MaterialDetachPayload


class SetParentCommand(InventoryCommandBase):
    type: Literal["material.set_parent"]
    payload: MaterialSetParentPayload


class ContentSetCommand(InventoryCommandBase):
    type: Literal["material.content.set"]
    payload: MaterialContentSetPayload


class ContentClearCommand(InventoryCommandBase):
    type: Literal["material.content.clear"]
    payload: MaterialContentClearPayload


class ConsumeInstanceCommand(InventoryCommandBase):
    type: Literal["material.consume"]
    payload: MaterialTerminalPayload


class DiscardInstanceCommand(InventoryCommandBase):
    type: Literal["material.discard"]
    payload: MaterialTerminalPayload


class AdjustCommand(InventoryCommandBase):
    type: Literal["material.adjust"]
    payload: MaterialAdjustPayload
    actor: NonEmptyString


class DeductResourceCommand(InventoryCommandBase):
    type: Literal["inventory.deduct"]
    payload: DeductResourcePayload


class DeductReagentCommand(InventoryCommandBase):
    type: Literal["inventory.deduct_reagent"]
    payload: DeductReagentPayload


class DeductRevertCommand(InventoryCommandBase):
    type: Literal["inventory.deduct_revert"]
    payload: DeductRevertPayload


InventoryCommand = Annotated[
    Union[
        TemplateUpsertCommand,
        TemplateDeleteCommand,
        InboundCommand,
        ReserveCommand,
        ReleaseCommand,
        ConsumeReservationCommand,
        QuarantineReservationCommand,
        DeployCommand,
        MoveCommand,
        DetachCommand,
        SetParentCommand,
        ContentSetCommand,
        ContentClearCommand,
        ConsumeInstanceCommand,
        DiscardInstanceCommand,
        AdjustCommand,
        DeductResourceCommand,
        DeductReagentCommand,
        DeductRevertCommand,
    ],
    Field(discriminator="type"),
]
INVENTORY_COMMAND_ADAPTER = TypeAdapter(InventoryCommand)
JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)


def parse_inventory_command(value: object) -> InventoryCommand:
    """Parse REST/WS command input through the same strict adapter."""

    if isinstance(value, InventoryCommandBase):
        return value
    return INVENTORY_COMMAND_ADAPTER.validate_python(value)


# ---------------------------------------------------------------------------
# REST/Cloud response models
# ---------------------------------------------------------------------------


class CommandStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"


class InventoryCommandResult(WireModel):
    command_id: str
    status: CommandStatus
    result: Optional[JsonObject] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    replayed: Optional[bool] = None


class CloudInventoryCommandResult(InventoryCommandResult):
    timestamp: UnixMillis


class CloudInventoryCommandResultRequest(WireModel):
    """HTTP callback shape accepted by Cloud command-result endpoint."""

    command_id: NonEmptyString
    status: CommandStatus
    result: Optional[JsonObject] = None
    error: Optional[str] = None


class ErrorResponse(WireModel):
    """FastAPI ``HTTPException`` JSON body used by detail endpoints."""

    detail: str


class LegacyMaterialQueryRequest(WireModel):
    """Old HostNode query body, extended additively with logical ``id``."""

    uuids: List[NonEmptyString] = Field(default_factory=list)
    id: Optional[NonEmptyString] = None
    with_children: bool = True

    @model_validator(mode="after")
    def validate_selector(self) -> "LegacyMaterialQueryRequest":
        if not self.uuids and self.id is None:
            raise ValueError("at least one uuid or id is required")
        return self


class LegacyMaterialQueryData(WireModel):
    nodes: List[JsonObject] = Field(default_factory=list)


class LegacyMaterialQueryResponse(WireModel):
    """Compatibility envelope consumed by the existing HTTP client."""

    code: Literal[0] = 0
    data: LegacyMaterialQueryData


class InventoryHealthResponse(WireModel):
    status: Literal["ok"]
    edge_id: str
    lab_id: str
    material_source: Literal["microbackend", "backend", "auto"]


class ResourceTemplateResponse(WireModel):
    template_id: NonEmptyString
    name: str
    category: str
    spec_json: str
    version: PositiveInt


class InventoryLotResponse(WireModel):
    lot_id: NonEmptyString
    template_id: str
    batch_no: str
    unit: str
    quantity_total: NonNegativeQuantity
    quantity_available: NonNegativeQuantity
    quantity_reserved: NonNegativeQuantity
    expiry: str
    quarantined: Annotated[int, Field(strict=True, ge=0, le=1)]
    warehouse_zone_id: str
    created_at: UnixMillis
    version: PositiveInt


class MaterialInstanceResponse(WireModel):
    edge_uuid: NonEmptyString
    legacy_cloud_id: str
    lot_id: str
    template_id: str
    barcode: str
    type: NonEmptyString
    status: InstanceState
    version: PositiveInt
    parent_uuid: str


class ResourceRelationResponse(WireModel):
    parent_uuid: NonEmptyString
    slot_id: str
    child_uuid: NonEmptyString
    version: PositiveInt


class SubstanceContentResponse(WireModel):
    instance_uuid: NonEmptyString
    state_json: str
    version: PositiveInt


class InventoryReservationResponse(WireModel):
    reservation_id: NonEmptyString
    workflow_id: NonEmptyString
    node_id: str
    attempt: PositiveInt
    status: ReservationState
    amounts_json: str
    created_at: UnixMillis
    version: PositiveInt


class InventoryLedgerEntryResponse(WireModel):
    ledger_id: PositiveInt
    occurred_at: UnixMillis
    op_type: NonEmptyString
    aggregate_type: str
    aggregate_id: str
    delta_json: str
    actor: str
    reason: str
    causation_id: str
    trace_id: str
    span_id: str


class SyncOutboxRowResponse(WireModel):
    """SQLite outbox row DTO; deliberately uses ``payload_json``."""

    sequence: PositiveInt
    event_id: NonEmptyString
    edge_id: str
    lab_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: PositiveInt
    event_type: NonEmptyString
    occurred_at: UnixMillis
    causation_id: str
    payload_json: str
    traceparent: str
    tracestate: str
    trace_id: str
    span_id: str


class ProcessedCommandResponse(WireModel):
    command_id: NonEmptyString
    result_json: str
    status: CommandStatus
    processed_at: UnixMillis


class SyncCursorResponse(WireModel):
    cursor_name: NonEmptyString
    acked_sequence: NonNegativeInt
    updated_at: UnixMillis


class InventoryEvent(WireModel):
    """Edge-to-Cloud event DTO; deliberately uses parsed ``payload``."""

    event_id: NonEmptyString
    edge_id: str
    lab_id: str
    sequence: PositiveInt
    aggregate_type: str
    aggregate_id: str
    aggregate_version: PositiveInt
    event_type: NonEmptyString
    occurred_at: UnixMillis
    causation_id: str
    payload: JsonObject
    traceparent: Optional[NonEmptyString] = None
    tracestate: Optional[NonEmptyString] = None
    trace_id: Optional[NonEmptyString] = None
    span_id: Optional[NonEmptyString] = None


class CloudInventoryEventBatch(WireModel):
    """POST ``/edge/sync/events`` body, including the authoritative Edge key."""

    edge_id: NonEmptyString
    events: Annotated[List[InventoryEvent], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_edge_identity(self) -> "CloudInventoryEventBatch":
        for event in self.events:
            if event.edge_id and event.edge_id != self.edge_id:
                raise ValueError(
                    f"event {event.event_id} edge_id differs from batch edge_id"
                )
        return self


class CloudSyncAck(WireModel):
    acked_sequence: NonNegativeInt


class TemplateListResponse(WireModel):
    templates: List[ResourceTemplateResponse]


class LotListResponse(WireModel):
    lots: List[InventoryLotResponse]


class InstanceListResponse(WireModel):
    instances: List[MaterialInstanceResponse]


class RelationListResponse(WireModel):
    relations: List[ResourceRelationResponse]


class ContentListResponse(WireModel):
    contents: List[SubstanceContentResponse]


class ReservationListResponse(WireModel):
    reservations: List[InventoryReservationResponse]


class WorkflowReservationListResponse(ReservationListResponse):
    workflow_id: NonEmptyString


class LedgerListResponse(WireModel):
    entries: List[InventoryLedgerEntryResponse]


class OutboxListResponse(WireModel):
    events: List[SyncOutboxRowResponse]


class ProcessedCommandListResponse(WireModel):
    commands: List[ProcessedCommandResponse]


class SyncCursorListResponse(WireModel):
    cursors: List[SyncCursorResponse]


class InstanceDetailResponse(MaterialInstanceResponse):
    relation: Optional[ResourceRelationResponse] = None
    content: Optional[SubstanceContentResponse] = None


class InventorySnapshotResponse(WireModel):
    """Stable Edge Local API v1 snapshot shape."""

    snapshot_sequence: NonNegativeInt
    templates: List[ResourceTemplateResponse]
    lots: List[InventoryLotResponse]
    instances: List[MaterialInstanceResponse]
    relations: List[ResourceRelationResponse]
    contents: List[SubstanceContentResponse]
    reservations: List[InventoryReservationResponse]


class CloudInventorySnapshotAggregates(WireModel):
    templates: List[ResourceTemplateResponse]
    lots: List[InventoryLotResponse]
    instances: List[MaterialInstanceResponse]
    relations: List[ResourceRelationResponse]
    contents: List[SubstanceContentResponse]
    reservations: List[InventoryReservationResponse]


class CloudInventorySnapshotRequest(WireModel):
    """Cloud snapshot request; intentionally differs from the Edge Local DTO."""

    edge_id: NonEmptyString
    snapshot_sequence: NonNegativeInt
    aggregates: CloudInventorySnapshotAggregates

    @classmethod
    def from_edge_snapshot(
        cls,
        edge_id: str,
        snapshot: object,
    ) -> "CloudInventorySnapshotRequest":
        local = InventorySnapshotResponse.model_validate(snapshot)
        return cls(
            edge_id=edge_id,
            snapshot_sequence=local.snapshot_sequence,
            aggregates=CloudInventorySnapshotAggregates(
                templates=local.templates,
                lots=local.lots,
                instances=local.instances,
                relations=local.relations,
                contents=local.contents,
                reservations=local.reservations,
            ),
        )


class OutboxBacklogResponse(WireModel):
    max_sequence: NonNegativeInt
    acked_sequence: NonNegativeInt


__all__ = [
    "CloudInventoryCommandResultRequest",
    "CloudInventoryCommandResult",
    "CloudInventoryEventBatch",
    "CloudInventorySnapshotAggregates",
    "CloudInventorySnapshotRequest",
    "CloudResponse",
    "CloudResponseError",
    "CloudSyncAck",
    "CommandStatus",
    "ContentListResponse",
    "ErrorResponse",
    "InboundCommand",
    "InstanceDetailResponse",
    "InstanceListResponse",
    "InventoryCommand",
    "InventoryCommandBase",
    "InventoryCommandResult",
    "InventoryEvent",
    "InventoryHealthResponse",
    "InventoryLedgerEntryResponse",
    "InventoryLotResponse",
    "InventoryReservationResponse",
    "InventorySnapshotResponse",
    "JSON_OBJECT_ADAPTER",
    "JsonObject",
    "JsonValue",
    "LedgerListResponse",
    "LegacyMaterialQueryData",
    "LegacyMaterialQueryRequest",
    "LegacyMaterialQueryResponse",
    "LotListResponse",
    "MaterialInstanceResponse",
    "OutboxBacklogResponse",
    "OutboxListResponse",
    "ProcessedCommandListResponse",
    "ProcessedCommandResponse",
    "RelationListResponse",
    "ReservationListResponse",
    "ResourceRelationResponse",
    "ResourceTemplateResponse",
    "SubstanceContentResponse",
    "SyncOutboxRowResponse",
    "SyncCursorListResponse",
    "SyncCursorResponse",
    "TemplateListResponse",
    "WorkflowReservationListResponse",
    "parse_inventory_command",
]
