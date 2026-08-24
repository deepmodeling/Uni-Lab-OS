"""``materials.v1`` 物料传输模型。

数据库 Record 描述表行；这里的模型描述稳定的通信协议，因此 JSON 字段在
线协议中不带 ``_json`` 后缀，substance 也始终是具名对象而不是三元组。
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import Field, JsonValue, model_validator

from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.materials import ResourceTemplateHandle


class ResourceTemplateWrite(ServerObject):
    template_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    display_name: Optional[NonEmptyStr] = None
    resource_type: NonEmptyStr = "resource"
    class_name: Optional[str] = None
    module_name: Optional[str] = None
    template_version: NonEmptyStr = "1"
    category: list[str] = Field(default_factory=list)
    available_sites: list[JsonObject] = Field(default_factory=list)
    handles: list[ResourceTemplateHandle] = Field(default_factory=list)
    definition: JsonObject = Field(default_factory=dict)
    status: Literal["active", "deprecated"] = "active"


class ResourceTemplateRead(ResourceTemplateWrite):
    template_uuid: NonEmptyStr
    definition_hash: NonEmptyStr
    status: Literal["active", "deprecated", "deleted"] = "active"
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    deleted_at_ms: Optional[int] = Field(default=None, ge=0)
    version: int = Field(ge=1)


class MaterialPosition(ServerObject):
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
    cross_section_type: Literal[
        "rectangle", "circle", "rounded_rectangle"
    ] = "rectangle"
    extra: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_optional_position(self) -> "MaterialPosition":
        values = (self.position_x, self.position_y, self.position_z)
        if any(value is None for value in values) and any(
            value is not None for value in values
        ):
            raise ValueError("position_x/y/z must be all null or all set")
        return self


class MaterialSubstance(ServerObject):
    substance_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    quantity: float = Field(ge=0)
    quantity_unit: NonEmptyStr
    physical_state: Literal["liquid", "solid", "gas", "unknown"] = "liquid"
    composition: list[JsonValue] = Field(default_factory=list)
    meta_data: JsonObject = Field(default_factory=dict)


class MaterialDataWrite(ServerObject):
    data: JsonObject = Field(default_factory=dict)
    substances: list[MaterialSubstance] = Field(default_factory=list)
    sites_initialized: bool = False
    unknown_counter: Optional[int] = Field(default=None, ge=0)
    state_status: NonEmptyStr = "created"
    source_event_uuid: Optional[NonEmptyStr] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: int = Field(default=0, ge=0)


class MaterialDataRead(MaterialDataWrite):
    content_version: int = Field(ge=1)
    state_hash: NonEmptyStr
    updated_at_ms: int = Field(ge=0)
    version: int = Field(ge=1)


class MaterialIdentityWrite(ServerObject):
    resource_id: NonEmptyStr
    parent_material_uuid: Optional[NonEmptyStr] = None
    lot_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    description: str = ""
    resource_type: NonEmptyStr = "resource"
    class_name: NonEmptyStr = "Resource"
    machine_name: str = ""
    barcode: str = ""
    barcode_symbology: str = ""
    template_name: NonEmptyStr
    resource_schema: JsonObject = Field(default_factory=dict)
    model: JsonObject = Field(default_factory=dict)
    icon_uri: str = ""
    config: JsonObject = Field(default_factory=dict)
    extra: JsonObject = Field(default_factory=dict)
    meta_data: JsonObject = Field(default_factory=dict)
    lifecycle_status: Literal[
        "active", "reserved", "in_use", "quarantined", "consumed", "retired"
    ] = "active"


class MaterialIdentityRead(MaterialIdentityWrite):
    template_uuid: NonEmptyStr
    material_uuid: NonEmptyStr
    ordinal: int = Field(default=0, ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    deleted_at_ms: Optional[int] = Field(default=None, ge=0)
    version: int = Field(ge=1)


class SiteWrite(ServerObject):
    site_uuid: Optional[NonEmptyStr] = None
    schema_version: Literal[1] = 1
    template_name: NonEmptyStr
    site_index: Union[int, NonEmptyStr]
    label: NonEmptyStr
    visible: bool = True
    occupied_material_uuid: Optional[NonEmptyStr] = None
    pose: JsonObject = Field(default_factory=dict)
    allowed_resource_categories: list[str] = Field(default_factory=list)
    parent_link: str = ""
    description: str = ""
    meta_data: JsonObject = Field(default_factory=dict)
    extra: JsonObject = Field(default_factory=dict)


class SiteCreate(ServerObject):
    """创建树中的 Site；只允许关联 client_ref，不接受实例 UUID。"""

    schema_version: Literal[1] = 1
    template_name: NonEmptyStr
    site_index: Union[int, NonEmptyStr]
    label: NonEmptyStr
    visible: bool = True
    occupied_client_ref: Optional[NonEmptyStr] = None
    pose: JsonObject = Field(default_factory=dict)
    allowed_resource_categories: list[str] = Field(default_factory=list)
    parent_link: str = ""
    description: str = ""
    meta_data: JsonObject = Field(default_factory=dict)
    extra: JsonObject = Field(default_factory=dict)


class SiteRead(SiteWrite):
    site_uuid: NonEmptyStr
    owner_material_uuid: NonEmptyStr
    ordinal: int = Field(default=0, ge=0)
    changed_by_job_uuid: Optional[NonEmptyStr] = None
    changed_by_command_uuid: Optional[NonEmptyStr] = None
    changed_at_ms: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    deleted_at_ms: Optional[int] = Field(default=None, ge=0)
    version: int = Field(ge=1)


class MaterialNodeCreate(ServerObject):
    """待创建树中的一项；client_ref 只用于解析父子关系和返回 UUID 映射。"""

    client_ref: NonEmptyStr
    parent_client_ref: Optional[NonEmptyStr] = None
    ordinal: Optional[int] = Field(default=None, ge=0)
    identity: MaterialIdentityWrite
    position: MaterialPosition = Field(default_factory=MaterialPosition)
    data: MaterialDataWrite = Field(default_factory=MaterialDataWrite)
    sites: list[SiteCreate] = Field(default_factory=list)


class MaterialTreeCreate(ServerObject):
    nodes: list[MaterialNodeCreate]

    @model_validator(mode="after")
    def _validate_tree(self) -> "MaterialTreeCreate":
        if not self.nodes:
            raise ValueError("material tree requires at least one node")
        refs = [node.client_ref for node in self.nodes]
        if len(refs) != len(set(refs)):
            raise ValueError("material tree client_ref values must be unique")
        ref_set = set(refs)
        known: set[str] = set()
        roots = 0
        for node in self.nodes:
            if node.parent_client_ref is None:
                roots += 1
            elif node.parent_client_ref not in known:
                raise ValueError("material tree must be parent-first")
            for site in node.sites:
                if (
                    site.occupied_client_ref is not None
                    and site.occupied_client_ref not in ref_set
                ):
                    raise ValueError(
                        "site occupied_client_ref must reference a node in the create tree"
                    )
            known.add(node.client_ref)
        if roots != 1:
            raise ValueError("material tree requires exactly one root")
        return self


class MaterialAggregateRead(ServerObject):
    material: MaterialIdentityRead
    position: MaterialPosition
    position_version: int = Field(ge=1)
    data: MaterialDataRead
    sites: list[SiteRead] = Field(default_factory=list)
    state_hash: NonEmptyStr


class MaterialTreeRead(ServerObject):
    root_material_uuid: NonEmptyStr
    snapshot_sequence: int = Field(ge=0)
    nodes: list[MaterialAggregateRead]
    client_ref_map: dict[str, str] = Field(default_factory=dict)
    state_hash: NonEmptyStr


class MaterialPatch(ServerObject):
    name: Optional[NonEmptyStr] = None
    description: Optional[str] = None
    machine_name: Optional[str] = None
    barcode: Optional[str] = None
    barcode_symbology: Optional[str] = None
    icon_uri: Optional[str] = None
    config: Optional[JsonObject] = None
    extra: Optional[JsonObject] = None
    meta_data: Optional[JsonObject] = None
    lifecycle_status: Optional[
        Literal[
            "active",
            "reserved",
            "in_use",
            "quarantined",
            "consumed",
            "retired",
        ]
    ] = None


class MaterialMove(ServerObject):
    material_uuid: NonEmptyStr
    destination_site_uuid: Optional[NonEmptyStr] = None
    parent_material_uuid: Optional[NonEmptyStr] = None


class MaterialTransferItem(ServerObject):
    """一次跨设备转运中的单个物料及其权威目标位置。"""

    material_uuid: NonEmptyStr
    target_material_uuid: NonEmptyStr
    target_site: Optional[Union[int, NonEmptyStr]] = None


class MaterialTransfer(ServerObject):
    """由微后端提交位置并驱动两端 resource service 的转运请求。"""

    source_device_id: NonEmptyStr
    target_device_id: NonEmptyStr
    items: list[MaterialTransferItem]

    @model_validator(mode="after")
    def _validate_transfer(self) -> "MaterialTransfer":
        if not self.items:
            raise ValueError("material transfer requires at least one item")
        material_uuids = [item.material_uuid for item in self.items]
        if len(material_uuids) != len(set(material_uuids)):
            raise ValueError("material transfer cannot contain duplicate materials")
        return self


class MaterialTransferResult(ServerObject):
    """权威位置提交结果；返回成功即表示两端同步也已确认。"""

    source_device_id: NonEmptyStr
    target_device_id: NonEmptyStr
    material_uuids: list[NonEmptyStr]
    target_material_uuids: list[NonEmptyStr]
    destination_site_uuids: list[Optional[NonEmptyStr]]
    materials: list[MaterialAggregateRead]


class MaterialDeviceSync(ServerObject):
    """微后端发给设备内建 resource service 的幂等同步命令。"""

    transfer_uuid: NonEmptyStr
    device_id: NonEmptyStr
    action: Literal["unload", "load"]
    material_uuids: list[NonEmptyStr]
    destination_site_uuids: list[Optional[NonEmptyStr]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _validate_sync(self) -> "MaterialDeviceSync":
        if not self.material_uuids:
            raise ValueError("material device sync requires at least one material")
        if self.action == "load" and len(self.destination_site_uuids) != len(
            self.material_uuids
        ):
            raise ValueError("load sync requires one destination Site per material")
        if self.action == "unload" and self.destination_site_uuids:
            raise ValueError("unload sync must not include destination Sites")
        return self


class InventoryLotInbound(ServerObject):
    """登记或补充一批可按数量扣减的试剂/耗材。"""

    lot_uuid: Optional[NonEmptyStr] = None
    template_uuid: NonEmptyStr
    batch_no: str = ""
    unit: NonEmptyStr
    quantity: float = Field(gt=0)
    expiry_at_ms: Optional[int] = Field(default=None, ge=0)


class InventoryLotRead(ServerObject):
    lot_uuid: NonEmptyStr
    template_uuid: NonEmptyStr
    batch_no: str = ""
    unit: NonEmptyStr
    quantity_total: float = Field(ge=0)
    quantity_available: float = Field(ge=0)
    quantity_reserved: float = Field(ge=0)
    expiry_at_ms: Optional[int] = Field(default=None, ge=0)
    quarantined: bool = False
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    version: int = Field(ge=1)


class InventoryRequirement(ServerObject):
    """调度器冻结的一项库存需求。

    ``material`` 表示独立物料实例，只改变生命周期而不扣数量；``reagent``
    表示可计量库存，按 lot FIFO 从 available 预留并在动作开始时扣减。
    """

    key: NonEmptyStr
    kind: Literal["material", "reagent"]
    material_uuid: Optional[NonEmptyStr] = None
    template_uuid: Optional[NonEmptyStr] = None
    lot_uuid: Optional[NonEmptyStr] = None
    parent_material_uuid: Optional[NonEmptyStr] = None
    site_uuid: Optional[NonEmptyStr] = None
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def _validate_requirement(self) -> "InventoryRequirement":
        if self.kind == "material":
            if self.quantity is not None or self.unit is not None or self.lot_uuid is not None:
                raise ValueError("material requirement cannot carry lot quantity fields")
            if (self.material_uuid is None) == (self.template_uuid is None):
                raise ValueError(
                    "material requirement needs exactly one of material_uuid/template_uuid"
                )
            if self.material_uuid is not None and (
                self.parent_material_uuid is not None or self.site_uuid is not None
            ):
                raise ValueError(
                    "exact material requirement cannot also carry warehouse selectors"
                )
            return self
        if self.material_uuid is not None or self.parent_material_uuid is not None:
            raise ValueError("reagent requirement cannot select a material instance")
        if self.quantity is None or self.unit is None:
            raise ValueError("reagent requirement needs quantity and unit")
        if self.lot_uuid is None and self.template_uuid is None:
            raise ValueError("reagent requirement needs lot_uuid or template_uuid")
        if self.site_uuid is not None:
            raise ValueError("reagent requirement cannot carry site_uuid")
        return self


class InventoryReservationCreate(ServerObject):
    reservation_uuid: Optional[NonEmptyStr] = None
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    requirements: list[InventoryRequirement]
    expires_at_ms: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_requirements(self) -> "InventoryReservationCreate":
        if not self.requirements:
            raise ValueError("inventory reservation requires at least one requirement")
        keys = [item.key for item in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("inventory requirement keys must be unique per job")
        return self


class InventoryTaskReservationCreate(ServerObject):
    """后端调度器一次性提交整张任务的 Job 库存需求。"""

    task_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    reservations: list[InventoryReservationCreate]

    @model_validator(mode="after")
    def _validate_task_reservations(self) -> "InventoryTaskReservationCreate":
        if not self.reservations:
            raise ValueError("task inventory reservation requires at least one job")
        jobs = [item.job_uuid for item in self.reservations]
        nodes = [item.node_uuid for item in self.reservations]
        if len(jobs) != len(set(jobs)):
            raise ValueError("task inventory reservation contains duplicate jobs")
        if len(nodes) != len(set(nodes)):
            raise ValueError("task inventory reservation contains duplicate nodes")
        if any(item.task_uuid != self.task_uuid for item in self.reservations):
            raise ValueError("all inventory reservations must belong to the task")
        if any(
            item.scheduler_revision != self.scheduler_revision
            for item in self.reservations
        ):
            raise ValueError("all inventory reservations must use one scheduler revision")
        return self


class InventoryAllocation(ServerObject):
    key: NonEmptyStr
    kind: Literal["material", "reagent"]
    material_uuid: Optional[NonEmptyStr] = None
    template_uuid: NonEmptyStr
    lot_uuid: Optional[NonEmptyStr] = None
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[NonEmptyStr] = None


class InventoryReservationRead(ServerObject):
    reservation_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    request_hash: NonEmptyStr
    items: list[InventoryAllocation]
    status: Literal[
        "active", "consumed", "released", "canceled", "expired", "quarantined"
    ]
    expires_at_ms: Optional[int] = Field(default=None, ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    version: int = Field(ge=1)


class InventoryTaskReservationRead(ServerObject):
    task_uuid: NonEmptyStr
    scheduler_revision: int = Field(ge=0)
    reservations: list[InventoryReservationRead]


class InventoryReservationTransition(ServerObject):
    reservation_uuid: NonEmptyStr
    reason: str = ""


class MaterialDelete(ServerObject):
    material_uuid: NonEmptyStr
    recursive: bool = False


class MaterialDeleteResult(ServerObject):
    root_material_uuid: NonEmptyStr
    deleted_material_uuids: list[NonEmptyStr]
    deleted_site_uuids: list[NonEmptyStr] = Field(default_factory=list)


class MaterialSnapshot(ServerObject):
    root_material_uuid: NonEmptyStr
    nodes: list[MaterialAggregateRead]
    state_hash: Optional[NonEmptyStr] = None


class MaterialSnapshotChange(ServerObject):
    aggregate_type: Literal["material", "site"]
    aggregate_uuid: NonEmptyStr
    section: Literal["identity", "position", "data", "site", "topology"]
    before_hash: Optional[NonEmptyStr] = None
    after_hash: Optional[NonEmptyStr] = None
    changed_fields: list[str] = Field(default_factory=list)


class MaterialSnapshotDiff(ServerObject):
    root_material_uuid: NonEmptyStr
    base_state_hash: NonEmptyStr
    observed_state_hash: NonEmptyStr
    changes: list[MaterialSnapshotChange] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


__all__ = [
    "InventoryAllocation",
    "InventoryLotInbound",
    "InventoryLotRead",
    "InventoryRequirement",
    "InventoryReservationCreate",
    "InventoryReservationRead",
    "InventoryReservationTransition",
    "InventoryTaskReservationCreate",
    "InventoryTaskReservationRead",
    "MaterialAggregateRead",
    "MaterialDataRead",
    "MaterialDataWrite",
    "MaterialDelete",
    "MaterialDeleteResult",
    "MaterialDeviceSync",
    "MaterialIdentityRead",
    "MaterialIdentityWrite",
    "MaterialMove",
    "MaterialNodeCreate",
    "MaterialPatch",
    "MaterialPosition",
    "MaterialSnapshot",
    "MaterialSnapshotChange",
    "MaterialSnapshotDiff",
    "MaterialSubstance",
    "MaterialTreeCreate",
    "MaterialTreeRead",
    "MaterialTransfer",
    "MaterialTransferItem",
    "MaterialTransferResult",
    "ResourceTemplateRead",
    "ResourceTemplateWrite",
    "SiteCreate",
    "SiteRead",
    "SiteWrite",
]
