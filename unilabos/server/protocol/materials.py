"""``materials.v1`` 物料传输模型。

数据库 Record 描述表行；这里的模型描述稳定的通信协议，因此 JSON 字段在
线协议中不带 ``_json`` 后缀，substance 也始终是具名对象而不是三元组。
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import Field, JsonValue, model_validator

from unilabos.server.models.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.models.materials import ResourceTemplateHandle


class ResourceTemplateWrite(ServerObject):
    template_uuid: NonEmptyStr
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
    template_uuid: NonEmptyStr
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
    material_uuid: NonEmptyStr
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


class SiteRead(SiteWrite):
    site_uuid: NonEmptyStr
    owner_material_uuid: NonEmptyStr
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
    identity: MaterialIdentityWrite
    position: MaterialPosition = Field(default_factory=MaterialPosition)
    data: MaterialDataWrite = Field(default_factory=MaterialDataWrite)
    sites: list[SiteWrite] = Field(default_factory=list)


class MaterialTreeCreate(ServerObject):
    nodes: list[MaterialNodeCreate]
    known_random_uuid: bool = False

    @model_validator(mode="after")
    def _validate_tree(self) -> "MaterialTreeCreate":
        if not self.nodes:
            raise ValueError("material tree requires at least one node")
        refs = [node.client_ref for node in self.nodes]
        if len(refs) != len(set(refs)):
            raise ValueError("material tree client_ref values must be unique")
        known: set[str] = set()
        roots = 0
        for node in self.nodes:
            if node.parent_client_ref is None:
                roots += 1
            elif node.parent_client_ref not in known:
                raise ValueError("material tree must be parent-first")
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
    client_uuid_map: dict[str, str] = Field(default_factory=dict)
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
    "MaterialAggregateRead",
    "MaterialDataRead",
    "MaterialDataWrite",
    "MaterialDelete",
    "MaterialDeleteResult",
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
    "ResourceTemplateRead",
    "ResourceTemplateWrite",
    "SiteRead",
    "SiteWrite",
]
