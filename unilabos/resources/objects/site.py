"""Uni-Lab-OS 唯一的 canonical Site 模型与适配函数。

``SiteDefinition`` 是 Registry 中不含实例身份的静态槽位规格；
``ResourceSite`` 是微后端实例化后带 UUID、owner 和占用关系的 Site 快照。
两者是同一个 Site 协议的两个生命周期阶段，统一放在本模块，避免出现
``site_definition`` 与 ``objects.site`` 两套入口。
"""

from __future__ import annotations

import copy
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    TypeAlias,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import NotRequired, TypedDict

from unilabos.resources.objects.base import ResourceObject
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionType,
    _copy_mapping_payload,
    normalize_site_pose_payload,
)


#: 库位准入白名单的字段名（V1 同名）。定义里是顶层字段；实例 Site 没有这个顶层字段，放在
#: ``extra`` 里随物料权威持久化，由物料移动统一校验（不在白名单的模板报 6008）。
SITE_TEMPLATE_WHITELIST_KEY = "allowed_resource_template_uuids"


class SiteDefinition(BaseModel):
    """Registry 中不含实例身份的 Site 静态定义。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    schema_version: Literal[1] = 1
    index: Union[int, str]
    label: str
    visible: bool = True
    pose: ResourceDictPosition = Field(default_factory=ResourceDictPosition)
    allowed_resource_categories: List[str] = Field(
        default_factory=list,
        description=(
            "ResourceTemplate category hints for frontend canvas filtering only; "
            "Edge and backend do not enforce compatibility"
        ),
    )
    allowed_resource_template_uuids: List[str] = Field(
        default_factory=list,
        description=(
            "Resource templates this Site accepts: template UUIDs or template names "
            "(registry ids; the portable form, since template UUIDs are allocated per "
            "materials database). Empty accepts any template. Enforced on every placement "
            "(V1 site_template_not_allowed, 6008)"
        ),
    )
    parent_link: str = ""
    description: str = ""
    meta_data: Dict[str, JsonValue] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _omit_empty_whitelist(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
        # 不限制模板时不输出该字段：注册表条目、缓存与实例快照的形状与引入白名单前一致
        data = handler(self)
        if isinstance(data, dict) and not data.get(SITE_TEMPLATE_WHITELIST_KEY):
            data.pop(SITE_TEMPLATE_WHITELIST_KEY, None)
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_nested_geometry(cls, value: Any):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("Site 初始化定义必须是对象")
        return normalize_site_pose_payload(value)

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Site.label 不能为空")
        return value.strip()

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: Union[int, str]) -> Union[int, str]:
        if isinstance(value, bool):
            raise ValueError("Site.index 不能是布尔值")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Site.index 不能为空")
        return value

    @field_validator("allowed_resource_categories", "allowed_resource_template_uuids")
    @classmethod
    def _normalize_string_list(
        cls, values: List[str], info: ValidationInfo
    ) -> List[str]:
        result: List[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Site.{info.field_name} 只能包含非空字符串")
            normalized = value.strip()
            key = normalized.casefold()
            if key not in seen:
                result.append(normalized)
                seen.add(key)
        return result


SiteDefinitionInput: TypeAlias = Union[SiteDefinition, Mapping[str, Any]]
SITE_DEFINITION_FIELDS: tuple[str, ...] = tuple(SiteDefinition.model_fields)


def site_template_whitelist(extra: Optional[Mapping[str, Any]]) -> List[str]:
    """实例 Site ``extra`` 里的准入白名单（模板 UUID 或模板名）；空列表表示不限制。"""

    values = (extra or {}).get(SITE_TEMPLATE_WHITELIST_KEY) if isinstance(extra, Mapping) else None
    if not isinstance(values, (list, tuple)):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def site_definition_extra(definition: Mapping[str, Any]) -> Dict[str, Any]:
    """定义实例化为 Site 时写进实例 ``extra`` 的字段（目前只有准入白名单）。"""

    allowed = list(definition.get(SITE_TEMPLATE_WHITELIST_KEY) or [])
    return {SITE_TEMPLATE_WHITELIST_KEY: allowed} if allowed else {}


def site_instance_definition(site: Mapping[str, Any]) -> Dict[str, Any]:
    """从实例 Site 取回它的定义（白名单从 ``extra`` 读回），用于与注册表定义比对。"""

    payload = {key: site[key] for key in SITE_DEFINITION_FIELDS if key in site}
    allowed = site_template_whitelist(site.get("extra"))
    if allowed:
        payload[SITE_TEMPLATE_WHITELIST_KEY] = allowed
    return SiteDefinition.model_validate(payload).model_dump()


class ResourceSiteType(TypedDict):
    """物料根字段 ``sites`` 中的规范 Site 结构。"""

    schema_version: Literal[1]
    uuid: str
    template_name: str
    material_uuid: str
    index: Union[int, str]
    label: str
    visible: NotRequired[bool]
    occupied_material_uuid: NotRequired[Optional[str]]
    pose: NotRequired[ResourceDictPositionType]
    allowed_resource_categories: NotRequired[List[str]]
    parent_link: NotRequired[str]
    description: NotRequired[str]
    meta_data: NotRequired[Dict[str, Any]]
    extra: NotRequired[Dict[str, Any]]


class ResourceSite(ResourceObject):
    """Edge、微后端和 PLR Adapter 共用的唯一 Site 实例模型。

    PLR 特有但需要往返保留的字段进入显式 ``extra``；canonical v1 仍拒绝
    未声明的顶层字段，避免 Adapter 形状反向污染微后端协议。
    """

    schema_version: Literal[1] = 1
    uuid: str
    template_name: str
    material_uuid: str
    index: Union[int, str]
    label: str
    visible: bool = True
    occupied_material_uuid: Optional[str] = None
    pose: ResourceDictPosition = Field(default_factory=ResourceDictPosition)
    allowed_resource_categories: List[str] = Field(
        default_factory=list,
        description=(
            "ResourceTemplate category hints for frontend canvas filtering only; "
            "not an Edge/backend mount constraint"
        ),
    )
    parent_link: str = ""
    description: str = ""
    meta_data: Dict[str, JsonValue] = Field(default_factory=dict)
    extra: Dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_shape(cls, value: Any, info: ValidationInfo):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("Site 必须是对象")

        site = normalize_site_pose_payload(value)
        is_legacy = "schema_version" not in site

        if "occupied_by" in site:
            raise ValueError(
                "Site.occupied_by 不受支持；请直接提供 occupied_material_uuid"
            )

        # 旧协议的未知顶层字段继续显式归档；规范 v1 输入只允许声明过的字段。
        known = set(cls.model_fields)
        if is_legacy:
            legacy_fields = {
                key: site.pop(key) for key in list(site) if key not in known
            }
            if legacy_fields:
                metadata = site.get("meta_data")
                if metadata is None:
                    metadata = {}
                if not isinstance(metadata, dict):
                    raise ValueError("Site.meta_data 必须是对象")
                metadata = copy.deepcopy(metadata)
                existing = metadata.get("legacy_fields")
                if existing is not None and not isinstance(existing, dict):
                    raise ValueError("Site.meta_data.legacy_fields 必须是对象")
                metadata["legacy_fields"] = {**(existing or {}), **legacy_fields}
                site["meta_data"] = metadata

        return site

    @field_validator("uuid", "template_name", "material_uuid", "label")
    @classmethod
    def _require_non_empty_string(cls, value: str, info: ValidationInfo) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Site.{info.field_name} 不能为空")
        return value.strip()

    @field_validator("occupied_material_uuid")
    @classmethod
    def _normalize_occupied_uuid(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Site.occupied_material_uuid 必须是非空字符串或 null")
        return value.strip()

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: Union[int, str]) -> Union[int, str]:
        if isinstance(value, bool):
            raise ValueError("Site.index 不能是布尔值")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Site.index 不能为空")
        return value

    @field_validator("allowed_resource_categories")
    @classmethod
    def _normalize_string_list(
        cls, values: List[str], info: ValidationInfo
    ) -> List[str]:
        result: List[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Site.{info.field_name} 只能包含非空字符串")
            normalized = value.strip()
            key = normalized.casefold()
            if key not in seen:
                result.append(normalized)
                seen.add(key)
        return result

    @model_validator(mode="after")
    def _validate_references(self) -> "ResourceSite":
        if self.occupied_material_uuid == self.material_uuid:
            raise ValueError("Site 不能承载拥有该 Site 的物料本身")
        return self


def normalize_available_sites(
    value: Optional[Sequence[SiteDefinitionInput]],
) -> List[Dict[str, Any]]:
    """把装饰器/YAML 中的 ``available_sites`` 规范化为共用 pose 模型。"""

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("available_sites 必须是数组")

    result: List[Dict[str, Any]] = []
    seen_indexes: set[tuple[str, Union[int, str]]] = set()
    seen_labels: set[str] = set()
    for ordinal, raw_site in enumerate(value):
        if isinstance(raw_site, SiteDefinition):
            site = raw_site
        else:
            if not isinstance(raw_site, Mapping):
                raise ValueError(f"available_sites[{ordinal}] 必须是对象")
            payload = _copy_mapping_payload(raw_site)
            payload.setdefault("index", ordinal)
            payload.setdefault("label", str(payload["index"]))
            site = SiteDefinition.model_validate(payload)

        index_key = (type(site.index).__name__, site.index)
        if index_key in seen_indexes:
            raise ValueError(f"available_sites 中存在重复 index: {site.index}")
        label_key = site.label.casefold()
        if label_key in seen_labels:
            raise ValueError(f"available_sites 中存在重复 label: {site.label}")
        seen_indexes.add(index_key)
        seen_labels.add(label_key)
        result.append(site.model_dump())
    return result


def _site_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    raise ValueError("实例 Site 必须是对象")


def _definition_from_instance(value: Any) -> Dict[str, Any]:
    return site_instance_definition(_site_payload(value))


def validate_instantiated_sites(
    definitions: Optional[Sequence[SiteDefinitionInput]],
    *,
    owner_uuid: str,
    template_name: str,
    current_sites: Optional[List[Any]] = None,
    sites_initialized: bool,
) -> List[Dict[str, Any]]:
    """校验微后端实例 Site 快照与 Registry 模板定义一致。

    Edge 不生成 Site UUID，也不把 Registry ``available_sites`` 复制进实例。
    ``sites_initialized=True`` 且 ``current_sites=[]`` 是权威空快照，必须保留；
    只有微后端 create/import/migration 边界可以把模板定义实例化为 Site。
    """

    normalized = normalize_available_sites(definitions)
    if not owner_uuid:
        raise ValueError("校验实例 Site 时 owner_uuid 不能为空")
    if not template_name:
        raise ValueError("校验实例 Site 时 template_name 不能为空")

    if not sites_initialized:
        if current_sites:
            raise ValueError("sites_initialized=false 时不能携带实例 Site")
        raise ValueError(
            f"资源 {owner_uuid} 的 Site 尚未由微后端实例化，Edge 不得本地补齐"
        )

    if current_sites is None:
        raise ValueError("sites_initialized=true 时 sites 必须是数组")

    existing_payloads = [_site_payload(site) for site in current_sites]
    if not existing_payloads:
        return []
    if not normalized:
        raise ValueError(
            f"资源 {owner_uuid} 存在实例 Site，但 Registry 没有 available_sites 定义"
        )

    existing_by_label = {
        str(site.get("label", "")).casefold(): site
        for site in existing_payloads
        if site.get("label")
    }
    existing_by_index = {
        (type(site.get("index")).__name__, site.get("index")): site
        for site in existing_payloads
        if site.get("index") is not None
    }
    used_site_ids: set[int] = set()
    result: List[Dict[str, Any]] = []

    for definition in normalized:
        existing = existing_by_label.get(str(definition["label"]).casefold())
        if existing is None:
            existing = existing_by_index.get(
                (type(definition["index"]).__name__, definition["index"])
            )
        if existing is None:
            raise ValueError(
                f"资源 {owner_uuid} 的实例快照缺少 available_sites 定义 "
                f"{definition['label']}"
            )

        used_site_ids.add(id(existing))
        existing_definition = _definition_from_instance(existing)
        if existing_definition != definition:
            raise ValueError(
                f"设备 {owner_uuid} 的 Site {definition['label']} "
                "固定定义与 available_sites 冲突"
            )
        existing_owner = existing.get("material_uuid")
        if existing_owner != owner_uuid:
            raise ValueError(
                f"Site {definition['label']} 的 material_uuid={existing_owner!r} "
                f"与 owner={owner_uuid!r} 冲突"
            )
        existing_template = existing.get("template_name")
        if existing_template != template_name:
            raise ValueError(
                f"Site {definition['label']} 的 template_name={existing_template!r} "
                f"与设备模板 {template_name!r} 冲突"
            )
        if not existing.get("uuid"):
            raise ValueError(f"Site {definition['label']} 缺少微后端分配的 UUID")
        result.append(copy.deepcopy(existing))

    unused = [site for site in existing_payloads if id(site) not in used_site_ids]
    if unused:
        labels = [site.get("label", site.get("index")) for site in unused]
        raise ValueError(
            f"资源 {owner_uuid} 存在 available_sites 未声明的实例 Site: {labels}"
        )
    return result


__all__ = [
    "SITE_DEFINITION_FIELDS",
    "SITE_TEMPLATE_WHITELIST_KEY",
    "ResourceSite",
    "ResourceSiteType",
    "SiteDefinition",
    "SiteDefinitionInput",
    "normalize_available_sites",
    "normalize_site_pose_payload",
    "site_definition_extra",
    "site_instance_definition",
    "site_template_whitelist",
    "validate_instantiated_sites",
]
