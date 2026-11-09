"""``materials.v1`` 物料传输模型与写请求幂等信封。

数据库 Record 描述表行；这里的模型描述稳定的通信协议，因此 JSON 字段在
线协议中不带 ``_json`` 后缀，substance 也始终是具名对象而不是三元组。
所有写请求共用 ``InventoryMutation`` 幂等信封，成功结果以
``MutationResult`` 携带 ledger sequence 范围与受影响聚合版本。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, Optional, TypeVar, Union

from pydantic import (
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from unilabos.protocol.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.materials import ResourceTemplateHandle


PROTOCOL_VERSION = "materials.v1"


AggregateType = Literal[
    "resource_template", "material", "site", "lot", "reservation"
]


#: ``InventoryMutation.actor_type`` 的规范取值。字段仍是开放字符串（兼容旧
#: 客户端），但进程内写点应从这里取值，前端据此渲染变更来源 tag：
#:
#: - ``human``          前端/操作员直接编辑（浏览器请求应显式携带）；
#: - ``graph``          开机图物料对齐（``materials.ensure``，actor_uuid=图 uuid）；
#: - ``registry``       Registry 资源模板同步（``sync_template``）；
#: - ``device``         设备驱动创建/快照/转移（actor_uuid=设备 id/uuid）；
#: - ``virtual_device`` 虚拟设备；
#: - ``scheduler``      调度器库存预留/释放；
#: - ``workflow``       工作流动作（如出库扣减 ``apply_deduct_resource``）；
#: - ``backend``        云端/旧后端同步进来的变更；
#: - ``edge``           未细分的 Edge 进程内写点（兜底默认值）。
ACTOR_HUMAN = "human"
ACTOR_GRAPH = "graph"
ACTOR_REGISTRY = "registry"
ACTOR_DEVICE = "device"
ACTOR_VIRTUAL_DEVICE = "virtual_device"
ACTOR_SCHEDULER = "scheduler"
ACTOR_WORKFLOW = "workflow"
ACTOR_BACKEND = "backend"
ACTOR_EDGE = "edge"
KNOWN_ACTOR_TYPES: frozenset[str] = frozenset(
    {
        ACTOR_HUMAN,
        ACTOR_GRAPH,
        ACTOR_REGISTRY,
        ACTOR_DEVICE,
        ACTOR_VIRTUAL_DEVICE,
        ACTOR_SCHEDULER,
        ACTOR_WORKFLOW,
        ACTOR_BACKEND,
        ACTOR_EDGE,
    }
)


class AggregatePrecondition(ServerObject):
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    expected_version: Optional[int] = Field(default=None, ge=0)
    expected_state_hash: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def _require_condition(self) -> "AggregatePrecondition":
        if self.expected_version is None and self.expected_state_hash is None:
            raise ValueError("precondition requires expected_version or state hash")
        return self


class InventoryMutation(ServerObject):
    """所有写请求共用的幂等信封。"""

    protocol_version: Literal["materials.v1"] = PROTOCOL_VERSION
    command_uuid: NonEmptyStr
    effect_key: NonEmptyStr
    operation: NonEmptyStr
    #: 变更来源，取值见 ``KNOWN_ACTOR_TYPES``；前端按此渲染来源 tag。
    actor_type: NonEmptyStr = ACTOR_EDGE
    actor_uuid: Optional[NonEmptyStr] = None
    job_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: int = Field(default=0, ge=0)
    preconditions: list[AggregatePrecondition] = Field(default_factory=list)
    payload: JsonObject = Field(default_factory=dict)


class AggregateVersion(ServerObject):
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    version: int = Field(ge=1)
    state_hash: NonEmptyStr


class InventoryChange(ServerObject):
    sequence: int = Field(ge=1)
    event_uuid: NonEmptyStr
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    operation: NonEmptyStr
    previous_version: int = Field(ge=0)
    aggregate_version: int = Field(ge=1)
    state_hash: NonEmptyStr
    delta: JsonObject = Field(default_factory=dict)
    job_uuid: Optional[NonEmptyStr] = None
    command_uuid: Optional[NonEmptyStr] = None
    effect_key: Optional[NonEmptyStr] = None
    actor_type: NonEmptyStr
    actor_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: int = Field(ge=0)
    delivery_status: Literal["pending", "sent", "acknowledged", "dead_letter"]


ResultT = TypeVar("ResultT")


class MutationResult(ServerObject, Generic[ResultT]):
    protocol_version: Literal["materials.v1"] = PROTOCOL_VERSION
    command_uuid: NonEmptyStr
    effect_key: NonEmptyStr
    replayed: bool = False
    changed: bool = True
    ledger_sequence_start: int = Field(ge=1)
    ledger_sequence_end: int = Field(ge=1)
    affected: list[AggregateVersion] = Field(default_factory=list)
    data: ResultT

    @model_validator(mode="after")
    def _validate_ledger_range(self) -> "MutationResult[ResultT]":
        if self.ledger_sequence_end < self.ledger_sequence_start:
            raise ValueError("ledger sequence range is reversed")
        return self


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


#: ``MaterialSubstance`` 上只在显式给出时才序列化的化学品字段（见其说明）。
SUBSTANCE_CHEMICAL_FIELDS = ("chemical_uuid", "chemical", "chemical_unlinked")


class ChemicalSummary(ServerObject):
    """内容物引用的化学品定义摘要（只读，取自 ``/api/v1/chemicals``）。"""

    chemical_uuid: NonEmptyStr
    name: NonEmptyStr
    cas: Optional[str] = None
    inchi_key: Optional[str] = None
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    molecular_weight: Optional[float] = Field(default=None, gt=0)
    density_g_per_ml: Optional[float] = Field(default=None, gt=0)
    physical_state: Literal["liquid", "solid", "gas", "unknown"] = "unknown"


class MaterialSubstance(ServerObject):
    """一份内容物。

    ``chemical_uuid`` 引用化学品定义，写入时三种形态语义不同：

    - **省略**（设备快照 / 增量、旧客户端）：沿用同一物料同名（不区分大小写）内容物已有的引用
      或"已解除"状态，都没有时按名称 / 英文名 / 别名 / CAS 唯一匹配自动关联；
    - **显式 null**：不关联，本次写入不自动匹配。对已关联的内容物这是**解除**：记为已解除，
      之后省略引用的写入（设备按名称上报）也不再自动关联，直到再次显式关联；
    - **显式 uuid**：必须指向未删除的化学品（该物料上已有的引用可原样保留），并清除"已解除"。

    ``chemical`` 是读取时附带的只读摘要，写入时忽略，可带可不带；引用的化学品已删除时为
    null（uuid 保留）。``chemical_unlinked`` 是读取时附带的只读状态：为 true 表示该内容物被
    显式解除过关联、不会自动匹配；写入时忽略。读取时未关联的内容物不出现 ``chemical_uuid`` /
    ``chemical``，未解除过的不出现 ``chemical_unlinked``。

    这些字段只在显式给出时才序列化：没有它们的旧载荷与新模型的完整 dump 逐字一致（payload
    完整性比对、请求哈希、state_hash 都不变），"省略"与"显式 null"也不会哈希成同一个请求。
    """

    substance_uuid: Optional[NonEmptyStr] = None
    name: NonEmptyStr
    quantity: float = Field(ge=0)
    quantity_unit: NonEmptyStr
    physical_state: Literal["liquid", "solid", "gas", "unknown"] = "liquid"
    composition: list[JsonValue] = Field(default_factory=list)
    meta_data: JsonObject = Field(default_factory=dict)
    chemical_uuid: Optional[NonEmptyStr] = None
    chemical: Optional[ChemicalSummary] = None
    chemical_unlinked: bool = False

    @property
    def chemical_uuid_given(self) -> bool:
        """写入方是否显式给出了 ``chemical_uuid``（含显式 null）。"""

        return "chemical_uuid" in self.model_fields_set

    @model_serializer(mode="wrap")
    def _omit_unset_chemical_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        values = handler(self)
        if isinstance(values, dict):
            for field in SUBSTANCE_CHEMICAL_FIELDS:
                if field not in self.model_fields_set:
                    values.pop(field, None)
        return values


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
    # 展示名；空值由权威落库时回退 name（与 device 的 id/display_name 约定一致）。
    display_name: str = ""
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
    """待创建树中的一项；client_ref 只用于解析父子关系和返回 UUID 映射。

    material_uuid 缺省由权威分配；显式给出即「带条件的创建」（adopt 语义）：
    调用方要求以该 uuid 落库，权威中已存在同 uuid 时创建冲突失败。
    开机图物料对齐、出库扣减产物落库等「uuid 必须与外部一致」的场景使用。
    """

    client_ref: NonEmptyStr
    parent_client_ref: Optional[NonEmptyStr] = None
    ordinal: Optional[int] = Field(default=None, ge=0)
    material_uuid: Optional[NonEmptyStr] = None
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
        explicit_uuids = [
            node.material_uuid for node in self.nodes if node.material_uuid
        ]
        if len(explicit_uuids) != len(set(explicit_uuids)):
            raise ValueError("material tree explicit material_uuid values must be unique")
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


class MaterialInstantiate(ServerObject):
    """「物料出库/实例化」入口参数：按 registry 资源类名实例化并权威登记。

    与 MaterialTreeCreate 的关系：微前端没有 PLR，无法生成完整草稿树；本模型
    只描述「资源类 + 实例名」，由微后端（Host 进程内已加载 registry/PLR）实例化
    草稿并转 MaterialTreeCreate 落库（权威发 uuid）。出库产物随后以
    ResourceSlot 引用 {id, uuid} 形态提交给动作参数（unilabos_deduct_resource）。
    """

    registry_class: NonEmptyStr = Field(
        description="registry 资源类 id（resource_type_registry 的键）"
    )
    name: NonEmptyStr = Field(
        description="实例名（权威库展示名，同时作为 ResourceSlot 引用的 id）"
    )
    barcode: Optional[str] = Field(
        default=None,
        description="可选条码；提供时写入实例化产物根节点的 barcode 字段",
    )


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
    display_name: Optional[NonEmptyStr] = None
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
    """登记或补充一批按量计量的库存（散装试剂或散装耗材，只记数量/单位/有效期）。

    需要逐件追踪、放到位点的物料（枪头盒、孔板、试剂瓶等）不走 lot，而是作为
    material 实例创建。
    """

    lot_uuid: Optional[NonEmptyStr] = None
    template_uuid: NonEmptyStr
    batch_no: str = ""
    unit: NonEmptyStr
    quantity: float = Field(gt=0)
    expiry_at_ms: Optional[int] = Field(default=None, ge=0)


class InventoryStocktake(ServerObject):
    """人工盘点计数库存；预留或已实例化库存的差异必须另行复核。"""

    lot_uuid: NonEmptyStr
    expected_version: int = Field(ge=1, strict=True)
    measured_count: int = Field(ge=0, strict=True)
    reason: NonEmptyStr


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

    两种 kind 区分的是账目形态而不是物料种类：``material`` 表示独立物料实例
    （有 uuid、可放到位点的个体，例如枪头盒、孔板、试剂瓶），只改变生命周期而不
    扣数量；``lot`` 表示按量计量的 ``inventory_lot`` 库存（散装试剂、散装耗材等），
    按 lot FIFO 从 available 预留并在动作开始时扣减。
    """

    key: NonEmptyStr
    kind: Literal["material", "lot"]
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
            raise ValueError("lot requirement cannot select a material instance")
        if self.quantity is None or self.unit is None:
            raise ValueError("lot requirement needs quantity and unit")
        if self.lot_uuid is None and self.template_uuid is None:
            raise ValueError("lot requirement needs lot_uuid or template_uuid")
        if self.site_uuid is not None:
            raise ValueError("lot requirement cannot carry site_uuid")
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
    kind: Literal["material", "lot"]
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


