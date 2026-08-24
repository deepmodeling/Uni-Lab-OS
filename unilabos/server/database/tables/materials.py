"""``materials.db`` 的 SQLModel 表记录与内嵌值对象。"""

from __future__ import annotations

from typing import ClassVar, List, Literal, Optional, Union

from pydantic import JsonValue, field_validator, model_validator
from sqlalchemy import Text
from sqlmodel import Field

from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)
from unilabos.server.database.migrations.v1.materials import (
    MATERIALS_DATABASE,
    MATERIALS_TABLES,
)


def _normalize_string_list(values: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("string list can only contain non-empty strings")
        normalized = value.strip()
        key = normalized.casefold()
        if key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _normalize_site_index(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("site index cannot be a boolean")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("site index cannot be empty")
    return value


class ResourceTemplateHandle(ServerObject):
    """ResourceTemplate 内嵌的 handle 定义，不是独立数据库记录。"""

    key: NonEmptyStr
    label: NonEmptyStr
    io_type: Literal["source", "target", "bidirectional"]
    data_type: NonEmptyStr
    side: Optional[Literal["NORTH", "SOUTH", "EAST", "WEST"]] = None
    data_key: Optional[NonEmptyStr] = None
    data_source: Optional[NonEmptyStr] = None
    description: str = ""
    handle_schema: JsonObject = Field(default_factory=dict)
    meta_data: JsonObject = Field(default_factory=dict)


class ResourceTemplateRecord(TableObject, table=True):
    """一行保存完整模板；category、Site 定义和 handle 都是模型字段。"""

    __tablename__: ClassVar[str] = "resource_template"

    template_uuid: NonEmptyStr = Field(primary_key=True)
    name: NonEmptyStr
    display_name: NonEmptyStr
    resource_type: NonEmptyStr
    class_name: Optional[str] = None
    module_name: Optional[str] = None
    template_version: NonEmptyStr
    category: List[str] = Field(
        default_factory=list,
        sa_column=json_text_column("category_json", default_json="[]"),
    )
    available_sites: List[JsonObject] = Field(
        default_factory=list,
        sa_column=json_text_column("available_sites_json", default_json="[]"),
    )
    handles: List[ResourceTemplateHandle] = Field(
        default_factory=list,
        sa_column=json_text_column("handles_json", default_json="[]"),
    )
    definition_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("definition_json", default_json="{}"),
    )
    definition_hash: NonEmptyStr
    status: Literal["active", "deprecated", "deleted"] = Field(sa_type=Text)
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    _normalize_category = field_validator("category")(_normalize_string_list)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "ResourceTemplateRecord":
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if (self.status == "deleted") != (self.deleted_at_ms is not None):
            raise ValueError("deleted template status and deleted_at_ms must agree")
        duplicated = {"category", "available_sites", "handles"} & set(
            self.definition_json
        )
        if duplicated:
            names = ", ".join(sorted(duplicated))
            raise ValueError(
                f"promoted template fields duplicated in definition: {names}"
            )
        return self


class InventoryLotRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "inventory_lot"

    lot_uuid: NonEmptyStr = Field(primary_key=True)
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


class MaterialRecord(TableObject, table=True):
    """Material 身份和低频静态字段；位置与动态内容使用独立模型。"""

    __tablename__: ClassVar[str] = "material"

    material_uuid: NonEmptyStr = Field(primary_key=True)
    resource_id: NonEmptyStr
    template_uuid: NonEmptyStr
    parent_material_uuid: Optional[NonEmptyStr] = None
    ordinal: int = Field(default=0, ge=0)
    lot_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    description: str = ""
    resource_type: NonEmptyStr
    class_name: NonEmptyStr
    machine_name: str = ""
    barcode: str = ""
    barcode_symbology: str = ""
    template_name: NonEmptyStr
    resource_schema_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("resource_schema_json", default_json="{}"),
    )
    model_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("model_json", default_json="{}"),
    )
    icon_uri: str = ""
    config_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("config_json", default_json="{}"),
    )
    extra_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("extra_json", default_json="{}"),
    )
    meta_data_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data_json", default_json="{}"),
    )
    lifecycle_status: Literal[
        "active", "reserved", "in_use", "quarantined", "consumed", "retired"
    ] = Field(sa_type=Text)
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
        return self


