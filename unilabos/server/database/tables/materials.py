"""``materials.db`` 的 SQLModel 表记录与内嵌值对象。"""

from __future__ import annotations

from typing import ClassVar, List, Literal, Optional, Union

from pydantic import JsonValue, field_validator, model_validator
from sqlalchemy import Text
from sqlmodel import Field

from unilabos.protocol.base import JsonArray
from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)
from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    DatabaseSpec,
    TableSpec,
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
    # 批次所在库位与仓储分区（V1 lot location / warehouse zone）
    location_site_uuid: Optional[NonEmptyStr] = None
    warehouse_zone_id: str = ""

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
    display_name: NonEmptyStr
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
    ``chemical_uuid`` 引用化学品定义（可空、无外键：化学品是软删除）。
    ``chemical_unlinked`` 记录显式解除过关联：之后省略引用的写入不再按名称自动匹配，
    显式关联时清除；为 true 时 ``chemical_uuid`` 必为空。
    """

    __tablename__: ClassVar[str] = "material_substance"

    substance_uuid: NonEmptyStr = Field(primary_key=True)
    material_uuid: NonEmptyStr
    ordinal: int = Field(ge=0)
    name: NonEmptyStr
    chemical_uuid: Optional[NonEmptyStr] = None
    chemical_unlinked: bool = False
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
        if self.chemical_unlinked and self.chemical_uuid is not None:
            raise ValueError("an unlinked substance cannot reference a chemical")
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


class MaterialLinkRecord(TableObject, table=True):
    """物料/设备节点间的一条拓扑边（node-link 的 link 对象）。

    ``link_uuid`` 由 (source, target, handles, type) uuid5 稳定派生，
    开机图对齐幂等 upsert；原始 link 对象的其余字段原样入 ``extra_json``。
    边是运行态拓扑（管路/电气/handle 连接），随权威演化，可运行时增删。
    """

    __tablename__: ClassVar[str] = "material_link"

    link_uuid: NonEmptyStr = Field(primary_key=True)
    source_material_uuid: NonEmptyStr
    target_material_uuid: NonEmptyStr
    link_type: str = ""
    source_handle: str = ""
    target_handle: str = ""
    extra_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("extra_json", default_json="{}"),
    )
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_link(self) -> "MaterialLinkRecord":
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        return self


class LabGraphRecord(TableObject, table=True):
    """命名设备图快照（node-link JSON）。

    与 material/material_link 的运行当前态互补：payload 是上传/启动时刻的
    版本化存档（revision 递增、软删可复活），供 ``unilab -g <名称|uuid>``
    复用与回滚；当前真实拓扑经 material + material_link 实时序列化导出。
    """

    __tablename__: ClassVar[str] = "lab_graph"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    name: NonEmptyStr
    tags: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("tags", default_json="[]"),
    )
    payload: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("payload", default_json="{}"),
    )
    revision: int = Field(default=1, ge=1)


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


class ChemicalRecord(TableObject, table=True):
    """化学品定义：CAS / InChIKey 是可空唯一键，只在未删除的行之间唯一。"""

    __tablename__: ClassVar[str] = "chemical"

    chemical_uuid: NonEmptyStr = Field(primary_key=True)
    name: NonEmptyStr
    name_en: Optional[str] = None
    aliases: List[JsonValue] = Field(
        default_factory=list,
        sa_column=json_text_column("aliases_json", default_json="[]"),
    )
    cas: Optional[str] = None
    inchi_key: Optional[str] = None
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    molecular_weight: Optional[float] = Field(default=None, gt=0)
    density_g_per_ml: Optional[float] = Field(default=None, gt=0)
    physical_state: Literal["liquid", "solid", "gas", "unknown"] = Field(
        default="unknown", sa_type=Text
    )
    description: str = ""
    meta_data_json: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data_json", default_json="{}"),
    )
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    deleted_at_ms: Optional[int] = Field(default=None, ge=0)
    version: PositiveVersion = 1


class MaterialExecutionRecord(TableObject, table=True):
    """一次设备尝试的冻结物理计划与结算记录。"""

    __tablename__: ClassVar[str] = "material_execution"
    job_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    claim_uuid: NonEmptyStr
    request_json: JsonObject = Field(sa_column=json_text_column("request_json", default_json="{}"))
    request_hash: NonEmptyStr
    snapshot_json: JsonObject = Field(sa_column=json_text_column("snapshot_json", default_json="{}"))
    state: Literal["prepared", "settled", "released"] = Field(sa_type=Text)
    receipt_json: Optional[JsonObject] = Field(default=None, sa_column=json_text_column("receipt_json", default_json="{}", nullable=True))
    result_json: Optional[JsonObject] = Field(default=None, sa_column=json_text_column("result_json", default_json="{}", nullable=True))
    confirmation_json: Optional[JsonObject] = Field(default=None, sa_column=json_text_column("confirmation_json", default_json="{}", nullable=True))
    version: PositiveVersion = 1
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds


class MaterialExecutionHoldRecord(TableObject, table=True):
    """未结算动作对物料或库位的唯一占用。"""

    __tablename__: ClassVar[str] = "material_execution_hold"
    aggregate_type: Literal["material", "site"] = Field(sa_type=Text, primary_key=True)
    aggregate_uuid: NonEmptyStr = Field(primary_key=True)
    job_uuid: NonEmptyStr = Field(foreign_key="material_execution.job_uuid")


class MaterialContentRecord(TableObject, table=True):
    """V1 试剂 / 样品 / 当前内容物：``material_substance`` 行上的角色扩展（数量仍在 substance）。"""

    __tablename__: ClassVar[str] = "material_content"
    substance_uuid: NonEmptyStr = Field(primary_key=True)
    material_uuid: NonEmptyStr
    content_kind: Literal["reagent", "sample", "current"] = Field(sa_type=Text)
    concentration_value: Optional[float] = None
    concentration_unit: Optional[str] = None
    density_g_per_ml: Optional[float] = Field(default=None, gt=0)
    density_source: Optional[str] = None
    code: Optional[str] = None
    sample_type: Optional[str] = None
    source: Optional[str] = None
    collected_at_ms: Optional[UnixMilliseconds] = None
    expires_at_ms: Optional[UnixMilliseconds] = None
    description: str = ""
    revision: PositiveVersion = 1
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds


class MaterialContentHistoryRecord(TableObject, table=True):
    """试剂 / 样品 / 当前内容物的变更历史（V1 reagent-history / substance-history）。"""

    __tablename__: ClassVar[str] = "material_content_history"
    history_uuid: NonEmptyStr = Field(primary_key=True)
    material_uuid: NonEmptyStr
    subject_type: Literal["reagent", "sample", "current_substance"] = Field(sa_type=Text)
    subject_uuid: NonEmptyStr
    event_type: NonEmptyStr
    operator_type: str = "system"
    quantity_delta: Optional[float] = None
    quantity_unit: Optional[str] = None
    revision: Optional[int] = None
    changes: JsonObject = Field(default_factory=dict, sa_column=json_text_column("changes_json", default_json="{}"))
    extension: JsonObject = Field(default_factory=dict, sa_column=json_text_column("extension_json", default_json="{}"))
    workflow_task_uuid: Optional[str] = None
    workflow_node_job_uuid: Optional[str] = None
    trace_id: Optional[str] = None
    causation_id: Optional[str] = None
    recorded_at_ms: UnixMilliseconds


class MaterialStateHistoryRecord(TableObject, table=True):
    """物料状态追加历史（V1 ``/materials/{uuid}/states``）。"""

    __tablename__: ClassVar[str] = "material_state_history"
    state_uuid: NonEmptyStr = Field(primary_key=True)
    material_uuid: NonEmptyStr
    status: Optional[str] = None
    state_data: JsonObject = Field(default_factory=dict, sa_column=json_text_column("state_data_json", default_json="{}"))
    source: Optional[str] = None
    description: Optional[str] = None
    meta_data_json: JsonObject = Field(default_factory=dict, sa_column=json_text_column("meta_data_json", default_json="{}"))
    observed_at_ms: UnixMilliseconds
    created_at_ms: UnixMilliseconds


class MaterialContentReservationRecord(TableObject, table=True):
    """任务对容器内容物（``material_substance``）的数量预留。"""

    __tablename__: ClassVar[str] = "material_content_reservation"
    reservation_uuid: NonEmptyStr = Field(primary_key=True)
    substance_uuid: NonEmptyStr
    material_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: str = ""
    job_uuid: Optional[str] = None
    quantity: float = Field(gt=0)
    quantity_unit: NonEmptyStr
    status: Literal["active", "consumed", "released"] = Field(sa_type=Text)
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class MaterialSourceBindingRecord(TableObject, table=True):
    """任务准入时冻结的物料来源绑定；``shared_source`` 允许多任务共享同一来源。"""

    __tablename__: ClassVar[str] = "material_source_binding"
    binding_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt: int = Field(default=1, ge=1)
    material_uuid: NonEmptyStr
    template_uuid: NonEmptyStr
    custody_policy: Literal["task_exclusive", "shared_source"] = Field(sa_type=Text)
    selector: JsonObject = Field(default_factory=dict, sa_column=json_text_column("selector_json", default_json="{}"))
    status: Literal["active", "released"] = Field(default="active", sa_type=Text)
    created_at_ms: UnixMilliseconds
    released_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1


class StationIngressReservationRecord(TableObject, table=True):
    """工站入站库位预留（V1 ``/inventory/ingress-reservations``）。"""

    __tablename__: ClassVar[str] = "station_ingress_reservation"
    reservation_uuid: NonEmptyStr = Field(primary_key=True)
    idempotency_key: NonEmptyStr
    request_hash: NonEmptyStr
    backend_task_uuid: Optional[str] = None
    invocation_key: Optional[str] = None
    carrier_material_uuid: NonEmptyStr
    site_uuid: Optional[str] = None
    state: Literal["reserved", "in_transit", "received", "expired", "canceled"] = Field(sa_type=Text)
    expires_at_ms: UnixMilliseconds
    transport_started_at_ms: Optional[int] = None
    received_at_ms: Optional[int] = None
    canceled_at_ms: Optional[int] = None
    cancel_reason: Optional[str] = None
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    revision: PositiveVersion = 1


class InventoryProcessedCommandRecord(TableObject, table=True):
    """V1 类型化库存命令的首次结果（按 command_id 幂等重放）。"""

    __tablename__: ClassVar[str] = "inventory_processed_command"
    command_id: NonEmptyStr = Field(primary_key=True)
    command_type: NonEmptyStr
    request_hash: NonEmptyStr
    status: Literal["completed", "rejected"] = Field(sa_type=Text)
    result: JsonValue = Field(default=None, sa_column=json_text_column("result_json", default_json="{}"))
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    business_code: Optional[int] = None
    actor: str = ""
    processed_at_ms: UnixMilliseconds


class LabMetaRecord(TableObject, table=True):
    """实验室档案键值（名称、领域包、网格设置）。"""

    __tablename__: ClassVar[str] = "lab_meta"
    meta_key: NonEmptyStr = Field(primary_key=True)
    meta_value: JsonValue = Field(default=None, sa_column=json_text_column("meta_value_json", default_json="null"))
    updated_at_ms: UnixMilliseconds


class LabZoneRecord(TableObject, table=True):
    """实验室 2D 分区（实验台 / 仪器区 / 存储区 …）。"""

    __tablename__: ClassVar[str] = "lab_zone"
    zone_id: NonEmptyStr = Field(primary_key=True)
    name: str = ""
    kind: str = "bench"
    x: float = 0
    y: float = 0
    w: float = 100
    h: float = 100
    meta: JsonObject = Field(default_factory=dict, sa_column=json_text_column("meta_json", default_json="{}"))
    version: PositiveVersion = 1
    updated_at_ms: UnixMilliseconds


class LabPlacementRecord(TableObject, table=True):
    """分区内的在台摆放（容器实例或设备）。"""

    __tablename__: ClassVar[str] = "lab_placement"
    subject_id: NonEmptyStr = Field(primary_key=True)
    subject_kind: str = "container"
    zone_id: str = ""
    x: float = 0
    y: float = 0
    w: float = 40
    h: float = 40
    rotation: float = 0
    label: str = ""
    meta: JsonObject = Field(default_factory=dict, sa_column=json_text_column("meta_json", default_json="{}"))
    version: PositiveVersion = 1
    updated_at_ms: UnixMilliseconds


MATERIALS_TABLE_MODELS = (
    ResourceTemplateRecord,
    InventoryLotRecord,
    MaterialRecord,
    MaterialPositionRecord,
    MaterialDataTable,
    MaterialSubstanceRecord,
    SiteRecord,
    MaterialLinkRecord,
    LabGraphRecord,
    InventoryReservationRecord,
    InventoryCommandEffectRecord,
    InventoryLedgerRecord,
    ChemicalRecord,
    MaterialExecutionRecord,
    MaterialExecutionHoldRecord,
    MaterialContentRecord,
    MaterialContentHistoryRecord,
    MaterialStateHistoryRecord,
    MaterialContentReservationRecord,
    MaterialSourceBindingRecord,
    StationIngressReservationRecord,
    InventoryProcessedCommandRecord,
    LabMetaRecord,
    LabZoneRecord,
    LabPlacementRecord,
)


MATERIALS_TABLES = (
    SCHEMA_IDENTITY_TABLE,
    TableSpec(
        "resource_template",
        """
        CREATE TABLE IF NOT EXISTS resource_template (
            template_uuid TEXT PRIMARY KEY CHECK (TRIM(template_uuid) <> ''),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            display_name TEXT NOT NULL CHECK (TRIM(display_name) <> ''),
            resource_type TEXT NOT NULL CHECK (TRIM(resource_type) <> ''),
            class_name TEXT,
            module_name TEXT,
            template_version TEXT NOT NULL CHECK (TRIM(template_version) <> ''),
            category_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(category_json) AND json_type(category_json) = 'array'
            ),
            available_sites_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(available_sites_json)
                AND json_type(available_sites_json) = 'array'
            ),
            handles_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(handles_json) AND json_type(handles_json) = 'array'
            ),
            definition_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(definition_json) AND json_type(definition_json) = 'object'
            ),
            definition_hash TEXT NOT NULL CHECK (TRIM(definition_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('active','deprecated','deleted')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                (status = 'deleted' AND deleted_at_ms IS NOT NULL)
                OR (status <> 'deleted' AND deleted_at_ms IS NULL)
            ),
            CHECK (json_type(definition_json, '$.category') IS NULL),
            CHECK (json_type(definition_json, '$.available_sites') IS NULL),
            CHECK (json_type(definition_json, '$.handles') IS NULL)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_template_name_active
            ON resource_template(name) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_template_type_active
            ON resource_template(resource_type, name)
            WHERE deleted_at_ms IS NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_lot",
        """
        CREATE TABLE IF NOT EXISTS inventory_lot (
            lot_uuid TEXT PRIMARY KEY CHECK (TRIM(lot_uuid) <> ''),
            template_uuid TEXT NOT NULL,
            batch_no TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL CHECK (TRIM(unit) <> ''),
            quantity_total REAL NOT NULL CHECK (quantity_total >= 0),
            quantity_available REAL NOT NULL CHECK (quantity_available >= 0),
            quantity_reserved REAL NOT NULL CHECK (quantity_reserved >= 0),
            expiry_at_ms INTEGER CHECK (expiry_at_ms >= 0),
            quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0,1)),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            location_site_uuid TEXT,
            warehouse_zone_id TEXT NOT NULL DEFAULT '',
            CHECK (quantity_available + quantity_reserved <= quantity_total),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_lot_available
            ON inventory_lot(template_uuid, quarantined, expiry_at_ms)
            WHERE quantity_available > 0
            """,
        ),
    ),
    TableSpec(
        "material",
        """
        CREATE TABLE IF NOT EXISTS material (
            material_uuid TEXT PRIMARY KEY CHECK (TRIM(material_uuid) <> ''),
            resource_id TEXT NOT NULL CHECK (TRIM(resource_id) <> ''),
            template_uuid TEXT NOT NULL,
            parent_material_uuid TEXT,
            ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
            lot_uuid TEXT,
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            display_name TEXT NOT NULL CHECK (TRIM(display_name) <> ''),
            description TEXT NOT NULL DEFAULT '',
            resource_type TEXT NOT NULL CHECK (TRIM(resource_type) <> ''),
            class_name TEXT NOT NULL CHECK (TRIM(class_name) <> ''),
            machine_name TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL DEFAULT '',
            barcode_symbology TEXT NOT NULL DEFAULT '',
            template_name TEXT NOT NULL CHECK (TRIM(template_name) <> ''),
            resource_schema_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(resource_schema_json)
                AND json_type(resource_schema_json) = 'object'
            ),
            model_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(model_json) AND json_type(model_json) = 'object'
            ),
            icon_uri TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(config_json) AND json_type(config_json) = 'object'
            ),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN (
                'active','reserved','in_use','quarantined','consumed','retired'
            )),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (parent_material_uuid IS NULL OR parent_material_uuid <> material_uuid),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(parent_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(lot_uuid) REFERENCES inventory_lot(lot_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_root_name_active
            ON material(LOWER(name))
            WHERE parent_material_uuid IS NULL AND deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_resource_id_active
            ON material(resource_id)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active
            ON material(barcode) WHERE deleted_at_ms IS NULL AND barcode <> ''
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_parent
            ON material(parent_material_uuid, ordinal, material_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_template_status
            ON material(template_uuid, lifecycle_status, material_uuid)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_material_prevent_cycle
            BEFORE UPDATE OF parent_material_uuid ON material
            WHEN NEW.parent_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE descendants(material_uuid) AS (
                    SELECT OLD.material_uuid
                    UNION
                    SELECT material.material_uuid
                    FROM material JOIN descendants
                        ON material.parent_material_uuid = descendants.material_uuid
                )
                SELECT RAISE(ABORT, 'material tree cycle')
                WHERE NEW.parent_material_uuid IN descendants;
            END
            """,
        ),
    ),
    TableSpec(
        "material_position",
        """
        CREATE TABLE IF NOT EXISTS material_position (
            material_uuid TEXT PRIMARY KEY,
            size_depth REAL NOT NULL DEFAULT 0 CHECK (size_depth >= 0),
            size_width REAL NOT NULL DEFAULT 0 CHECK (size_width >= 0),
            size_height REAL NOT NULL DEFAULT 0 CHECK (size_height >= 0),
            scale_x REAL NOT NULL DEFAULT 0,
            scale_y REAL NOT NULL DEFAULT 0,
            scale_z REAL NOT NULL DEFAULT 0,
            layout TEXT NOT NULL DEFAULT 'x-y' CHECK (
                layout IN ('2d','x-y','z-y','x-z')
            ),
            position_x REAL,
            position_y REAL,
            position_z REAL,
            position3d_x REAL NOT NULL DEFAULT 0,
            position3d_y REAL NOT NULL DEFAULT 0,
            position3d_z REAL NOT NULL DEFAULT 0,
            rotation_x REAL NOT NULL DEFAULT 0,
            rotation_y REAL NOT NULL DEFAULT 0,
            rotation_z REAL NOT NULL DEFAULT 0,
            cross_section_type TEXT NOT NULL DEFAULT 'rectangle' CHECK (
                cross_section_type IN ('rectangle','circle','rounded_rectangle')
            ),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                (position_x IS NULL AND position_y IS NULL AND position_z IS NULL)
                OR (position_x IS NOT NULL AND position_y IS NOT NULL
                    AND position_z IS NOT NULL)
            ),
            FOREIGN KEY(material_uuid) REFERENCES material(material_uuid)
                ON DELETE CASCADE
        )
        """,
    ),
    TableSpec(
        "material_data",
        """
        CREATE TABLE IF NOT EXISTS material_data (
            material_uuid TEXT PRIMARY KEY,
            data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            sites_initialized INTEGER NOT NULL DEFAULT 0
                CHECK (sites_initialized IN (0,1)),
            unknown_counter INTEGER CHECK (unknown_counter >= 0),
            state_status TEXT NOT NULL DEFAULT 'created'
                CHECK (TRIM(state_status) <> ''),
            content_version INTEGER NOT NULL DEFAULT 1 CHECK (content_version > 0),
            state_hash TEXT NOT NULL DEFAULT '',
            source_event_uuid TEXT,
            source_job_uuid TEXT,
            source_command_uuid TEXT,
            observed_at_ms INTEGER NOT NULL DEFAULT 0 CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            FOREIGN KEY(material_uuid) REFERENCES material(material_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE TRIGGER IF NOT EXISTS trg_material_initialize_children
            AFTER INSERT ON material
            BEGIN
                INSERT INTO material_position(material_uuid, updated_at_ms)
                VALUES (NEW.material_uuid, NEW.created_at_ms);
                INSERT INTO material_data(material_uuid, updated_at_ms)
                VALUES (NEW.material_uuid, NEW.created_at_ms);
            END
            """,
        ),
    ),
    TableSpec(
        "material_substance",
        """
        CREATE TABLE IF NOT EXISTS material_substance (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            chemical_uuid TEXT CHECK (chemical_uuid IS NULL OR TRIM(chemical_uuid) <> ''),
            chemical_unlinked INTEGER NOT NULL DEFAULT 0 CHECK (
                chemical_unlinked IN (0, 1) AND (chemical_unlinked = 0 OR chemical_uuid IS NULL)
            ),
            quantity REAL NOT NULL CHECK (quantity >= 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            physical_state TEXT NOT NULL DEFAULT 'liquid' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            composition_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(composition_json)
                AND json_type(composition_json) = 'array'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            content_version INTEGER NOT NULL CHECK (content_version > 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= observed_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(material_uuid, ordinal),
            FOREIGN KEY(material_uuid) REFERENCES material_data(material_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_substance_name
            ON material_substance(LOWER(name), quantity_unit, material_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_substance_chemical
            ON material_substance(chemical_uuid) WHERE chemical_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "site",
        """
        CREATE TABLE IF NOT EXISTS site (
            site_uuid TEXT PRIMARY KEY CHECK (TRIM(site_uuid) <> ''),
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            owner_material_uuid TEXT NOT NULL,
            ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
            template_name TEXT NOT NULL CHECK (TRIM(template_name) <> ''),
            site_index NOT NULL CHECK (
                typeof(site_index) = 'integer'
                OR (typeof(site_index) = 'text' AND TRIM(site_index) <> '')
            ),
            label TEXT NOT NULL CHECK (TRIM(label) <> ''),
            visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0,1)),
            occupied_material_uuid TEXT,
            pose_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(pose_json) AND json_type(pose_json) = 'object'
            ),
            allowed_resource_categories_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(allowed_resource_categories_json)
                AND json_type(allowed_resource_categories_json) = 'array'
            ),
            parent_link TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            changed_by_job_uuid TEXT,
            changed_by_command_uuid TEXT,
            changed_at_ms INTEGER NOT NULL CHECK (changed_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                occupied_material_uuid IS NULL
                OR occupied_material_uuid <> owner_material_uuid
            ),
            CHECK (deleted_at_ms IS NULL OR occupied_material_uuid IS NULL),
            FOREIGN KEY(owner_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(occupied_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_occupied_material_active
            ON site(occupied_material_uuid)
            WHERE deleted_at_ms IS NULL AND occupied_material_uuid IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_owner_index_active
            ON site(owner_material_uuid, site_index) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_owner_label_active
            ON site(owner_material_uuid, LOWER(label)) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_site_occupant_requires_descendant_insert
            BEFORE INSERT ON site WHEN NEW.occupied_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT parent_material_uuid FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material JOIN ancestors
                        ON material.material_uuid = ancestors.material_uuid
                    WHERE material.parent_material_uuid IS NOT NULL
                )
                SELECT RAISE(ABORT, 'site occupant must be an owner descendant')
                WHERE NOT EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE material_uuid = NEW.owner_material_uuid
                );
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_site_occupant_requires_descendant_update
            BEFORE UPDATE OF occupied_material_uuid, owner_material_uuid ON site
            WHEN NEW.occupied_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT parent_material_uuid FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material JOIN ancestors
                        ON material.material_uuid = ancestors.material_uuid
                    WHERE material.parent_material_uuid IS NOT NULL
                )
                SELECT RAISE(ABORT, 'site occupant must be an owner descendant')
                WHERE NOT EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE material_uuid = NEW.owner_material_uuid
                );
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_occupied_material_parent_change
            BEFORE UPDATE OF parent_material_uuid ON material
            WHEN NEW.parent_material_uuid IS NOT OLD.parent_material_uuid
                AND EXISTS (
                    SELECT 1 FROM site
                    WHERE deleted_at_ms IS NULL
                        AND occupied_material_uuid = OLD.material_uuid
                )
            BEGIN
                SELECT RAISE(
                    ABORT, 'clear site occupant before changing material parent'
                );
            END
            """,
        ),
    ),
    TableSpec(
        "material_link",
        """
        CREATE TABLE IF NOT EXISTS material_link (
            link_uuid TEXT PRIMARY KEY CHECK (TRIM(link_uuid) <> ''),
            source_material_uuid TEXT NOT NULL CHECK (
                TRIM(source_material_uuid) <> ''
            ),
            target_material_uuid TEXT NOT NULL CHECK (
                TRIM(target_material_uuid) <> ''
            ),
            link_type TEXT NOT NULL DEFAULT '',
            source_handle TEXT NOT NULL DEFAULT '',
            target_handle TEXT NOT NULL DEFAULT '',
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            FOREIGN KEY(source_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(target_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_link_identity
            ON material_link(
                source_material_uuid, target_material_uuid,
                source_handle, target_handle, link_type
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_link_source
            ON material_link(source_material_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_link_target
            ON material_link(target_material_uuid)
            """,
        ),
    ),
    TableSpec(
        "lab_graph",
        """
        CREATE TABLE IF NOT EXISTS lab_graph (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            name TEXT NOT NULL,
            tags TEXT NOT NULL CHECK (
                json_valid(tags) AND json_type(tags) = 'array'
            ),
            payload TEXT NOT NULL CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            ),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_lab_graph_name_active
            ON lab_graph(LOWER(name)) WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_lab_graph_created_active
            ON lab_graph(create_time DESC, uuid DESC) WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_reservation",
        """
        CREATE TABLE IF NOT EXISTS inventory_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL CHECK (TRIM(node_uuid) <> ''),
            job_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(job_uuid) <> ''),
            scheduler_revision INTEGER NOT NULL CHECK (scheduler_revision >= 0),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            items_json TEXT NOT NULL CHECK (
                json_valid(items_json) AND json_type(items_json) = 'array'
                AND json_array_length(items_json) > 0
            ),
            status TEXT NOT NULL CHECK (status IN (
                'active','consumed','released','canceled','expired','quarantined'
            )),
            expires_at_ms INTEGER CHECK (expires_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_reservation_task
            ON inventory_reservation(task_uuid, node_uuid, created_at_ms)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_reservation_expiry
            ON inventory_reservation(expires_at_ms, reservation_uuid)
            WHERE status = 'active' AND expires_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_command_effect",
        """
        CREATE TABLE IF NOT EXISTS inventory_command_effect (
            command_uuid TEXT NOT NULL CHECK (TRIM(command_uuid) <> ''),
            effect_key TEXT NOT NULL CHECK (TRIM(effect_key) <> ''),
            job_uuid TEXT,
            operation TEXT NOT NULL CHECK (TRIM(operation) <> ''),
            request_json TEXT NOT NULL CHECK (
                json_valid(request_json) AND json_type(request_json) = 'object'
            ),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('applying','applied','rejected')),
            result_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(result_json) AND json_type(result_json) = 'object'
            ),
            ledger_sequence_start INTEGER,
            ledger_sequence_end INTEGER,
            error_code TEXT,
            error_message TEXT,
            started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= started_at_ms),
            completed_at_ms INTEGER CHECK (completed_at_ms >= started_at_ms),
            PRIMARY KEY(command_uuid, effect_key),
            CHECK (
                (status = 'applying' AND completed_at_ms IS NULL)
                OR (status IN ('applied','rejected') AND completed_at_ms IS NOT NULL)
            ),
            CHECK (
                (status = 'applied' AND ledger_sequence_start IS NOT NULL
                    AND ledger_sequence_end IS NOT NULL
                    AND ledger_sequence_end >= ledger_sequence_start)
                OR (status <> 'applied' AND ledger_sequence_start IS NULL
                    AND ledger_sequence_end IS NULL)
            ),
            FOREIGN KEY(ledger_sequence_start) REFERENCES inventory_ledger(sequence)
                ON DELETE RESTRICT,
            FOREIGN KEY(ledger_sequence_end) REFERENCES inventory_ledger(sequence)
                ON DELETE RESTRICT
        )
        """,
    ),
    TableSpec(
        "inventory_ledger",
        """
        CREATE TABLE IF NOT EXISTS inventory_ledger (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            aggregate_type TEXT NOT NULL CHECK (aggregate_type IN (
                'resource_template','material','site','lot','reservation'
            )),
            aggregate_uuid TEXT NOT NULL CHECK (TRIM(aggregate_uuid) <> ''),
            operation TEXT NOT NULL CHECK (TRIM(operation) <> ''),
            previous_version INTEGER NOT NULL CHECK (previous_version >= 0),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            delta_json TEXT NOT NULL CHECK (
                json_valid(delta_json) AND json_type(delta_json) = 'object'
            ),
            job_uuid TEXT,
            command_uuid TEXT,
            effect_key TEXT,
            actor_type TEXT NOT NULL CHECK (TRIM(actor_type) <> ''),
            actor_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                delivery_status IN ('pending','sent','acknowledged','dead_letter')
            ),
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            available_at_ms INTEGER NOT NULL DEFAULT 0 CHECK (available_at_ms >= 0),
            last_sent_at_ms INTEGER CHECK (last_sent_at_ms >= 0),
            acked_at_ms INTEGER CHECK (acked_at_ms >= 0),
            last_error TEXT,
            CHECK (aggregate_version = previous_version + 1),
            CHECK (
                (command_uuid IS NULL AND effect_key IS NULL)
                OR (command_uuid IS NOT NULL AND effect_key IS NOT NULL)
            ),
            CHECK (
                (delivery_status IN ('pending','sent','dead_letter')
                    AND acked_at_ms IS NULL)
                OR (delivery_status = 'acknowledged' AND acked_at_ms IS NOT NULL)
            ),
            UNIQUE(aggregate_type, aggregate_uuid, aggregate_version),
            FOREIGN KEY(command_uuid, effect_key)
                REFERENCES inventory_command_effect(command_uuid, effect_key)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_delivery
            ON inventory_ledger(delivery_status, available_at_ms, sequence)
            WHERE delivery_status IN ('pending','sent')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_aggregate
            ON inventory_ledger(aggregate_type, aggregate_uuid, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_job
            ON inventory_ledger(job_uuid, sequence) WHERE job_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "chemical",
        """
        CREATE TABLE IF NOT EXISTS chemical (
            chemical_uuid TEXT PRIMARY KEY CHECK (TRIM(chemical_uuid) <> ''),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            name_en TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(aliases_json) AND json_type(aliases_json) = 'array'
            ),
            cas TEXT CHECK (cas IS NULL OR TRIM(cas) <> ''),
            inchi_key TEXT CHECK (inchi_key IS NULL OR TRIM(inchi_key) <> ''),
            molecular_formula TEXT,
            smiles TEXT,
            molecular_weight REAL CHECK (molecular_weight IS NULL OR molecular_weight > 0),
            density_g_per_ml REAL CHECK (density_g_per_ml IS NULL OR density_g_per_ml > 0),
            physical_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            description TEXT NOT NULL DEFAULT '',
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms IS NULL OR deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_chemical_cas_active
            ON chemical(cas) WHERE deleted_at_ms IS NULL AND cas IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_chemical_inchi_key_active
            ON chemical(inchi_key) WHERE deleted_at_ms IS NULL AND inchi_key IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chemical_name_active
            ON chemical(LOWER(name)) WHERE deleted_at_ms IS NULL
            """,
        ),
    ),
)

