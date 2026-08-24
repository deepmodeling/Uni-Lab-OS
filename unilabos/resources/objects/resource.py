"""资源快照的 canonical 领域模型。"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import TypedDict

from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionType,
)
from unilabos.resources.objects.joint_state import (
    ResourceJointState,
    ResourceJointStateType,
)
from unilabos.resources.objects.site import ResourceSite, ResourceSiteType
from unilabos.resources.objects.state import (
    LiquidHistoryEntry,
    LiquidStateEntry,
    SubstanceStateEntry,
    TRACKER_STATE_KEYS,
)
from unilabos.utils.log import logger


EXTRA_RESOURCE_CLASS = "unilabos_resource_class"
EXTRA_CLASS = EXTRA_RESOURCE_CLASS
FRONTEND_POSE_EXTRA = "unilabos_frontend_pose_extra"
EXTRA_RESOURCE_POSE = "unilabos_resource_pose"
EXTRA_RESOURCE_JOINT_STATE = "unilabos_resource_joint_state"
EXTRA_RESOURCE_META_DATA = "unilabos_resource_meta_data"
EXTRA_SAMPLE_UUID = "sample_uuid"
EXTRA_UNILABOS_SAMPLE_UUID = "unilabos_sample_uuid"
EXTRA_SITES = "sites"


class ResourceDictType(TypedDict):
    id: str
    uuid: str
    name: str
    description: str
    resource_schema: Dict[str, Any]
    model: Dict[str, Any]
    icon: str
    parent_uuid: Optional[str]
    parent: Optional["ResourceDictType"]
    type: Union[Literal["device"], str]
    klass: str
    pose: ResourceDictPositionType
    config: Dict[str, Any]
    data: Dict[str, Any]
    extra: Dict[str, Any]
    meta_data: Dict[str, JsonValue]
    machine_name: str
    barcode: str
    barcode_symbology: str
    template_name: str
    resource_template_uuid: str
    joint_state: Optional[ResourceJointStateType]
    sites: Optional[List[ResourceSiteType]]
    sites_initialized: bool
    substances: Optional[List[SubstanceStateEntry]]
    liquid_history: Optional[List[LiquidHistoryEntry]]
    unknown_counter: Optional[int]


class ResourceDict(BaseModel):
    """资源实例的唯一规范快照；运行时树行为留在 ``resource_tracker``。"""

    id: str = Field(description="Resource ID")
    uuid: str = Field(description="Resource UUID")
    name: str = Field(description="Resource name")
    description: str = Field(description="Resource description", default="")
    resource_schema: Dict[str, Any] = Field(
        description="Resource schema",
        default_factory=dict,
        serialization_alias="schema",
        validation_alias="schema",
    )
    model: Dict[str, Any] = Field(description="Resource model", default_factory=dict)
    icon: str = Field(description="Resource icon", default="")
    parent_uuid: Optional[str] = Field(description="Parent resource uuid", default=None)
    parent: Optional["ResourceDict"] = Field(
        description="Parent resource object", default=None, exclude=True
    )
    type: Union[Literal["device"], str] = Field(description="Resource type")
    klass: str = Field(alias="class", description="Resource class name")
    pose: ResourceDictPosition = Field(
        description="Resource pose relative to its parent, plus geometry/layout metadata",
        default_factory=ResourceDictPosition,
    )
    config: Dict[str, Any] = Field(description="Resource configuration")
    data: Dict[str, Any] = Field(description="Resource data, eg: container data without liquids, since liquids are tracked separately")
    extra: Dict[str, Any] = Field(
        description="UniLab communication and conversion metadata"
    )
    meta_data: Dict[str, JsonValue] = Field(
        description="Canonical resource metadata", default_factory=dict
    )
    machine_name: str = Field(
        description="Machine this resource belongs to", default=""
    )
    barcode: str = Field(description="Material barcode", default="")
    barcode_symbology: str = Field(description="Barcode symbology / 码制", default="")
    template_name: str = Field(description="Edge resource template name", default="")
    resource_template_uuid: str = Field(
        description="Micro-backend ResourceTemplate UUID", default=""
    )
    joint_state: Optional[ResourceJointState] = Field(
        description="Frequently changing device joint state; independent from pose",
        default=None,
    )
    sites: Optional[List[ResourceSite]] = Field(
        description="Carrier site definitions / 载架位点", default=None
    )
    sites_initialized: bool = Field(  # todo: 有可能删除
        description="Whether site preparation has run for this resource instance",
        default=False,
    )
    substances: Optional[List[SubstanceStateEntry]] = Field(
        description="Current (substance_name, quantity, unit) entries", default=None
    )
    liquid_history: Optional[List[LiquidHistoryEntry]] = Field(
        description="VolumeTracker (liquid_name, delta, unit) event history",
        default=None,
    )
    unknown_counter: Optional[int] = Field(
        description="Unnamed liquid counter", default=None, ge=0
    )

    @field_validator("substances", "liquid_history", mode="before")
    @classmethod
    def _normalize_tracker_entries(cls, value: Any, info: ValidationInfo):
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} 必须是数组")

        normalized = []
        for ordinal, item in enumerate(value):
            if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                raise ValueError(
                    f"{info.field_name}[{ordinal}] 必须是 (liquid_name, amount, unit) 三元组"
                )
            name, amount = item[0], item[1]
            unit = item[2] if len(item) == 3 else "ul"
            if name is None and info.field_name == "substances":
                raise ValueError(f"substances[{ordinal}].name 不能为空")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError(
                    f"{info.field_name}[{ordinal}].name 必须是非空字符串或 null"
                )
            if not isinstance(unit, str) or not unit.strip():
                raise ValueError(f"{info.field_name}[{ordinal}].unit 必须是非空字符串")
            normalized.append(
                (name.strip() if isinstance(name, str) else None, amount, unit.strip())
            )
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _promote_root_fields(cls, value: Any):
        if not isinstance(value, dict):
            return value
        content = copy.deepcopy(value)
        data = content.get("data")
        extra = content.get("extra")
        if not isinstance(data, dict):
            raise ValueError("根字段 data 必须是对象")
        if not isinstance(extra, dict):
            raise ValueError("根字段 extra 必须是对象")

        transport_uuid = data.pop("unilabos_uuid", None)
        root_uuid = content.get("uuid")
        normalized_root_uuid = str(root_uuid).strip() if root_uuid is not None else ""
        normalized_transport_uuid = (
            str(transport_uuid).strip() if transport_uuid is not None else ""
        )
        if (
            normalized_root_uuid
            and normalized_transport_uuid
            and normalized_root_uuid != normalized_transport_uuid
        ):
            raise ValueError("根字段 uuid 与 data.unilabos_uuid 冲突")
        resolved_uuid = normalized_root_uuid or normalized_transport_uuid
        if not resolved_uuid:
            raise ValueError(
                "资源缺少 UUID；请先通过微后端 runtime create 或 strict import"
            )
        content["uuid"] = resolved_uuid

        # substances 是全部内容物的唯一规范字段。旧 liquids 仅作为输入兼容，
        # 新 PLR 同时输出 substances/liquids 时以后者的全集 substances 为准。
        root_substances = content.get("substances")
        root_liquids = content.pop("liquids", None)
        data_substances = data.pop("substances", None)
        data_liquids = data.pop("liquids", None)
        if root_substances is None:
            root_substances = (
                data_substances
                if data_substances is not None
                else (root_liquids if root_liquids is not None else data_liquids)
            )
        elif data_substances is not None:
            def normalize_substances(entries: Any) -> Any:
                if not isinstance(entries, (list, tuple)):
                    return entries
                return [
                    (
                        item[0],
                        item[1],
                        item[2] if len(item) >= 3 else "ul",
                    )
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                    else item
                    for item in entries
                ]

            normalized_root = normalize_substances(root_substances)
            normalized_data = normalize_substances(data_substances)
            if normalized_root != normalized_data:
                raise ValueError("根字段 substances 与 data.substances 冲突")
        content["substances"] = root_substances

        extra_template_name = extra.pop(EXTRA_RESOURCE_CLASS, None)
        extra_joint_state = extra.pop(EXTRA_RESOURCE_JOINT_STATE, None)
        root_joint_state = content.get("joint_state")
        if root_joint_state is not None and extra_joint_state is not None:
            normalized_root_joint_state = ResourceJointState.model_validate(
                root_joint_state
            ).model_dump()
            normalized_extra_joint_state = ResourceJointState.model_validate(
                extra_joint_state
            ).model_dump()
            if normalized_root_joint_state != normalized_extra_joint_state:
                raise ValueError(
                    f"根字段 joint_state 与 extra.{EXTRA_RESOURCE_JOINT_STATE} 冲突"
                )
        if root_joint_state is None and extra_joint_state is not None:
            content["joint_state"] = extra_joint_state
        missing = object()
        extra_meta_data = extra.pop(EXTRA_RESOURCE_META_DATA, missing)
        data_meta_data = data.pop("meta_data", missing)
        root_meta_data = content.get("meta_data", missing)

        def normalize_meta_data(
            raw_meta_data: Any, source: str
        ) -> Optional[Dict[str, Any]]:
            if raw_meta_data is missing:
                return None
            if isinstance(raw_meta_data, BaseModel):
                raw_meta_data = raw_meta_data.model_dump()
            if not isinstance(raw_meta_data, Mapping):
                raise ValueError(f"{source} 必须是对象")
            return copy.deepcopy(dict(raw_meta_data))

        resolved_meta_data: Optional[Dict[str, Any]] = None
        resolved_meta_data_source = ""
        for source, raw_meta_data in (
            ("根字段 meta_data", root_meta_data),
            ("data.meta_data", data_meta_data),
            (f"extra.{EXTRA_RESOURCE_META_DATA}", extra_meta_data),
        ):
            normalized_meta_data = normalize_meta_data(raw_meta_data, source)
            if normalized_meta_data is None:
                continue
            if (
                resolved_meta_data is not None
                and normalized_meta_data != resolved_meta_data
            ):
                raise ValueError(f"{resolved_meta_data_source} 与 {source} 冲突")
            resolved_meta_data = normalized_meta_data
            resolved_meta_data_source = source
        for state_key in TRACKER_STATE_KEYS:
            state_value = data.pop(state_key, None)
            if content.get(state_key) is None and state_value is not None:
                content[state_key] = state_value
        content["data"] = data
        content["extra"] = extra

        config = content.get("config")
        if not isinstance(config, dict):
            config = {}
        else:
            config = copy.deepcopy(config)
        config_meta_data = config.pop("meta_data", missing)
        normalized_config_meta_data = normalize_meta_data(
            config_meta_data, "config.meta_data"
        )
        if normalized_config_meta_data is not None:
            if (
                resolved_meta_data is not None
                and normalized_config_meta_data != resolved_meta_data
            ):
                raise ValueError(
                    f"{resolved_meta_data_source} 与 config.meta_data 冲突"
                )
            resolved_meta_data = normalized_config_meta_data
        content["meta_data"] = resolved_meta_data or {}
        content["config"] = config

        config_barcode = config.pop("barcode", None)
        config_barcode_data = ""
        config_barcode_symbology = ""
        if isinstance(config_barcode, dict):
            config_barcode_data = config_barcode.get("data") or ""
            config_barcode_symbology = config_barcode.get("symbology") or ""
        elif config_barcode:
            config_barcode_data = config_barcode
        if not isinstance(config_barcode_data, str):
            raise ValueError("config.barcode.data 必须是字符串")
        if not isinstance(config_barcode_symbology, str):
            raise ValueError("config.barcode.symbology 必须是字符串")
        root_barcode = content.get("barcode") or ""
        root_barcode_symbology = content.get("barcode_symbology") or ""
        if root_barcode and config_barcode_data and root_barcode != config_barcode_data:
            raise ValueError("根字段 barcode 与 config.barcode.data 冲突")
        if (
            root_barcode_symbology
            and config_barcode_symbology
            and root_barcode_symbology != config_barcode_symbology
        ):
            raise ValueError(
                "根字段 barcode_symbology 与 config.barcode.symbology 冲突"
            )
        content["barcode"] = root_barcode or config_barcode_data
        content["barcode_symbology"] = (
            root_barcode_symbology or config_barcode_symbology
        )

        config_template_name = config.pop("template_name", None)
        config_model = config.get("model")
        template_name = content.get("template_name")
        for source_name, source_value in (
            (f"extra.{EXTRA_RESOURCE_CLASS}", extra_template_name),
            ("config.template_name", config_template_name),
        ):
            if (
                template_name
                and source_value
                and str(template_name) != str(source_value)
            ):
                raise ValueError(f"根字段 template_name 与 {source_name} 冲突")
            if not template_name and source_value:
                template_name = source_value
        if not template_name:
            template_name = (
                (config_model if isinstance(config_model, str) else None)
                or config.get("type")
                or content.get("type")
                or ""
            )
        content["template_name"] = str(template_name)

        config_template_uuid = config.pop("resource_template_uuid", None)
        root_template_uuid = content.get("resource_template_uuid")
        if (
            root_template_uuid
            and config_template_uuid
            and root_template_uuid != config_template_uuid
        ):
            raise ValueError(
                "根字段 resource_template_uuid 与 config.resource_template_uuid 冲突"
            )
        content["resource_template_uuid"] = str(
            root_template_uuid or config_template_uuid or ""
        )

        config_pose = config.pop("pose", None)
        root_pose = content.get("pose")

        def normalize_resource_pose(
            raw_pose: Any, source: str
        ) -> Optional[Dict[str, Any]]:
            if raw_pose is None:
                return None
            if isinstance(raw_pose, ResourceDictPosition):
                return raw_pose.model_dump()
            if not isinstance(raw_pose, dict):
                raise ValueError(f"{source} 必须是对象")
            return ResourceDictPosition.model_validate(raw_pose).model_dump()

        normalized_root_pose = normalize_resource_pose(root_pose, "根字段 pose")
        normalized_config_pose = normalize_resource_pose(config_pose, "config.pose")
        config_pose_has_position = (
            isinstance(config_pose, dict) and "position" in config_pose
        ) or isinstance(config_pose, ResourceDictPosition)
        if (
            normalized_root_pose is not None
            and normalized_config_pose is not None
        ):
            root_without_position = {
                key: value
                for key, value in normalized_root_pose.items()
                if key != "position"
            }
            config_without_position = {
                key: value
                for key, value in normalized_config_pose.items()
                if key != "position"
            }
            if root_without_position != config_without_position:
                raise ValueError("根字段 pose 与 config.pose 冲突")
            if (
                config_pose_has_position
                and normalized_root_pose["position"]
                != normalized_config_pose["position"]
            ):
                raise ValueError("根字段 pose.position 与 config.pose.position 冲突")
        normalized_pose = normalized_root_pose or normalized_config_pose

        content.pop("available_sites", None)
        config.pop("available_sites", None)

        config_sites = config.pop("sites", None)
        if (
            content.get("sites") is not None
            and config_sites is not None
            and content["sites"] != config_sites
        ):
            raise ValueError("根字段 sites 与 config.sites 冲突")
        if content.get("sites") is None and config_sites is not None:
            content["sites"] = config_sites

        config_sites_initialized = config.pop("sites_initialized", None)
        if "sites_initialized" in content and config_sites_initialized is not None:
            if content["sites_initialized"] != config_sites_initialized:
                raise ValueError(
                    "根字段 sites_initialized 与 config.sites_initialized 冲突"
                )
        elif config_sites_initialized is not None:
            content["sites_initialized"] = config_sites_initialized

        if content.get("sites"):
            content["sites_initialized"] = True
        elif content.get("sites_initialized") and content.get("sites") is None:
            content["sites"] = []

        if "position" in content:
            raise ValueError("根字段 position 已删除，请使用 pose.position")
        content["pose"] = normalized_pose or {}

        owner_uuid = content.get("uuid")
        if owner_uuid and content.get("sites") is not None:
            normalized_sites = []
            for ordinal, raw_site in enumerate(content["sites"]):
                if isinstance(raw_site, ResourceSite):
                    raw_site = raw_site.model_dump()
                if not isinstance(raw_site, dict):
                    raise ValueError(f"sites[{ordinal}] 必须是对象")
                normalized_sites.append(copy.deepcopy(raw_site))
            content["sites"] = normalized_sites
        return content

    @model_validator(mode="after")
    def _validate_site_ownership(self) -> "ResourceDict":
        if self.sites:
            seen_uuids: set[str] = set()
            seen_indexes: set[tuple[str, Union[int, str]]] = set()
            seen_labels: set[str] = set()
            seen_occupants: set[str] = set()
            for site in self.sites:
                if site.material_uuid != self.uuid:
                    raise ValueError(
                        f"Site {site.uuid} 的 material_uuid={site.material_uuid} "
                        f"与所属物料 {self.uuid} 不一致"
                    )
                if site.template_name != self.template_name:
                    raise ValueError(
                        f"Site {site.uuid} 的 template_name={site.template_name} "
                        f"与所属物料模板 {self.template_name} 不一致"
                    )
                if site.uuid in seen_uuids:
                    raise ValueError(
                        f"物料 {self.uuid} 下存在重复 Site UUID: {site.uuid}"
                    )
                index_key = (type(site.index).__name__, site.index)
                if index_key in seen_indexes:
                    raise ValueError(
                        f"物料 {self.uuid} 下存在重复 Site index: {site.index}"
                    )
                label_key = site.label.casefold()
                if label_key in seen_labels:
                    raise ValueError(
                        f"物料 {self.uuid} 下存在重复 Site label: {site.label}"
                    )
                if site.occupied_material_uuid:
                    if site.occupied_material_uuid in seen_occupants:
                        raise ValueError(
                            f"物料 {site.occupied_material_uuid} 同时占用了 {self.uuid} 下多个 Site"
                        )
                    seen_occupants.add(site.occupied_material_uuid)
                seen_uuids.add(site.uuid)
                seen_indexes.add(index_key)
                seen_labels.add(label_key)
        return self

    @field_serializer("parent_uuid")
    def _serialize_parent(self, parent_uuid: Optional["ResourceDict"]):
        return self.uuid_parent

    @field_validator("parent", mode="before")
    @classmethod
    def _deserialize_parent(cls, parent: Optional["ResourceDict"]):
        return parent if isinstance(parent, ResourceDict) else None

    @property
    def uuid_parent(self) -> Optional[str]:
        parent_instance_uuid = self.parent_instance_uuid
        if (
            parent_instance_uuid is not None
            and self.parent_uuid
            and parent_instance_uuid != self.parent_uuid
        ):
            logger.warning(f"{self.name}[{self.uuid}]的parent uuid未同步！")
        return (
            parent_instance_uuid
            if parent_instance_uuid is not None
            else self.parent_uuid
        )

    @property
    def parent_instance_uuid(self) -> Optional[str]:
        return self.parent.uuid if self.parent is not None else None

    @property
    def parent_instance_name(self) -> Optional[str]:
        return self.parent.name if self.parent is not None else None

    @property
    def is_root_node(self) -> bool:
        return self.parent is None

    @property
    def liquids(self) -> Optional[List[LiquidStateEntry]]:
        """旧调用兼容视图：只返回非质量单位的液体，不再保存第二份状态。"""

        if self.substances is None:
            return None
        mass_units = {"ng", "ug", "mg", "g", "kg"}
        return [
            item for item in self.substances if item[2].strip().lower() not in mass_units
        ]

    @liquids.setter
    def liquids(self, value: Optional[List[LiquidStateEntry]]) -> None:
        self.substances = value


RESOURCE_ROOT_FIELDS: tuple[str, ...] = tuple(
    field.serialization_alias or field.alias or field_name
    for field_name, field in ResourceDict.model_fields.items()
)

PLR_CONFIG_ROOT_KEYS = (
    "barcode",
    "barcode_symbology",
    "template_name",
    "resource_template_uuid",
    "sites",
    "sites_initialized",
)


def assemble_tracker_state(resource: ResourceDict) -> Dict[str, Any]:
    state = copy.deepcopy(resource.data)
    for state_key in TRACKER_STATE_KEYS:
        root_value = getattr(resource, state_key)
        if root_value is not None:
            state[state_key] = copy.deepcopy(root_value)
    return state


__all__ = [
    "EXTRA_CLASS",
    "EXTRA_RESOURCE_CLASS",
    "EXTRA_RESOURCE_META_DATA",
    "EXTRA_RESOURCE_JOINT_STATE",
    "EXTRA_RESOURCE_POSE",
    "EXTRA_SAMPLE_UUID",
    "EXTRA_SITES",
    "EXTRA_UNILABOS_SAMPLE_UUID",
    "FRONTEND_POSE_EXTRA",
    "PLR_CONFIG_ROOT_KEYS",
    "RESOURCE_ROOT_FIELDS",
    "ResourceDict",
    "ResourceDictType",
    "assemble_tracker_state",
]