class MaterialPositionRecord(TableObject, table=True):
    """ResourceDictPosition 的独立 1:1 存储模型。"""

    __tablename__: ClassVar[str] = "material_position"

    material_uuid: NonEmptyStr = Field(primary_key=True)
    size_depth: float = Field(default=0, ge=0)
    size_width: float = Field(default=0, ge=0)
    size_height: float = Field(default=0, ge=0)
    scale_x: float = 0
    scale_y: float = 0
    scale_z: float = 0
    layout: Literal["2d", "x-y", "z-y", "x-z"] = Field(
        default="x-y", sa_type=Text
    )
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    position_z: Optional[float] = None
    position3d_x: float = 0
    position3d_y: float = 0
    position3d_z: float = 0
    rotation_x: float = 0
    rotation_y: float = 0
    rotation_z: float = 0
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"] = Field(
        default="rectangle", sa_type=Text
    )
    extra_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("extra_json", default_json="{}"),
    )
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_position(self) -> "MaterialPositionRecord":
        values = (self.position_x, self.position_y, self.position_z)
        if any(value is None for value in values) and any(
            value is not None for value in values
        ):
            raise ValueError("position_x/y/z must be all null or all set")
        return self


class MaterialSubstanceRecord(TableObject, table=True):
    """MaterialData 下的一份 current substance。

    ``name/quantity/quantity_unit`` 与 canonical ``LiquidStateEntry`` 三元组直接对应。
    """

    __tablename__: ClassVar[str] = "material_substance"

    substance_uuid: NonEmptyStr = Field(primary_key=True)
    material_uuid: NonEmptyStr
    ordinal: int = Field(ge=0)
    name: NonEmptyStr
    quantity: float = Field(ge=0)
    quantity_unit: NonEmptyStr
    physical_state: Literal["liquid", "solid", "gas", "unknown"] = Field(
        default="liquid", sa_type=Text
    )
    composition: List[JsonValue] = Field(
        default_factory=list,
        sa_column=json_text_column("composition_json", default_json="[]"),
    )
    meta_data_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data_json", default_json="{}"),
    )
    content_version: PositiveVersion
    observed_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "MaterialSubstanceRecord":
        if self.updated_at_ms < self.observed_at_ms:
            raise ValueError("updated_at_ms cannot precede observed_at_ms")
        return self


class _MaterialDataColumns(TableObject):
    """``material_data`` 行字段；聚合模型额外 hydration substances。"""

    material_uuid: NonEmptyStr
    data_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("data_json", default_json="{}"),
    )
    sites_initialized: bool = False
    unknown_counter: Optional[int] = Field(default=None, ge=0)
    state_status: NonEmptyStr = "created"
    content_version: PositiveVersion = 1
    state_hash: str = ""
    source_event_uuid: Optional[NonEmptyStr] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds = 0
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class MaterialDataTable(_MaterialDataColumns, table=True):
    """纯表映射；``substances`` 实际存放在 material_substance。"""

    __tablename__: ClassVar[str] = "material_data"

    # 该表共享聚合字段基类，显式关闭 ORM hydration 的半成品赋值校验。
    model_config = {**_MaterialDataColumns.model_config, "validate_assignment": False}

    material_uuid: NonEmptyStr = Field(primary_key=True)


class MaterialDataRecord(_MaterialDataColumns):
    """Material 动态数据与已 hydration 的 substances 聚合。"""

    substances: List[MaterialSubstanceRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_substances(self) -> "MaterialDataRecord":
        if any(item.material_uuid != self.material_uuid for item in self.substances):
            raise ValueError("substance material_uuid must match MaterialData owner")
        ordinals = [item.ordinal for item in self.substances]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("substance ordinals must be unique within MaterialData")
        return self


class SiteRecord(TableObject, table=True):
    """一行对应一个完整 ResourceSite 当前快照。"""

    __tablename__: ClassVar[str] = "site"

    site_uuid: NonEmptyStr = Field(primary_key=True)
    schema_version: Literal[1] = Field(default=1, sa_type=Text)
    owner_material_uuid: NonEmptyStr
    ordinal: int = Field(default=0, ge=0)
    template_name: NonEmptyStr
    # v1 SQLite 列允许整数或文本；SQLModel 仅用 TEXT 提供可生成的列亲和性。
    site_index: Union[int, NonEmptyStr] = Field(sa_type=Text)
    label: NonEmptyStr
    visible: bool = True
    occupied_material_uuid: Optional[NonEmptyStr] = None
    pose: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("pose_json", default_json="{}"),
    )
    allowed_resource_categories: List[str] = Field(
        default_factory=list,
        sa_column=json_text_column(
            "allowed_resource_categories_json", default_json="[]"
        ),
    )
    parent_link: str = ""
    description: str = ""
    meta_data_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data_json", default_json="{}"),
    )
    extra_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("extra_json", default_json="{}"),
    )
    changed_by_job_uuid: Optional[NonEmptyStr] = None
    changed_by_command_uuid: Optional[NonEmptyStr] = None
    changed_at_ms: UnixMilliseconds
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    _strict_site_index = field_validator("site_index", mode="before")(
        _normalize_site_index
    )
    _normalize_categories = field_validator("allowed_resource_categories")(
        _normalize_string_list
    )

    @model_validator(mode="after")
    def _validate_site(self) -> "SiteRecord":
        if self.occupied_material_uuid == self.owner_material_uuid:
            raise ValueError("site owner cannot occupy its own Site")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if self.deleted_at_ms is not None and self.occupied_material_uuid is not None:
            raise ValueError("occupied Site cannot be deleted")
        return self


