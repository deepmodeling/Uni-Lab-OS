"""``materials.db`` 的资源、物料、Site 与库存记录。"""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import Field, JsonValue, field_validator, model_validator

from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)


def _normalize_site_index(value: object) -> object:
    """保持 canonical Site 对 int/str index 的严格区分。"""

    if isinstance(value, bool):
        raise ValueError("site index cannot be a boolean")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("site index cannot be empty")
    return value


class ResourceTemplateRecord(ServerObject):
    template_uuid: NonEmptyStr
    name: NonEmptyStr
    display_name: NonEmptyStr
    resource_type: NonEmptyStr
    class_name: Optional[str] = None
    module_name: Optional[str] = None
    template_version: NonEmptyStr
    definition_json: JsonObject
    definition_hash: NonEmptyStr
    status: Literal["active", "deprecated", "deleted"]
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "ResourceTemplateRecord":
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if (self.status == "deleted") != (self.deleted_at_ms is not None):
            raise ValueError("deleted template status and deleted_at_ms must agree")
        if self.deleted_at_ms is not None and self.deleted_at_ms < self.created_at_ms:
            raise ValueError("deleted_at_ms cannot precede created_at_ms")
        return self


class ResourceTemplateCategoryRecord(ServerObject):
    """模板分类只用于检索和前端展示，不参与 Site 准入。"""

    template_uuid: NonEmptyStr
    category: NonEmptyStr
    sort_order: int = Field(default=0, ge=0)


class ResourceHandleTemplateRecord(ServerObject):
    """Registry resource handle 的规范列，不复用 action handle 语义。"""

    handle_uuid: NonEmptyStr
    template_uuid: NonEmptyStr
    handle_key: NonEmptyStr
    label: NonEmptyStr
    io_type: Literal["source", "target", "bidirectional"]
    data_type: NonEmptyStr
    side: Optional[Literal["NORTH", "SOUTH", "EAST", "WEST"]] = None
    data_key: Optional[NonEmptyStr] = None
    data_source: Optional[NonEmptyStr] = None
    description: str = ""
    handle_schema_json: JsonObject = Field(default_factory=dict)
    meta_data_json: JsonObject = Field(default_factory=dict)
    definition_hash: NonEmptyStr
    sort_order: int = Field(default=0, ge=0)
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "ResourceHandleTemplateRecord":
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if self.deleted_at_ms is not None and self.deleted_at_ms < self.created_at_ms:
            raise ValueError("deleted_at_ms cannot precede created_at_ms")
        return self


class PoseRecord(ServerObject):
    """与 ``ResourceDictPosition`` 一一对应的扁平持久化列。"""

    size_depth: float = Field(default=0, ge=0)
    size_width: float = Field(default=0, ge=0)
    size_height: float = Field(default=0, ge=0)
    scale_x: float = 0
    scale_y: float = 0
    scale_z: float = 0
    layout: Literal["2d", "x-y", "z-y", "x-z"] = "x-y"
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    position_z: Optional[float] = None
    position3d_x: float = 0
    position3d_y: float = 0
    position3d_z: float = 0
    rotation_x: float = 0
    rotation_y: float = 0
    rotation_z: float = 0
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"] = (
        "rectangle"
    )
    pose_extra_json: Optional[JsonObject] = None
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _position_is_all_or_none(self) -> "PoseRecord":
        values = (self.position_x, self.position_y, self.position_z)
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError("position_x/y/z must be all null or all set")
        return self


class MaterialRecord(ServerObject):
    """与 ``ResourceDict`` 根身份及静态字段对应的物料实例。"""

    material_uuid: NonEmptyStr
    resource_id: NonEmptyStr
    template_uuid: NonEmptyStr
    parent_material_uuid: Optional[NonEmptyStr] = None
    lot_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    description: str = ""
    resource_type: NonEmptyStr
    class_name: NonEmptyStr
    machine_name: str = ""
    barcode: str = ""
    barcode_symbology: str = ""
    template_name: NonEmptyStr
    resource_schema_json: JsonObject = Field(default_factory=dict)
    model_json: JsonObject = Field(default_factory=dict)
    icon_uri: str = ""
    config_json: JsonObject = Field(default_factory=dict)
    extra_json: JsonObject = Field(default_factory=dict)
    meta_data_json: JsonObject = Field(default_factory=dict)
    lifecycle_status: Literal[
        "active",
        "reserved",
        "in_use",
        "quarantined",
        "consumed",
        "retired",
    ]
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_material(self) -> "MaterialRecord":
        if self.parent_material_uuid == self.material_uuid:
            raise ValueError("material cannot be its own parent")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if self.deleted_at_ms is not None and self.deleted_at_ms < self.created_at_ms:
            raise ValueError("deleted_at_ms cannot precede created_at_ms")
        return self