# ── 增量同步：设备按 uuid 只上报变了的对象，权威按段合并 ───────────────────
#
# 整树快照（MaterialSnapshot）要求节点集合与权威完全一致，适合结构变化与漂移
# 检测；日常的状态变化（体积、tip、位姿）用增量：每个节点只带要改的段，未给出
# 的段沿用权威现值。乐观锁靠每个节点 / 位点自带的 expected_version。


class MaterialDataDelta(ServerObject):
    """data 段的增量：字段为 None 表示不改。``substances`` 给出即整体替换内容物列表。"""

    data: Optional[JsonObject] = None
    substances: Optional[list[MaterialSubstance]] = None
    state_status: Optional[NonEmptyStr] = None
    unknown_counter: Optional[int] = Field(default=None, ge=0)
    observed_at_ms: int = Field(default=0, ge=0)

    @property
    def empty(self) -> bool:
        return (
            self.data is None
            and self.substances is None
            and self.state_status is None
            and self.unknown_counter is None
        )


class SiteDelta(ServerObject):
    """位点的非结构字段增量。

    占用关系不在这里：谁占着哪个位点与父子关系绑定（占用物必须是位点 owner 的后代），
    由 ``move_material`` / ``transfer_material`` 原子维护。
    """

    site_uuid: NonEmptyStr
    expected_version: Optional[int] = Field(default=None, ge=1)
    visible: Optional[bool] = None
    meta_data: Optional[JsonObject] = None
    extra: Optional[JsonObject] = None

    @property
    def empty(self) -> bool:
        return self.visible is None and self.meta_data is None and self.extra is None