ReservationStatus = Literal[
    "active", "consumed", "released", "canceled", "expired", "quarantined"
]


class InventoryReservationRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "inventory_reservation"

    reservation_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    request_hash: NonEmptyStr
    items: List[JsonObject] = Field(
        sa_column=json_text_column("items_json", default_json="[]")
    )
    status: ReservationStatus = Field(sa_type=Text)
    expires_at_ms: Optional[UnixMilliseconds] = None
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_reservation(self) -> "InventoryReservationRecord":
        if not self.items:
            raise ValueError("reservation requires at least one item")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        return self


class InventoryCommandEffectRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "inventory_command_effect"

    command_uuid: NonEmptyStr = Field(primary_key=True)
    effect_key: NonEmptyStr = Field(primary_key=True)
    job_uuid: Optional[NonEmptyStr] = None
    operation: NonEmptyStr
    request_json: JsonObject = Field(
        sa_column=json_text_column("request_json", default_json="{}")
    )
    request_hash: NonEmptyStr
    status: Literal["applying", "applied", "rejected"] = Field(sa_type=Text)
    result_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("result_json", default_json="{}"),
    )
    ledger_sequence_start: Optional[int] = Field(default=None, ge=1)
    ledger_sequence_end: Optional[int] = Field(default=None, ge=1)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    started_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    completed_at_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_effect(self) -> "InventoryCommandEffectRecord":
        if (self.status == "applying") != (self.completed_at_ms is None):
            raise ValueError("effect status and completed_at_ms must agree")
        has_range = self.ledger_sequence_start is not None
        if has_range != (self.ledger_sequence_end is not None):
            raise ValueError("ledger range endpoints must be set together")
        if self.status == "applied" and not has_range:
            raise ValueError("applied effect requires a ledger range")
        if self.status != "applied" and has_range:
            raise ValueError("only applied effect may have a ledger range")
        return self


class InventoryLedgerRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "inventory_ledger"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    event_uuid: NonEmptyStr
    aggregate_type: Literal[
        "resource_template", "material", "site", "lot", "reservation"
    ] = Field(sa_type=Text)
    aggregate_uuid: NonEmptyStr
    operation: NonEmptyStr
    previous_version: int = Field(ge=0)
    aggregate_version: PositiveVersion
    state_hash: NonEmptyStr
    delta_json: JsonObject = Field(
        sa_column=json_text_column("delta_json", default_json="{}")
    )
    job_uuid: Optional[NonEmptyStr] = None
    command_uuid: Optional[NonEmptyStr] = None
    effect_key: Optional[NonEmptyStr] = None
    actor_type: NonEmptyStr
    actor_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds
    delivery_status: Literal["pending", "sent", "acknowledged", "dead_letter"] = Field(
        default="pending", sa_type=Text
    )
    delivery_attempt_count: int = Field(default=0, ge=0)
    available_at_ms: UnixMilliseconds = 0
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    last_error: Optional[str] = None

    @model_validator(mode="after")
    def _validate_ledger(self) -> "InventoryLedgerRecord":
        if self.aggregate_version != self.previous_version + 1:
            raise ValueError("aggregate version must advance by exactly one")
        if (self.command_uuid is None) != (self.effect_key is None):
            raise ValueError("command_uuid and effect_key must be set together")
        if (self.delivery_status == "acknowledged") != (self.acked_at_ms is not None):
            raise ValueError("delivery status and acked_at_ms must agree")
        return self


MATERIALS_TABLE_MODELS = (
    ResourceTemplateRecord,
    InventoryLotRecord,
    MaterialRecord,
    MaterialPositionRecord,
    MaterialDataTable,
    MaterialSubstanceRecord,
    SiteRecord,
    InventoryReservationRecord,
    InventoryCommandEffectRecord,
    InventoryLedgerRecord,
)


__all__ = [
    "InventoryCommandEffectRecord",
    "InventoryLedgerRecord",
    "InventoryLotRecord",
    "InventoryReservationRecord",
    "MaterialDataRecord",
    "MaterialDataTable",
    "MaterialRecord",
    "MaterialPositionRecord",
    "MaterialSubstanceRecord",
    "MATERIALS_DATABASE",
    "MATERIALS_TABLE_MODELS",
    "MATERIALS_TABLES",
    "ResourceTemplateHandle",
    "ResourceTemplateRecord",
    "SiteRecord",
]