class MaterialPoseRecord(PoseRecord):
    material_uuid: NonEmptyStr
    frame_kind: Literal["lab", "material", "site"]
    frame_material_uuid: Optional[NonEmptyStr] = None
    frame_site_uuid: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def _validate_frame(self) -> "MaterialPoseRecord":
        valid = (
            (
                self.frame_kind == "lab"
                and self.frame_material_uuid is None
                and self.frame_site_uuid is None
            )
            or (
                self.frame_kind == "material"
                and self.frame_material_uuid is not None
                and self.frame_site_uuid is None
            )
            or (
                self.frame_kind == "site"
                and self.frame_material_uuid is None
                and self.frame_site_uuid is not None
            )
        )
        if not valid:
            raise ValueError("pose frame reference does not match frame_kind")
        return self


class MaterialStateSourceEventRecord(ServerObject):
    """跨重连保留的 material state 幂等键；同一事件可包含多份物料状态。"""

    source_event_uuid: NonEmptyStr
    material_uuid: NonEmptyStr
    source_kind: Literal[
        "adapter_report",
        "backend_command",
        "import",
        "reconcile",
        "manual_override",
    ]
    state_hash: NonEmptyStr
    applied_content_version: PositiveVersion
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "MaterialStateSourceEventRecord":
        if self.received_at_ms < self.observed_at_ms:
            raise ValueError("received_at_ms cannot precede observed_at_ms")
        return self


class MaterialStateRecord(ServerObject):
    """Resource 当前物质状态。

    ``liquids_json`` 仅保存当前规范数组；``liquid_history`` 不复制到 latest，
    变化由 inventory ledger/历史库承担。设备 joint state 属于 telemetry.db。
    """

    material_uuid: NonEmptyStr
    status: NonEmptyStr
    sites_initialized: bool = False
    data_json: JsonObject = Field(default_factory=dict)
    liquids_json: Optional[List[JsonValue]] = None
    unknown_counter: Optional[int] = Field(default=None, ge=0)
    content_version: PositiveVersion = 1
    state_hash: NonEmptyStr
    source_event_uuid: NonEmptyStr
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "MaterialStateRecord":
        if self.updated_at_ms < self.observed_at_ms:
            raise ValueError("updated_at_ms cannot precede observed_at_ms")
        return self


class SiteRecord(ServerObject):
    """一个 ResourceSite 聚合；pose、前端 category 和占用不再拆表。"""

    site_uuid: NonEmptyStr
    schema_version: Literal[1] = 1
    owner_material_uuid: NonEmptyStr
    template_name: NonEmptyStr
    site_index: Union[int, NonEmptyStr]
    label: NonEmptyStr
    visible: bool = True
    occupied_material_uuid: Optional[NonEmptyStr] = None
    pose_json: JsonObject = Field(default_factory=dict)
    allowed_resource_categories_json: List[NonEmptyStr] = Field(default_factory=list)
    parent_link: str = ""
    description: str = ""
    meta_data_json: JsonObject = Field(default_factory=dict)
    extra_json: JsonObject = Field(default_factory=dict)
    occupancy_changed_by_job_uuid: Optional[NonEmptyStr] = None
    occupancy_changed_by_command_uuid: Optional[NonEmptyStr] = None
    occupancy_changed_at_ms: UnixMilliseconds
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    _strict_site_index = field_validator("site_index", mode="before")(
        _normalize_site_index
    )

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "SiteRecord":
        if self.occupied_material_uuid == self.owner_material_uuid:
            raise ValueError("site owner cannot occupy its own Site")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if self.deleted_at_ms is not None and self.deleted_at_ms < self.created_at_ms:
            raise ValueError("deleted_at_ms cannot precede created_at_ms")
        if self.deleted_at_ms is not None and self.occupied_material_uuid is not None:
            raise ValueError("occupied Site cannot be deleted")
        return self


class InventoryLotRecord(ServerObject):
    lot_uuid: NonEmptyStr
    template_uuid: NonEmptyStr
    batch_no: str = ""
    unit: NonEmptyStr
    quantity_total: float = Field(ge=0)
    quantity_available: float = Field(ge=0)
    quantity_reserved: float = Field(ge=0)
    expiry_at_ms: Optional[UnixMilliseconds] = None
    quarantined: bool = False
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_lot(self) -> "InventoryLotRecord":
        if self.quantity_available + self.quantity_reserved > self.quantity_total:
            raise ValueError("available + reserved cannot exceed total")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        return self