class MaterialNodeDelta(ServerObject):
    """一个物料节点的增量：至少带一个段。"""

    material_uuid: NonEmptyStr
    expected_version: Optional[int] = Field(default=None, ge=1)
    expected_state_hash: Optional[NonEmptyStr] = None
    data: Optional[MaterialDataDelta] = None
    position: Optional[MaterialPosition] = None
    sites: list[SiteDelta] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_section(self) -> "MaterialNodeDelta":
        if (self.data is None or self.data.empty) and self.position is None and not self.sites:
            raise ValueError("material node delta carries no section")
        site_uuids = [site.site_uuid for site in self.sites]
        if len(site_uuids) != len(set(site_uuids)):
            raise ValueError("material node delta site_uuid values must be unique")
        return self


class MaterialDelta(ServerObject):
    root_material_uuid: NonEmptyStr
    nodes: list[MaterialNodeDelta]

    @model_validator(mode="after")
    def _validate_nodes(self) -> "MaterialDelta":
        if not self.nodes:
            raise ValueError("material delta requires at least one node")
        uuids = [node.material_uuid for node in self.nodes]
        if len(uuids) != len(set(uuids)):
            raise ValueError("material delta material_uuid values must be unique")
        return self


class MaterialDeltaResult(ServerObject):
    root_material_uuid: NonEmptyStr
    applied_material_uuids: list[NonEmptyStr] = Field(default_factory=list)
    applied_site_uuids: list[NonEmptyStr] = Field(default_factory=list)
    unchanged_material_uuids: list[NonEmptyStr] = Field(default_factory=list)


__all__ = [
    "ACTOR_BACKEND",
    "ACTOR_DEVICE",
    "ACTOR_EDGE",
    "ACTOR_GRAPH",
    "ACTOR_HUMAN",
    "ACTOR_REGISTRY",
    "ACTOR_SCHEDULER",
    "ACTOR_VIRTUAL_DEVICE",
    "ACTOR_WORKFLOW",
    "KNOWN_ACTOR_TYPES",
    "AggregatePrecondition",
    "AggregateType",
    "AggregateVersion",
    "ChemicalSummary",
    "InventoryAllocation",
    "InventoryChange",
    "InventoryLotInbound",
    "InventoryLotRead",
    "InventoryMutation",
    "InventoryRequirement",
    "MutationResult",
    "PROTOCOL_VERSION",
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
    "MaterialDataDelta",
    "MaterialDelta",
    "MaterialDeltaResult",
    "MaterialNodeDelta",
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
    "SiteDelta",
    "SiteRead",
    "SiteWrite",
    "SUBSTANCE_CHEMICAL_FIELDS",
]