MATERIALS_TABLES += (
    TableSpec("material_execution", """CREATE TABLE IF NOT EXISTS material_execution (
        job_uuid TEXT PRIMARY KEY, task_uuid TEXT NOT NULL, claim_uuid TEXT NOT NULL,
        request_json TEXT NOT NULL CHECK(json_valid(request_json)), request_hash TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        state TEXT NOT NULL CHECK(state IN ('prepared','settled','released')),
        receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
        result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
        confirmation_json TEXT CHECK(confirmation_json IS NULL OR json_valid(confirmation_json)),
        version INTEGER NOT NULL CHECK(version>0), created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
    )""",
        ("CREATE INDEX IF NOT EXISTS idx_material_execution_task ON material_execution(task_uuid,state)",)),
    TableSpec("material_execution_hold", """CREATE TABLE IF NOT EXISTS material_execution_hold (
        aggregate_type TEXT NOT NULL CHECK(aggregate_type IN ('material','site')),
        aggregate_uuid TEXT NOT NULL, job_uuid TEXT NOT NULL REFERENCES material_execution(job_uuid),
        PRIMARY KEY(aggregate_type,aggregate_uuid)
    )"""),
    # V1.0 库存概念并入 materials.db（单一库存权威，不另建 inventory.db）：试剂 / 样品 / 当前内容物
    # 是 material_substance 行上的角色扩展（数量仍以 material_substance 为唯一权威）；另有内容物与
    # 状态历史、内容物预留、来源绑定、工站入站预留、V1 类型化库存命令的幂等结果与实验室布局。
    TableSpec(
        "material_content",
        """
        CREATE TABLE IF NOT EXISTS material_content (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            content_kind TEXT NOT NULL CHECK (content_kind IN ('reagent','sample','current')),
            concentration_value REAL,
            concentration_unit TEXT,
            density_g_per_ml REAL CHECK (density_g_per_ml IS NULL OR density_g_per_ml > 0),
            density_source TEXT,
            code TEXT,
            sample_type TEXT,
            source TEXT,
            collected_at_ms INTEGER CHECK (collected_at_ms IS NULL OR collected_at_ms >= 0),
            expires_at_ms INTEGER CHECK (expires_at_ms IS NULL OR expires_at_ms >= 0),
            description TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            CHECK (
                (concentration_value IS NULL AND concentration_unit IS NULL)
                OR (concentration_value IS NOT NULL AND concentration_unit IS NOT NULL)
            ),
            CHECK (content_kind = 'sample' OR code IS NULL),
            CHECK (expires_at_ms IS NULL OR collected_at_ms IS NULL OR expires_at_ms >= collected_at_ms)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_content_kind
            ON material_content(material_uuid, content_kind)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_content_sample_code
            ON material_content(LOWER(code)) WHERE code IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "material_content_history",
        """
        CREATE TABLE IF NOT EXISTS material_content_history (
            history_uuid TEXT PRIMARY KEY CHECK (TRIM(history_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('reagent','sample','current_substance')),
            subject_uuid TEXT NOT NULL CHECK (TRIM(subject_uuid) <> ''),
            event_type TEXT NOT NULL CHECK (TRIM(event_type) <> ''),
            operator_type TEXT NOT NULL DEFAULT 'system',
            quantity_delta REAL,
            quantity_unit TEXT,
            revision INTEGER,
            changes_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(changes_json) AND json_type(changes_json) = 'object'
            ),
            extension_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extension_json) AND json_type(extension_json) = 'object'
            ),
            workflow_task_uuid TEXT,
            workflow_node_job_uuid TEXT,
            trace_id TEXT,
            causation_id TEXT,
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_content_history_material
            ON material_content_history(material_uuid, subject_type, recorded_at_ms, history_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_content_history_subject
            ON material_content_history(subject_uuid, recorded_at_ms, history_uuid)
            """,
        ),
    ),
    TableSpec(
        "material_state_history",
        """
        CREATE TABLE IF NOT EXISTS material_state_history (
            state_uuid TEXT PRIMARY KEY CHECK (TRIM(state_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            status TEXT,
            state_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(state_data_json) AND json_type(state_data_json) = 'object'
            ),
            source TEXT,
            description TEXT,
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_state_history_timeline
            ON material_state_history(material_uuid, observed_at_ms DESC, state_uuid DESC)
            """,
        ),
    ),
    TableSpec(
        "material_content_reservation",
        """
        CREATE TABLE IF NOT EXISTS material_content_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            substance_uuid TEXT NOT NULL CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL DEFAULT '',
            job_uuid TEXT,
            quantity REAL NOT NULL CHECK (quantity > 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            status TEXT NOT NULL CHECK (status IN ('active','consumed','released')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_active
            ON material_content_reservation(substance_uuid, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_task
            ON material_content_reservation(task_uuid, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_job
            ON material_content_reservation(job_uuid, status) WHERE job_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "material_source_binding",
        """
        CREATE TABLE IF NOT EXISTS material_source_binding (
            binding_uuid TEXT PRIMARY KEY CHECK (TRIM(binding_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL CHECK (TRIM(node_uuid) <> ''),
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            template_uuid TEXT NOT NULL CHECK (TRIM(template_uuid) <> ''),
            custody_policy TEXT NOT NULL CHECK (custody_policy IN ('task_exclusive','shared_source')),
            selector_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(selector_json) AND json_type(selector_json) = 'object'
            ),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','released')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            released_at_ms INTEGER CHECK (released_at_ms IS NULL OR released_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(task_uuid, node_uuid, attempt)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_source_binding_material
            ON material_source_binding(material_uuid, custody_policy, status)
            """,
        ),
    ),
    TableSpec(
        "station_ingress_reservation",
        """
        CREATE TABLE IF NOT EXISTS station_ingress_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            idempotency_key TEXT NOT NULL UNIQUE CHECK (TRIM(idempotency_key) <> ''),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            backend_task_uuid TEXT,
            invocation_key TEXT,
            carrier_material_uuid TEXT NOT NULL CHECK (TRIM(carrier_material_uuid) <> ''),
            site_uuid TEXT,
            state TEXT NOT NULL CHECK (
                state IN ('reserved','in_transit','received','expired','canceled')
            ),
            expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms >= 0),
            transport_started_at_ms INTEGER,
            received_at_ms INTEGER,
            canceled_at_ms INTEGER,
            cancel_reason TEXT,
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_station_ingress_active_site
            ON station_ingress_reservation(site_uuid)
            WHERE site_uuid IS NOT NULL AND state IN ('reserved','in_transit')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_station_ingress_state_expiry
            ON station_ingress_reservation(state, expires_at_ms, reservation_uuid)
            """,
        ),
    ),
    # V1 ``POST /inventory/commands`` 的类型化命令（试剂分装、内容物预留、来源绑定 …）按 command_id
    # 幂等：首次结果（完成或拒绝）原样留存，重放直接返回；也是 ``/inventory/commands/processed`` 的来源。
    TableSpec(
        "inventory_processed_command",
        """
        CREATE TABLE IF NOT EXISTS inventory_processed_command (
            command_id TEXT PRIMARY KEY CHECK (TRIM(command_id) <> ''),
            command_type TEXT NOT NULL CHECK (TRIM(command_type) <> ''),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('completed','rejected')),
            result_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
            error_code TEXT,
            error_message TEXT,
            business_code INTEGER,
            actor TEXT NOT NULL DEFAULT '',
            processed_at_ms INTEGER NOT NULL CHECK (processed_at_ms >= 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_processed_command_time
            ON inventory_processed_command(processed_at_ms DESC, command_id)
            """,
        ),
    ),
    TableSpec(
        "lab_meta",
        """
        CREATE TABLE IF NOT EXISTS lab_meta (
            meta_key TEXT PRIMARY KEY CHECK (TRIM(meta_key) <> ''),
            meta_value_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(meta_value_json)),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """,
    ),
    TableSpec(
        "lab_zone",
        """
        CREATE TABLE IF NOT EXISTS lab_zone (
            zone_id TEXT PRIMARY KEY CHECK (TRIM(zone_id) <> ''),
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'bench',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 100,
            h REAL NOT NULL DEFAULT 100,
            meta_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_json) AND json_type(meta_json) = 'object'
            ),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """,
    ),
    TableSpec(
        "lab_placement",
        """
        CREATE TABLE IF NOT EXISTS lab_placement (
            subject_id TEXT PRIMARY KEY CHECK (TRIM(subject_id) <> ''),
            subject_kind TEXT NOT NULL DEFAULT 'container',
            zone_id TEXT NOT NULL DEFAULT '',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 40,
            h REAL NOT NULL DEFAULT 40,
            rotation REAL NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_json) AND json_type(meta_json) = 'object'
            ),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS idx_lab_placement_zone ON lab_placement(zone_id)",
        ),
    ),
)


MATERIALS_DATABASE = DatabaseSpec(
    key="materials",
    filename="materials.db",
    role="resource, material, site, topology link and lab graph authority",
    synchronous="FULL",
    tables=MATERIALS_TABLES,
    # 0.13 发布线的基线：schema v1、contract v1，没有迁移。之后改 DDL 或行内容契约
    # （contract_version）时提升 schema_version 并在 migrations 里补 v2 起的迁移。
    contract_version=1,
    schema_version=1,
    migrations=(),
    # 物料是实验室的物理事实：结构或契约变化必须 schema_version +1 并给出迁移。
    # 开发期没有迁移路径时备份后重建；正式发布前改为 "refuse"（拒绝启动、保留旧库）。
    on_unmigratable="rebuild",
)

__all__ = [
    "ChemicalRecord",
    "InventoryCommandEffectRecord",
    "InventoryLedgerRecord",
    "InventoryLotRecord",
    "InventoryReservationRecord",
    "LabGraphRecord",
    "MaterialDataRecord",
    "MaterialDataTable",
    "MaterialLinkRecord",
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