ReservationStatus = Literal[
    "active", "consumed", "released", "canceled", "expired", "quarantined"
]


class InventoryReservationRecord(ServerObject):
    """一个 backend job 一份预留；retry 的新 job 必须使用新 reservation。"""

    reservation_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    request_hash: NonEmptyStr
    items_json: List[JsonObject]
    status: ReservationStatus
    expires_at_ms: Optional[UnixMilliseconds] = None
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "InventoryReservationRecord":
        if not self.items_json:
            raise ValueError("reservation requires at least one item")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        return self


class InventoryCommandEffectRecord(ServerObject):
    """materials writer 的命令幂等记录，不代表本地 job retry。"""

    command_uuid: NonEmptyStr
    effect_key: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    operation: NonEmptyStr
    request_json: JsonObject
    request_hash: NonEmptyStr
    status: Literal["applying", "applied", "rejected"]
    result_json: JsonObject = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    ledger_sequence_start: Optional[int] = Field(default=None, ge=1)
    ledger_sequence_end: Optional[int] = Field(default=None, ge=1)
    started_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    completed_at_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_effect(self) -> "InventoryCommandEffectRecord":
        if self.updated_at_ms < self.started_at_ms:
            raise ValueError("updated_at_ms cannot precede started_at_ms")
        if (
            self.completed_at_ms is not None
            and self.completed_at_ms < self.started_at_ms
        ):
            raise ValueError("completed_at_ms cannot precede started_at_ms")
        if (self.status == "applying") != (self.completed_at_ms is None):
            raise ValueError("effect status and completed_at_ms must agree")
        has_range = (
            self.ledger_sequence_start is not None
            and self.ledger_sequence_end is not None
            and self.ledger_sequence_end >= self.ledger_sequence_start
        )
        if (self.status == "applied") != has_range:
            raise ValueError("only applied effects require a valid ledger range")
        return self


class InventoryLedgerRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    aggregate_type: Literal[
        "resource_template",
        "handle_template",
        "material",
        "material_state",
        "site",
        "lot",
        "reservation",
    ]
    aggregate_uuid: NonEmptyStr
    operation: NonEmptyStr
    previous_version: int = Field(ge=0)
    aggregate_version: PositiveVersion
    state_hash: NonEmptyStr
    delta_json: JsonObject
    job_uuid: Optional[NonEmptyStr] = None
    command_uuid: Optional[NonEmptyStr] = None
    effect_key: Optional[NonEmptyStr] = None
    actor_type: NonEmptyStr
    actor_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_ledger_version(self) -> "InventoryLedgerRecord":
        if self.aggregate_version != self.previous_version + 1:
            raise ValueError(
                "aggregate_version must immediately follow previous_version"
            )
        if (self.command_uuid is None) != (self.effect_key is None):
            raise ValueError("command_uuid and effect_key must be set together")
        return self


class InventoryEventOutboxRecord(ServerObject):
    """对 ledger event 的轻量投递引用；ACK 存在 peer checkpoint。"""

    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    created_at_ms: UnixMilliseconds


class InventorySyncStateRecord(ServerObject):
    peer_key: NonEmptyStr
    acked_sequence: int = Field(default=0, ge=0)
    sent_through_sequence: int = Field(default=0, ge=0)
    snapshot_version: int = Field(default=0, ge=0)
    last_attempt_at_ms: Optional[UnixMilliseconds] = None
    last_ack_at_ms: Optional[UnixMilliseconds] = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_error: Optional[str] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_window(self) -> "InventorySyncStateRecord":
        if self.sent_through_sequence < self.acked_sequence:
            raise ValueError("sent_through_sequence cannot trail acked_sequence")
        return self


__all__ = [
    "InventoryCommandEffectRecord",
    "InventoryEventOutboxRecord",
    "InventoryLedgerRecord",
    "InventoryLotRecord",
    "InventoryReservationRecord",
    "InventorySyncStateRecord",
    "MaterialPoseRecord",
    "MaterialRecord",
    "MaterialStateRecord",
    "MaterialStateSourceEventRecord",
    "PoseRecord",
    "ResourceHandleTemplateRecord",
    "ResourceTemplateCategoryRecord",
    "ResourceTemplateRecord",
    "SiteRecord",
]
