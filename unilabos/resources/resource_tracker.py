import copy
import inspect
import uuid
from pydantic import BaseModel, Field, ValidationError
from typing import List, Tuple, Any, Dict, Mapping, Optional, cast, TYPE_CHECKING, Union

from unilabos.resources.objects.resource import (
    EXTRA_CLASS,
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_JOINT_STATE,
    EXTRA_RESOURCE_META_DATA,
    EXTRA_RESOURCE_POSE,
    EXTRA_SAMPLE_UUID,
    EXTRA_SITES,
    EXTRA_UNILABOS_SAMPLE_UUID,
    FRONTEND_POSE_EXTRA,
    PLR_CONFIG_ROOT_KEYS,
    RESOURCE_ROOT_FIELDS,
    ResourceDict,
    ResourceDictType,
    assemble_tracker_state,
)
from unilabos.resources.objects.site import ResourceSite, ResourceSiteType
from unilabos.resources.objects.sample import LabSample, SampleUUIDsType
from unilabos.resources.objects.state import TRACKER_STATE_KEYS
from unilabos.resources.plr_additional_res_reg import register
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionObjectType,
    ResourceDictPositionScale,
    ResourceDictPositionScaleType,
    ResourceDictPositionSize,
    ResourceDictPositionSizeType,
    ResourceDictPositionType,
)
from unilabos.utils.log import logger

if TYPE_CHECKING:
    from unilabos.devices.workstation.workstation_base import WorkstationBase
    from pylabrobot.resources import Resource as PLRResource


# 函数参数名常量 - 用于自动注入 sample_uuids 列表
PARAM_SAMPLE_UUIDS = "sample_uuids"

# JSON Command 中的系统参数字段名
JSON_UNILABOS_PARAM = "unilabos_param"

# 返回值中的 samples 字段名
RETURN_UNILABOS_SAMPLES = "unilabos_samples"


def plr_class_accepts_serialized_sites(plr_cls: type) -> bool:
    """判断 PLR 类构造器是否直接消费项目的 ``sites[]`` 列表。"""

    from pylabrobot.resources.carrier import Carrier

    return (
        not issubclass(plr_cls, Carrier)
        and "sites" in inspect.signature(plr_cls).parameters
    )


def sites_for_plr_deserialization(
    sites: List[Union[ResourceSite, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """生成唯一 canonical ``ResourceSite`` 构造输入。"""

    return [
        (
            site.model_dump()
            if isinstance(site, ResourceSite)
            else ResourceSite.model_validate(site).model_dump()
        )
        for site in sites
    ]


def _ensure_plr_uuid(resource: Optional["PLRResource"]) -> Optional[str]:
    if resource is None:
        return None
    resource_uuid = getattr(resource, "unilabos_uuid", "")
    if not resource_uuid:
        raise ValueError(f"PLR 资源 {resource.name} 缺少微后端分配的 unilabos_uuid")
    return str(resource_uuid)


def get_plr_template_name(
    resource: "PLRResource", serialized: Optional[Dict[str, Any]] = None
) -> str:
    """读取 PLR 对象携带的模板名，并兼容旧序列化字段。"""

    extra = getattr(resource, "unilabos_extra", {}) or {}
    if not isinstance(extra, dict):
        raise ValueError(f"{resource.name}.unilabos_extra 必须是对象")
    extra_template_name = extra.get(EXTRA_RESOURCE_CLASS)
    serialized = serialized or {}
    serialized_template_name = serialized.get("template_name")
    if (
        extra_template_name
        and serialized_template_name
        and str(extra_template_name) != str(serialized_template_name)
    ):
        raise ValueError(
            f"资源 {resource.name} 的 extra.{EXTRA_RESOURCE_CLASS} 与序列化 template_name 冲突"
        )
    return str(
        extra_template_name
        or serialized_template_name
        or serialized.get("model")
        or serialized.get("type")
        or resource.__class__.__name__
    )


def set_plr_template_name(resource: "PLRResource", template_name: str) -> None:
    """通过 ``unilabos_resource_class`` 向 PLR 对象注入模板名。"""

    normalized = str(template_name).strip()
    if not normalized:
        raise ValueError(f"资源 {resource.name} 的 template_name 不能为空")
    extra = copy.deepcopy(getattr(resource, "unilabos_extra", {}) or {})
    if not isinstance(extra, dict):
        raise ValueError(f"{resource.name}.unilabos_extra 必须是对象")
    existing = extra.get(EXTRA_RESOURCE_CLASS)
    if existing and str(existing) != normalized:
        raise ValueError(
            f"资源 {resource.name} 的 extra.{EXTRA_RESOURCE_CLASS}={existing!r} "
            f"与 template_name={normalized!r} 冲突"
        )
    extra[EXTRA_RESOURCE_CLASS] = normalized
    resource.unilabos_extra = extra


def _inject_plr_site_sidecar(
    resource: "PLRResource", site_defs: List[ResourceSite]
) -> None:
    """把规范 Site 元数据注入 PLR 对象，不修改其原生 Site 数据结构。"""

    extra = copy.deepcopy(getattr(resource, "unilabos_extra", {}) or {})
    if not isinstance(extra, dict):
        raise ValueError(f"{resource.name}.unilabos_extra 必须是对象")
    extra[EXTRA_SITES] = {site.label: site.model_dump() for site in site_defs}
    resource.unilabos_extra = extra


def _validate_canonical_plr_sites(
    resource: "PLRResource",
    sites: List[ResourceSite],
    owner_uuid: str,
    template_name: str,
) -> List[ResourceSite]:
    """核对 PLR 类持有或输出的 canonical Site 快照。"""

    result = [site.model_copy(deep=True) for site in sites]
    for ordinal, site in enumerate(result):
        if site.material_uuid != owner_uuid:
            raise ValueError(
                f"PLR 资源 {resource.name} 的 Site[{ordinal}] material_uuid 与 owner uuid 冲突"
            )
        if site.template_name != template_name:
            raise ValueError(
                f"PLR 资源 {resource.name} 的 Site[{ordinal}] template_name 与 owner template_name 冲突"
            )
    _inject_plr_site_sidecar(resource, result)
    return result


def _seed_random_plr_sites(resource: "PLRResource", owner_uuid: str) -> None:
    """为创建草稿补齐 ItemizedCarrier 的临时 canonical Site 快照。"""

    if getattr(resource, "resource_sites", None) is not None:
        return
    site_setter = getattr(resource, "set_resource_sites", None)
    child_locations = getattr(resource, "child_locations", None)
    child_size = getattr(resource, "child_size", None)
    occupants = getattr(resource, "sites", None)
    if (
        not callable(site_setter)
        or not isinstance(child_locations, dict)
        or not isinstance(child_size, dict)
        or not isinstance(occupants, list)
    ):
        return

    from pylabrobot.resources import ResourceHolder

    template_name = get_plr_template_name(resource)
    invisible_slots = getattr(resource, "invisible_slots", []) or []
    if isinstance(invisible_slots, str):
        invisible_slots = [invisible_slots]
    draft_sites: List[ResourceSite] = []
    for ordinal, (site_index, location) in enumerate(child_locations.items()):
        label = str(site_index)
        occupant = occupants[ordinal] if ordinal < len(occupants) else None
        occupied_material_uuid = None
        if occupant is not None and not isinstance(occupant, ResourceHolder):
            occupied_material_uuid = getattr(occupant, "unilabos_uuid", "") or None
            if occupied_material_uuid is None:
                occupied_material_uuid = str(uuid.uuid4())
                occupant.unilabos_uuid = occupied_material_uuid
        size = child_size.get(site_index) or {}
        draft_sites.append(
            ResourceSite(
                uuid=str(uuid.uuid4()),
                template_name=template_name,
                material_uuid=owner_uuid,
                index=site_index if isinstance(site_index, (int, str)) else ordinal,
                label=label,
                visible=site_index not in invisible_slots and label not in invisible_slots,
                occupied_material_uuid=occupied_material_uuid,
                pose={
                    "position": {
                        "x": getattr(location, "x", 0.0),
                        "y": getattr(location, "y", 0.0),
                        "z": getattr(location, "z", 0.0),
                    },
                    "position3d": {
                        "x": getattr(location, "x", 0.0),
                        "y": getattr(location, "y", 0.0),
                        "z": getattr(location, "z", 0.0),
                    },
                    "size": size,
                },
            )
        )
    site_setter(draft_sites)


def extract_plr_sites(
    resource: "PLRResource", serialized: Optional[Dict[str, Any]] = None
) -> Optional[List[ResourceSite]]:
    """从标准 Carrier 或 canonical ``ResourceSite`` 存储抽取规范快照。"""

    owner_uuid = _ensure_plr_uuid(resource)
    template_name = get_plr_template_name(resource, serialized)
    resource_sites = getattr(resource, "resource_sites", None)
    if isinstance(resource_sites, list) and all(
        isinstance(site, ResourceSite) for site in resource_sites
    ):
        return _validate_canonical_plr_sites(
            resource, resource_sites, owner_uuid, template_name
        )

    plr_sites = getattr(resource, "sites", None)
    if isinstance(plr_sites, list) and all(
        isinstance(site, ResourceSite) for site in plr_sites
    ):
        return _validate_canonical_plr_sites(
            resource, plr_sites, owner_uuid, template_name
        )

    serialized_sites = (serialized or {}).get("sites")
    if isinstance(serialized_sites, list):
        result = [ResourceSite.model_validate(site) for site in serialized_sites]
        return _validate_canonical_plr_sites(
            resource, result, owner_uuid, template_name
        )
    if not isinstance(plr_sites, dict):
        return None
    from pylabrobot.resources.carrier import Carrier

    if not isinstance(resource, Carrier):
        return None
    site_items = list(plr_sites.items())
    if any(
        site is None or not hasattr(site, "get_size_x") or not hasattr(site, "location")
        for _, site in site_items
    ):
        return None

    result = []
    for ordinal, (site_index, site_holder) in enumerate(site_items):
        site_uuid = getattr(site_holder, "unilabos_site_uuid", "")
        if not site_uuid:
            raise ValueError(
                f"载架 {resource.name} 的 Site {site_index} 缺少微后端分配的 UUID"
            )
        location = getattr(site_holder, "location", None)
        held_resource = getattr(site_holder, "resource", None)
        rotation = getattr(site_holder, "rotation", None)
        metadata = copy.deepcopy(
            getattr(site_holder, "unilabos_site_metadata", {}) or {}
        )
        payload = {
            **metadata,
            "schema_version": 1,
            "uuid": str(site_uuid),
            "template_name": template_name,
            "material_uuid": owner_uuid,
            "index": site_index if isinstance(site_index, (int, str)) else ordinal,
            "label": str(getattr(site_holder, "name", site_index)),
            "visible": bool(
                getattr(site_holder, "visible", metadata.get("visible", True))
            ),
            "occupied_material_uuid": _ensure_plr_uuid(held_resource),
            "pose": {
                "size": {
                    "width": site_holder.get_size_x(),
                    "height": site_holder.get_size_y(),
                    "depth": site_holder.get_size_z(),
                },
                "position": {
                    "x": getattr(location, "x", 0.0) if location is not None else 0.0,
                    "y": getattr(location, "y", 0.0) if location is not None else 0.0,
                    "z": getattr(location, "z", 0.0) if location is not None else 0.0,
                },
                "position3d": {
                    "x": getattr(location, "x", 0.0) if location is not None else 0.0,
                    "y": getattr(location, "y", 0.0) if location is not None else 0.0,
                    "z": getattr(location, "z", 0.0) if location is not None else 0.0,
                },
                "rotation": {
                    "x": getattr(rotation, "x", 0.0) if rotation is not None else 0.0,
                    "y": getattr(rotation, "y", 0.0) if rotation is not None else 0.0,
                    "z": getattr(rotation, "z", 0.0) if rotation is not None else 0.0,
                },
            },
            "allowed_resource_categories": list(
                metadata.get("allowed_resource_categories", []) or []
            ),
        }
        result.append(ResourceSite.model_validate(payload))
    return result


def apply_plr_site_metadata(
    resource: "PLRResource",
    sites_by_name: Dict[str, List[Union[ResourceSite, Dict[str, Any]]]],
) -> None:
    """把根级 Site 元数据恢复到反序列化后的 PLR 树，保证再次序列化不丢字段。"""

    raw_site_defs = sites_by_name.get(resource.name)
    site_defs = [
        site if isinstance(site, ResourceSite) else ResourceSite.model_validate(site)
        for site in (raw_site_defs or [])
    ]
    plr_sites = getattr(resource, "sites", None)
    site_setter = getattr(resource, "set_resource_sites", None)
    if raw_site_defs is not None and callable(site_setter):
        site_setter(site_defs)
    elif raw_site_defs is not None and isinstance(plr_sites, dict):
        remaining = dict(plr_sites)
        by_name = {
            str(getattr(site, "name", key)): (key, site)
            for key, site in remaining.items()
        }
        restored: Dict[Union[int, str], Any] = {}
        used_keys: set[Union[int, str]] = set()
        for ordinal, site_def in enumerate(site_defs):
            current_key = None
            site_holder = None
            if site_def.label in by_name:
                current_key, site_holder = by_name[site_def.label]
            elif site_def.index in remaining:
                current_key = site_def.index
                site_holder = remaining[current_key]
            elif ordinal < len(remaining):
                current_key, site_holder = list(remaining.items())[ordinal]
            if site_holder is None:
                raise ValueError(f"PLR 载架 {resource.name} 缺少 Site {site_def.label}")
            site_holder.unilabos_site_uuid = site_def.uuid
            site_holder.unilabos_site_metadata = site_def.model_dump()
            site_holder.visible = site_def.visible
            restored[site_def.index] = site_holder
            if current_key is not None:
                used_keys.add(current_key)
        for current_key, site_holder in remaining.items():
            if current_key not in used_keys:
                restored[current_key] = site_holder
        resource.sites = restored
    elif raw_site_defs is not None and isinstance(plr_sites, list):
        if all(isinstance(site, ResourceSite) for site in plr_sites):
            # ResourceSite 存储是类自身的实现细节；这里只按实际值核对 canonical
            # 快照，不读取标记，也不调用设备类私有的序列化方法。
            if len(plr_sites) != len(site_defs):
                raise ValueError(
                    f"PLR 资源 {resource.name} 的 Site 数量与根字段不一致: "
                    f"native={len(plr_sites)}, canonical={len(site_defs)}"
                )
            children_by_uuid: Dict[str, Any] = {}
            for child in getattr(resource, "children", []) or []:
                child_uuid = _ensure_plr_uuid(child)
                if child_uuid is not None:
                    children_by_uuid[child_uuid] = child
            for ordinal, (native_site, site_def) in enumerate(
                zip(plr_sites, site_defs)
            ):
                if native_site != site_def:
                    raise ValueError(
                        f"PLR 资源 {resource.name} 的 Site[{ordinal}] 与根字段不一致: "
                        f"native={native_site.model_dump()}, canonical={site_def.model_dump()}"
                    )
                if site_def.occupied_material_uuid is not None:
                    occupant = children_by_uuid.get(site_def.occupied_material_uuid)
                    if occupant is None:
                        raise ValueError(
                            f"PLR 资源 {resource.name} 的 Site {site_def.label} 找不到占用物料 "
                            f"UUID={site_def.occupied_material_uuid}"
                        )
        else:
            raise ValueError(
                f"PLR 资源 {resource.name} 的 sites 必须使用 canonical ResourceSite"
            )

    if raw_site_defs is not None:
        _inject_plr_site_sidecar(resource, site_defs)

    for child in resource.children:
        apply_plr_site_metadata(child, sites_by_name)


def merge_resource_sites(
    current_sites: Optional[List[Dict[str, Any]]],
    incoming_sites: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    """旧 Resource PATCH 的只读兼容合并；新写入禁止调用。

    Site 占用的新唯一写入口是带 ``command_id/expected_version`` 的
    place/clear command。此函数仅用于读取旧 payload 时避免缺项被解释为删除。
    Site 完成初始化后，除 ``occupied_material_uuid`` 外的字段均为固定定义。
    UUID 不同但 label/index 相同，或同一 UUID 的固定字段发生变化，都视为
    身份/模板冲突，防止运行态上报覆盖已经持久化的 Site 规格。
    """

    if incoming_sites is None:
        return copy.deepcopy(current_sites)
    if current_sites is None:
        return [
            (
                site.model_dump()
                if isinstance(site, ResourceSite)
                else ResourceSite.model_validate(site).model_dump()
            )
            for site in incoming_sites
        ]

    def as_payload(site: Union[ResourceSite, Dict[str, Any]]) -> Dict[str, Any]:
        return (
            site.model_dump() if isinstance(site, ResourceSite) else copy.deepcopy(site)
        )

    result = [as_payload(site) for site in current_sites]
    uuid_to_index = {
        str(site.get("uuid")): index
        for index, site in enumerate(result)
        if site.get("uuid")
    }
    label_to_site = {
        str(site.get("label", "")).casefold(): site
        for site in result
        if site.get("label")
    }
    index_to_site = {
        (type(site.get("index")).__name__, site.get("index")): site
        for site in result
        if site.get("index") is not None
    }

    for raw_incoming in incoming_sites:
        incoming = as_payload(raw_incoming)
        incoming_uuid = str(incoming.get("uuid") or "")
        existing_index = uuid_to_index.get(incoming_uuid)
        if existing_index is None:
            same_label = label_to_site.get(str(incoming.get("label", "")).casefold())
            same_index = index_to_site.get(
                (type(incoming.get("index")).__name__, incoming.get("index"))
            )
            collision = same_label or same_index
            if collision is not None:
                if not collision.get("uuid"):
                    existing_index = result.index(collision)
                    collision["uuid"] = incoming_uuid
                    uuid_to_index[incoming_uuid] = existing_index
                else:
                    raise ValueError(
                        f"Site 身份冲突: label/index 已存在但 UUID 从 {collision.get('uuid')} 变为 {incoming_uuid}"
                    )
            if existing_index is None:
                canonical_incoming = ResourceSite.model_validate(incoming).model_dump()
                result.append(canonical_incoming)
                uuid_to_index[incoming_uuid] = len(result) - 1
                continue

        existing = result[existing_index]
        if "schema_version" not in existing:
            existing.setdefault("uuid", incoming_uuid)
            existing.setdefault("material_uuid", incoming.get("material_uuid"))
            existing.setdefault("template_name", incoming.get("template_name"))
            existing = ResourceSite.model_validate(existing).model_dump()
            result[existing_index] = existing
        canonical_existing = ResourceSite.model_validate(existing).model_dump()
        canonical_incoming = ResourceSite.model_validate(
            {**canonical_existing, **incoming}
        ).model_dump()
        for immutable_key in ResourceSite.model_fields:
            if immutable_key == "occupied_material_uuid":
                continue
            if canonical_existing[immutable_key] != canonical_incoming[immutable_key]:
                raise ValueError(
                    f"Site {incoming_uuid} 的不可变字段 {immutable_key} 冲突: "
                    f"{canonical_existing[immutable_key]!r} != {canonical_incoming[immutable_key]!r}"
                )
        canonical_existing["occupied_material_uuid"] = canonical_incoming[
            "occupied_material_uuid"
        ]
        result[existing_index] = canonical_existing

    seen_occupants: Dict[str, str] = {}
    for site in result:
        occupant_uuid = site.get("occupied_material_uuid")
        if not occupant_uuid:
            continue
        previous = seen_occupants.get(str(occupant_uuid))
        if previous is not None:
            raise ValueError(
                f"物料 {occupant_uuid} 同时占用 Site {previous} 和 {site.get('uuid')}"
            )
        seen_occupants[str(occupant_uuid)] = str(site.get("uuid"))
    return result


class GraphData(BaseModel):
    """图数据结构，包含节点和边"""

    nodes: List["ResourceTreeInstance"] = Field(
        description="Resource nodes list", default_factory=list
    )
    links: List[Dict[str, Any]] = Field(
        description="Resource links/edges list", default_factory=list
    )


class ResourceDictInstance(object):
    """ResourceDict的实例，同时提供一些方法"""

    def __init__(self, res_content: "ResourceDict"):
        self.res_content = res_content
        self.children: List[ResourceDictInstance] = []
        self.typ = "dict"

    @classmethod
    def get_resource_instance_from_dict(
        cls,
        content: ResourceDictType,
    ) -> "ResourceDictInstance":
        """从字典创建资源实例"""
        # children 属于 ResourceTree 的递归容器，不是 ResourceDict 领域字段。
        # 规范模型采用 extra=forbid，因此在树边界显式剥离，避免依赖静默忽略。
        content = copy.deepcopy(content)
        content.pop("children", None)
        if "id" not in content:
            content["id"] = content["name"]
        if not content.get("uuid"):
            transport_uuid = (content.get("data") or {}).get("unilabos_uuid")
            if not transport_uuid:
                raise ValueError(
                    f"资源 {content.get('id', content.get('name'))} 缺少微后端分配的 UUID"
                )
            content["uuid"] = str(transport_uuid)
        if "description" in content and content["description"] is None:
            # noinspection PyTypedDict
            del content["description"]
        if "model" in content and content["model"] is None:
            # noinspection PyTypedDict
            del content["model"]
        # noinspection PyTypedDict
        if "schema" in content and content["schema"] is None:
            # noinspection PyTypedDict
            del content["schema"]
        if not content.get("class"):
            # noinspection PyTypedDict
            content["class"] = ""
        if not content.get("config"):  # todo: 后续从后端保证字段非空
            content["config"] = {}
        if not content.get("data"):
            content["data"] = {}
        if not content.get("extra"):  # MagicCode
            content["extra"] = {}
        # 旧 PLR 输入可能只有 config.size_*；它只补静态 pose.size，绝不把运行时
        # position 镜像进 pose.position。
        if content.get("pose") is None and content["config"].get("pose") is None:
            size_keys = ("size_x", "size_y", "size_z")
            if any(key in content["config"] for key in size_keys):
                content["pose"] = {
                    "size": ResourceDictPositionSizeType(
                        width=content["config"].get("size_x", 0),
                        height=content["config"].get("size_y", 0),
                        depth=content["config"].get("size_z", 0),
                    )
                }
        try:
            res_dict = ResourceDict.model_validate(content)
            return ResourceDictInstance(res_dict)
        except ValidationError as err:
            raise err

    def get_plr_nested_dict(self) -> Dict[str, Any]:
        """获取资源实例的嵌套字典表示（根字段回装为 PLR 形式）。"""
        res_dict = self.res_content.model_dump(by_alias=True)
        res_dict["children"] = {
            child.res_content.id: child.get_plr_nested_dict() for child in self.children
        }
        res_dict["parent"] = self.res_content.parent_instance_name
        res_dict["extra"] = copy.deepcopy(res_dict.get("extra") or {})
        res_dict["location"] = (
            self.res_content.pose.position.model_dump()
            if self.res_content.pose.position is not None
            else None
        )
        res_dict["extra"][EXTRA_RESOURCE_POSE] = self.res_content.pose.model_dump(
            exclude={"position"}
        )
        joint_state = res_dict.pop("joint_state", None)
        if joint_state is not None:
            res_dict["extra"][EXTRA_RESOURCE_JOINT_STATE] = joint_state
        res_dict["extra"][EXTRA_RESOURCE_META_DATA] = copy.deepcopy(
            self.res_content.meta_data
        )
        del res_dict["pose"]
        del res_dict["meta_data"]
        barcode = res_dict.pop("barcode", "")
        symbology = res_dict.pop("barcode_symbology", "")
        res_dict["barcode"] = (
            {
                "data": barcode,
                "symbology": symbology or "",
                "position_on_resource": "front",
            }
            if barcode
            else None
        )
        res_dict["data"] = assemble_tracker_state(self.res_content)
        for state_key in TRACKER_STATE_KEYS:
            res_dict.pop(state_key, None)
        return res_dict


class ResourceTreeInstance(object):
    """
    资源树，表示一个根节点及其所有子节点的层次结构，继承ResourceDictInstance表示自己是根节点
    """

    @staticmethod
    def _build_uuid_map(
        resource_list: List[ResourceDictInstance],
    ) -> Dict[str, ResourceDictInstance]:
        """构建uuid到资源对象的映射，并检查重复"""
        uuid_map: Dict[str, ResourceDictInstance] = {}
        for res_instance in resource_list:
            res = res_instance.res_content
            if res.uuid in uuid_map:
                raise ValueError(f"发现重复的uuid: {res.uuid}")
            uuid_map[res.uuid] = res_instance
        return uuid_map

    @staticmethod
    def _build_uuid_instance_map(
        resource_list: List[ResourceDictInstance],
    ) -> Dict[str, ResourceDictInstance]:
        """构建uuid到资源实例的映射"""
        return {
            res_instance.res_content.uuid: res_instance
            for res_instance in resource_list
        }

    @staticmethod
    def _collect_tree_nodes(
        root_instance: ResourceDictInstance, uuid_map: Dict[str, ResourceDict]
    ) -> List[ResourceDictInstance]:
        """使用BFS收集属于某个根节点的所有节点"""
        # BFS遍历，根据parent_uuid字段找到所有属于这棵树的节点
        tree_nodes = [root_instance]
        visited = {root_instance.res_content.uuid}
        queue = [root_instance.res_content.uuid]

        while queue:
            current_uuid = queue.pop(0)
            # 查找所有parent_uuid指向当前节点的子节点
            for uuid_str, res in uuid_map.items():
                if res.uuid_parent == current_uuid and uuid_str not in visited:
                    child_instance = ResourceDictInstance(res)
                    tree_nodes.append(child_instance)
                    visited.add(uuid_str)
                    queue.append(uuid_str)

        return tree_nodes

    def __init__(self, resource: ResourceDictInstance):
        self.root_node = resource
        self._validate_tree()

    def _validate_tree(self):
        """
        验证树结构的一致性
        - 验证uuid唯一性
        - 验证parent-children关系一致性

        Raises:
            ValueError: 当发现不一致时
        """
        known_uuids: set[str] = set()
        uuid_to_resource: Dict[str, ResourceDict] = {}
        site_uuid_to_owner: Dict[str, str] = {}
        occupant_to_site: Dict[str, str] = {}

        def validate_node(node: ResourceDictInstance):
            # 检查uuid唯一性
            if node.res_content.uuid in known_uuids:
                raise ValueError(f"发现重复的uuid: {node.res_content.uuid}")
            if node.res_content.uuid:
                known_uuids.add(node.res_content.uuid)
                uuid_to_resource[node.res_content.uuid] = node.res_content
            else:
                logger.warning(f"警告: 资源 {node.res_content.id} 没有uuid")

            for site in node.res_content.sites or []:
                existing_owner = site_uuid_to_owner.get(site.uuid)
                if existing_owner is not None:
                    raise ValueError(
                        f"Site UUID {site.uuid} 同时属于物料 {existing_owner} 和 {node.res_content.uuid}"
                    )
                site_uuid_to_owner[site.uuid] = node.res_content.uuid
                if site.occupied_material_uuid:
                    existing_site = occupant_to_site.get(site.occupied_material_uuid)
                    if existing_site is not None:
                        raise ValueError(
                            f"物料 {site.occupied_material_uuid} 同时占用 Site {existing_site} 和 {site.uuid}"
                        )
                    occupant_to_site[site.occupied_material_uuid] = site.uuid

            # 验证并递归处理子节点
            for child in node.children:
                if child.res_content.parent != node.res_content:
                    parent_id = (
                        child.res_content.parent.id
                        if child.res_content.parent
                        else None
                    )
                    raise ValueError(
                        f"节点 {child.res_content.id} 的parent引用不正确，应该指向 {node.res_content.id}，但实际指向 {parent_id}"
                    )
                validate_node(child)

        validate_node(self.root_node)

        # 占用关系反向检查：occupied_material_uuid 必须指向所属物料子树中的真实物料。
        # 允许标准 PLR Carrier 的 ResourceHolder 中间层，因此校验“后代”而非仅直接 parent。
        for owner_node in self.get_all_nodes():
            owner = owner_node.res_content
            for site in owner.sites or []:
                occupant_uuid = site.occupied_material_uuid
                if not occupant_uuid:
                    continue
                occupant = uuid_to_resource.get(occupant_uuid)
                if occupant is None:
                    raise ValueError(
                        f"Site {site.uuid} 引用的 occupied_material_uuid={occupant_uuid} 不在物料树中"
                    )
                current = occupant
                ancestor_uuids: set[str] = set()
                while current.parent is not None and current.uuid not in ancestor_uuids:
                    ancestor_uuids.add(current.uuid)
                    if current.parent.uuid == owner.uuid:
                        break
                    current = current.parent
                else:
                    raise ValueError(
                        f"Site {site.uuid} 的占用物料 {occupant_uuid} 不属于 owner {owner.uuid} 的子树"
                    )

    def get_all_nodes(self) -> List[ResourceDictInstance]:
        """
        获取树中的所有节点（深度优先遍历）

        Returns:
            所有节点的资源实例列表
        """
        nodes = []

        def collect_nodes(node: ResourceDictInstance):
            nodes.append(node)
            for child in node.children:
                collect_nodes(child)

        collect_nodes(self.root_node)
        return nodes

    def find_by_uuid(self, target_uuid: str) -> Optional[ResourceDictInstance]:
        """
        通过uuid查找节点

        Args:
            target_uuid: 目标uuid

        Returns:
            找到的节点资源实例，如果没找到返回None
        """

        def search(node: ResourceDictInstance) -> Optional[ResourceDictInstance]:
            if node.res_content.uuid == target_uuid:
                return node
            for child in node.children:
                res = search(child)
                if res:
                    return res
            return None

        result = search(self.root_node)
        return result


class ResourceTreeSet(object):
    """
    多个根节点的resource集合，包含多个ResourceTree
    """

    def __init__(
        self,
        resource_list: List[List[ResourceDictInstance]] | List[ResourceTreeInstance],
    ):
        """
        初始化资源树集合

        Args:
            resource_list: 可以是以下两种类型之一：
                - List[ResourceTree]: 已经构建好的树列表
                - List[List[ResourceInstanceDict]]: 嵌套列表，每个内部列表代表一棵树

        Raises:
            TypeError: 当传入不支持的类型时
        """
        if not resource_list:
            self.trees: List[ResourceTreeInstance] = []
        elif isinstance(resource_list[0], ResourceTreeInstance):
            # 已经是ResourceTree列表
            self.trees = cast(List[ResourceTreeInstance], resource_list)
        else:
            raise TypeError(
                f"不支持的类型: {type(resource_list[0])}。"
                f"ResourceTreeSet 只接受 List[ResourceTree] 或 List[List[ResourceInstanceDict]]"
            )

    @classmethod
    def from_plr_resources(
        cls,
        resources: List["PLRResource"],
        known_newly_created=False,
        old_size=False,
        *,
        known_random_uuid: bool = False,
    ) -> "ResourceTreeSet":
        """
        从 PLR 资源创建 ResourceTreeSet。

        ``known_random_uuid`` 只用于尚未登记的创建草稿/模板测试。开启后会为
        缺少 UUID 的 Resource 和 Carrier Site 递归生成临时 UUID；它们只是
        client_ref，必须再交给微后端 create 并使用返回的权威 UUID 树。

        ``known_newly_created`` 为兼容旧调用保留，不再改变 UUID 校验规则。
        """

        missing = object()

        def replace_plr_type(source: str):
            replace_info = {
                "plate": "plate",
                "well": "well",
                "deck": "deck",
                "tip_rack": "tip_rack",
                "tip_spot": "tip_spot",
                "tube": "tube",
                "bottle_carrier": "bottle_carrier",
                "material_hole": "material_hole",
                "container": "container",
                "material_plate": "material_plate",
                "electrode_sheet": "electrode_sheet",
                "warehouse": "warehouse",
                "magazine_holder": "magazine_holder",
                "resource_group": "resource_group",
                "trash": "trash",
                "plate_adapter": "plate_adapter",
                "consumable": "consumable",
                "tool": "tool",
                "condenser": "condenser",
                "crucible": "crucible",
                "reagent_bottle": "reagent_bottle",
                "flask": "flask",
                "beaker": "beaker",
            }
            if source in replace_info:
                return replace_info[source]
            elif source is None:
                return ""
            else:
                logger.trace(f"转换pylabrobot的时候，出现未知类型 {source}")
                return source

        def build_uuid_mapping(
            res: "PLRResource", uuid_list: list, parent_uuid: Optional[str] = None
        ):
            """递归构建uuid和extra映射字典，返回(current_uuid, parent_uuid, extra)元组列表"""
            uid = getattr(res, "unilabos_uuid", "")
            if not uid:
                if not known_random_uuid:
                    raise ValueError(
                        f"PLR 资源 {res.name} 缺少微后端分配的 UUID；"
                        "请先调用 runtime create 并用返回的规范树构造 PLR"
                    )
                uid = str(uuid.uuid4())
                res.unilabos_uuid = uid

            if known_random_uuid:
                _seed_random_plr_sites(res, uid)

            plr_sites = getattr(res, "sites", None)
            if isinstance(plr_sites, dict):
                for site_index, site_holder in plr_sites.items():
                    if site_holder is None:
                        continue
                    if not getattr(site_holder, "unilabos_site_uuid", ""):
                        if not known_random_uuid:
                            raise ValueError(
                                f"载架 {res.name} 的 Site {site_index} "
                                "缺少微后端分配的 UUID"
                            )
                        site_holder.unilabos_site_uuid = str(uuid.uuid4())

            # 获取unilabos_extra，默认为空字典
            extra = copy.deepcopy(getattr(res, "unilabos_extra", {}) or {})
            if not isinstance(extra, dict):
                raise ValueError(f"{res.name}.unilabos_extra 必须是对象")
            # Site sidecar 只属于 PLR 运行时；ResourceDict 以根字段 sites 为唯一真相。
            extra.pop(EXTRA_SITES, None)
            # 模板名提升后不再保留在 Resource.extra，避免双真相。
            extra.pop(EXTRA_RESOURCE_CLASS, None)

            static_pose = extra.pop(EXTRA_RESOURCE_POSE, None)
            joint_state = extra.pop(EXTRA_RESOURCE_JOINT_STATE, None)
            resource_meta_data = extra.pop(EXTRA_RESOURCE_META_DATA, missing)
            if resource_meta_data is not missing and not isinstance(
                resource_meta_data, Mapping
            ):
                raise ValueError(
                    f"{res.name}.unilabos_extra.{EXTRA_RESOURCE_META_DATA} 必须是对象"
                )
            legacy_pose_extra = extra.pop(FRONTEND_POSE_EXTRA, None)
            uuid_list.append(
                (
                    uid,
                    parent_uuid,
                    extra,
                    static_pose,
                    joint_state,
                    legacy_pose_extra,
                    (
                        copy.deepcopy(dict(resource_meta_data))
                        if resource_meta_data is not missing
                        else missing
                    ),
                )
            )
            for child in res.children:
                build_uuid_mapping(child, uuid_list, uid)

        def resource_plr_inner(
            plr_resource: "PLRResource",
            d: dict,
            parent_resource: Optional[ResourceDict],
            states: dict,
            uuids: list,
        ) -> ResourceDictInstance:
            (
                current_uuid,
                parent_uuid,
                extra,
                static_pose,
                joint_state,
                legacy_pose_extra,
                resource_meta_data,
            ) = uuids.pop(0)

            serialized_location = d.get("location")
            raw_pos = (
                {
                    "x": serialized_location["x"],
                    "y": serialized_location["y"],
                    "z": serialized_location["z"],
                }
                if serialized_location is not None
                else None
            )
            sidecar_position = (
                copy.deepcopy(static_pose.get("position"))
                if isinstance(static_pose, dict) and "position" in static_pose
                else missing
            )
            if static_pose is None:
                serialized_rotation = d.get("rotation") or {"x": 0, "y": 0, "z": 0}
                static_pose = {
                    "size": {
                        "width": d["size_x"],
                        "height": d["size_y"],
                        "depth": d["size_z"],
                    },
                    "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                    "layout": d.get("layout", "x-y"),
                    # PLR serializer 会额外输出 ``type=Rotation``；它是传输标签，
                    # 不属于规范静态几何模型。
                    "rotation": {
                        "x": serialized_rotation["x"],
                        "y": serialized_rotation["y"],
                        "z": serialized_rotation["z"],
                    },
                    "cross_section_type": d.get("cross_section_type", "rectangle"),
                    "extra": legacy_pose_extra,
                }
            if raw_pos is not None:
                if sidecar_position is not missing:
                    normalized_sidecar_position = ResourceDictPositionObject.model_validate(
                        sidecar_position
                    ).model_dump()
                    if normalized_sidecar_position != raw_pos:
                        raise ValueError(
                            f"PLR 资源 {d['name']} 的 location 与 "
                            f"unilabos_extra.{EXTRA_RESOURCE_POSE}.position 冲突"
                        )
                static_pose["position"] = raw_pos

            # 先构建当前节点的字典（不包含children）
            r_dict = {
                "id": d["name"],
                "uuid": current_uuid,
                "name": d["name"],
                "parent": parent_resource,  # 直接传入 ResourceDict 对象
                "parent_uuid": parent_uuid,  # 使用 parent_uuid 而不是 parent 对象
                "type": replace_plr_type(d.get("category", "")),
                "class": d.get("class", ""),
                "template_name": get_plr_template_name(plr_resource, d),
                "pose": static_pose,
                "joint_state": joint_state,
                "config": {
                    k: v
                    for k, v in d.items()
                    if k
                    not in (
                        [
                            "name",
                            "template_name",
                            "sites",
                            "children",
                            "parent_name",
                            "location",
                            "rotation",
                            "size_x",
                            "size_y",
                            "size_z",
                            "cross_section_type",
                            "bottom_type",
                        ]
                        if not old_size
                        else [
                            "name",
                            "template_name",
                            "sites",
                            "children",
                            "parent_name",
                            "location",
                            "rotation",
                            "cross_section_type",
                            "bottom_type",
                        ]
                    )
                },
                "data": states[d["name"]],
                "extra": extra,
                "sites": extract_plr_sites(plr_resource, d),
                "sites_initialized": True,
            }
            if resource_meta_data is not missing:
                r_dict["meta_data"] = resource_meta_data

            # 先转换为 ResourceDictInstance，获取其中的 ResourceDict
            current_instance = ResourceDictInstance.get_resource_instance_from_dict(
                r_dict
            )
            current_resource = current_instance.res_content

            # 递归处理子节点，传入当前节点的 ResourceDict 作为 parent
            current_instance.children = [
                resource_plr_inner(
                    child_resource, child_dict, current_resource, states, uuids
                )
                for child_resource, child_dict in zip(
                    plr_resource.children, d.get("children", [])
                )
            ]

            return current_instance

        trees = []
        for resource in resources:
            # 构建uuid列表
            uuid_list = []
            build_uuid_mapping(
                resource, uuid_list, getattr(resource.parent, "unilabos_uuid", None)
            )

            serialized_data = resource.serialize()
            all_states = resource.serialize_all_state()

            # 根节点没有父节点，传入 None
            root_instance = resource_plr_inner(
                resource, serialized_data, None, all_states, uuid_list
            )
            tree_instance = ResourceTreeInstance(root_instance)
            trees.append(tree_instance)
        return cls(trees)

    def to_plr_resources(self, skip_devices=True) -> List["PLRResource"]:
        """
        将 ResourceTreeSet 转换为 PLR 资源列表

        Returns:
            List[PLRResource]: PLR 资源实例列表
        """
        register()
        from pylabrobot.resources import Resource as PLRResource
        from pylabrobot.utils.object_parsing import find_subclass

        # 类型映射
        TYPE_MAP = {
            "plate": "Plate",
            "well": "Well",
            "deck": "Deck",
            "container": "RegularContainer",
            "tip_spot": "TipSpot",
        }

        def collect_node_data(
            node: ResourceDictInstance,
            name_to_uuid: dict,
            all_states: dict,
            name_to_extra: dict,
            name_to_sites: dict,
        ):
            """一次遍历收集 UUID、state、extra、Site 与模板名称。"""
            name_to_uuid[node.res_content.name] = node.res_content.uuid
            all_states[node.res_content.name] = assemble_tracker_state(node.res_content)
            plr_extra = copy.deepcopy(node.res_content.extra)
            plr_extra[EXTRA_RESOURCE_POSE] = node.res_content.pose.model_dump(
                exclude={"position"}
            )
            if node.res_content.joint_state is not None:
                plr_extra[EXTRA_RESOURCE_JOINT_STATE] = (
                    node.res_content.joint_state.model_dump()
                )
            plr_extra[EXTRA_RESOURCE_META_DATA] = copy.deepcopy(
                node.res_content.meta_data
            )
            plr_extra[FRONTEND_POSE_EXTRA] = node.res_content.pose.extra
            plr_extra[EXTRA_RESOURCE_CLASS] = node.res_content.template_name
            name_to_extra[node.res_content.name] = plr_extra
            if node.res_content.sites is not None:
                name_to_sites[node.res_content.name] = [
                    site.model_dump() for site in node.res_content.sites
                ]
            for child in node.children:
                collect_node_data(
                    child,
                    name_to_uuid,
                    all_states,
                    name_to_extra,
                    name_to_sites,
                )

        def node_to_plr_dict(node: ResourceDictInstance, has_model: bool):
            """转换节点为 PLR 字典格式"""
            res = node.res_content
            plr_type = TYPE_MAP.get(res.type, res.type)
            if res.type not in TYPE_MAP:
                logger.warning(f"未知类型 {res.type}")

            # 反序列化方向：把根字段 barcode/barcode_symbology 组装回 config 的 barcode
            # （PLR Barcode dict {data, symbology, position_on_resource}），与
            # get_resource_instance_from_dict 从 config 读取的逻辑对称。PLR location
            # 对应唯一的 pose.position；其余 pose 字段通过 unilabos_extra sidecar 保留。
            config = dict(res.config)
            config.pop("sites", None)
            config.pop("template_name", None)
            if res.barcode:
                config["barcode"] = {
                    "data": res.barcode,
                    "symbology": res.barcode_symbology or "",
                    "position_on_resource": "front",
                }
            d = {
                **config,
                "name": res.name,
                "type": res.config.get("type", plr_type),
                "size_x": res.pose.size.width,
                "size_y": res.pose.size.height,
                "size_z": res.pose.size.depth,
                "location": (
                    {
                        "x": res.pose.position.x,
                        "y": res.pose.position.y,
                        "z": res.pose.position.z,
                        "type": "Coordinate",
                    }
                    if res.pose.position is not None
                    else None
                ),
                "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
                "category": res.config.get("category", plr_type),
                "children": [
                    node_to_plr_dict(child, has_model) for child in node.children
                ],
                "parent_name": res.parent_instance_name,
            }
            if has_model:
                d["model"] = res.config.get("model", None)
            if res.sites is not None:
                site_cls = find_subclass(d["type"], PLRResource)
                if site_cls is not None and plr_class_accepts_serialized_sites(
                    site_cls
                ):
                    d["sites"] = sites_for_plr_deserialization(res.sites)
            return d

        plr_resources = []
        tracker = DeviceNodeResourceTracker()

        for tree in self.trees:
            name_to_uuid: Dict[str, str] = {}
            all_states: Dict[str, Any] = {}
            name_to_extra: Dict[str, dict] = {}
            name_to_sites: Dict[str, List[Dict[str, Any]]] = {}
            collect_node_data(
                tree.root_node,
                name_to_uuid,
                all_states,
                name_to_extra,
                name_to_sites,
            )
            has_model = tree.root_node.res_content.type != "deck"
            plr_dict = node_to_plr_dict(tree.root_node, has_model)
            try:
                sub_cls = find_subclass(plr_dict["type"], PLRResource)
                if skip_devices and plr_dict["type"] == "device":
                    logger.info(f"跳过更新 {plr_dict['name']} 设备是class")
                    continue
                elif sub_cls is None:
                    raise ValueError(
                        f"无法找到类型 {plr_dict['type']} 对应的 PLR 资源类。原始信息：{tree.root_node.res_content}"
                    )
                spec = inspect.signature(sub_cls)
                if "category" not in spec.parameters:
                    plr_dict.pop("category", None)
                plr_resource = sub_cls.deserialize(plr_dict, allow_marshal=True)
                # PLR 的 Resource.deserialize 仍不恢复自身 location；统一只在
                # UniLabOS 适配边界补一次，避免再改 PLR 各个子类的 deserialize。
                from pylabrobot.resources import Coordinate
                from pylabrobot.serializer import deserialize

                plr_resource.location = (
                    cast(Coordinate, deserialize(plr_dict["location"]))
                    if plr_dict["location"] is not None
                    else None
                )
                plr_resource.load_all_state(all_states)
                # 使用 DeviceNodeResourceTracker 设置 UUID 和 Extra
                tracker.loop_set_uuid(plr_resource, name_to_uuid)
                tracker.loop_set_extra(plr_resource, name_to_extra)
                apply_plr_site_metadata(plr_resource, name_to_sites)
                plr_resources.append(plr_resource)

            except Exception as e:
                logger.error(f"转换 PLR 资源失败: {e} {str(plr_dict)[:1000]}")
                import traceback

                logger.error(f"堆栈: {traceback.format_exc()}")
                raise

        return plr_resources

    @classmethod
    def from_raw_dict_list(cls, raw_list: List[Dict[str, Any]]) -> "ResourceTreeSet":
        """
        从原始字典列表创建 ResourceTreeSet，自动建立 parent-children 关系

        Args:
            raw_list: 原始字典列表，每个字典代表一个资源节点

        Returns:
            ResourceTreeSet 实例

        Raises:
            ValueError: 当建立关系时发现不一致
        """
        # 第一步：校验微后端 UUID。
        for node_dict in raw_list:
            if not node_dict.get("uuid"):
                transport_uuid = (node_dict.get("data") or {}).get("unilabos_uuid")
                if not transport_uuid:
                    raise ValueError(
                        f"资源 {node_dict.get('id', node_dict.get('name'))} 缺少微后端分配的 UUID"
                    )
                node_dict["uuid"] = str(transport_uuid)

        # 第二步：将字典列表转换为 ResourceDictInstance 列表。
        instances = [
            ResourceDictInstance.get_resource_instance_from_dict(node_dict)
            for node_dict in raw_list
        ]

        # 第三步：建立映射关系
        uuid_to_instance: Dict[str, ResourceDictInstance] = {}
        id_to_instance: Dict[str, ResourceDictInstance] = {}

        for raw_node, instance in zip(raw_list, instances):
            # 建立 uuid 映射
            if instance.res_content.uuid:
                uuid_to_instance[instance.res_content.uuid] = instance
            # 建立 id 映射
            if instance.res_content.id:
                id_to_instance[instance.res_content.id] = instance

        # 第四步：建立 parent-children 关系
        for raw_node, instance in zip(raw_list, instances):
            # 优先使用 parent_uuid 进行匹配，如果不存在则使用 parent (id)
            parent_uuid = raw_node.get("parent_uuid")
            parent_id = raw_node.get("parent")
            parent_instance = None

            # 优先用 parent_uuid 匹配
            if parent_uuid and parent_uuid in uuid_to_instance:
                parent_instance = uuid_to_instance[parent_uuid]
            # 否则用 parent (id) 匹配
            elif parent_id and parent_id in id_to_instance:
                parent_instance = id_to_instance[parent_id]

            # 设置 parent 引用并建立 children 关系
            if parent_instance:
                instance.res_content.parent = parent_instance.res_content
                # 将当前节点添加到父节点的 children 列表（避免重复添加）
                if instance not in parent_instance.children:
                    parent_instance.children.append(instance)

        # 第五步：使用 from_nested_list 创建 ResourceTreeSet
        return cls.from_nested_instance_list(instances)

    @classmethod
    def from_nested_instance_list(
        cls, nested_list: List[ResourceDictInstance]
    ) -> "ResourceTreeSet":
        """
        从扁平化的资源列表创建ResourceTreeSet，自动按根节点分组

        Args:
            nested_list: 扁平化的资源实例列表，可能包含多个根节点

        Returns:
            ResourceTreeSet实例

        Raises:
            ValueError: 当没有找到任何根节点时
        """
        # 找到所有根节点
        known_uuids = {res_instance.res_content.uuid for res_instance in nested_list}
        root_instances = [
            ResourceTreeInstance(res_instance)
            for res_instance in nested_list
            if res_instance.res_content.is_root_node
            or res_instance.res_content.uuid_parent not in known_uuids
        ]
        return cls(root_instances)

    @property
    def root_nodes(self) -> List[ResourceDictInstance]:
        """
        获取所有树的根节点

        Returns:
            所有根节点的资源实例列表
        """
        return [tree.root_node for tree in self.trees]

    @property
    def root_nodes_uuid(self) -> List[ResourceDictInstance]:
        """
        获取所有树的根节点

        Returns:
            所有根节点的资源实例列表
        """
        return [tree.root_node.res_content.uuid for tree in self.trees]

    @property
    def all_nodes(self) -> List[ResourceDictInstance]:
        """
        获取所有树中的所有节点

        Returns:
            所有节点的资源实例列表
        """
        return [node for tree in self.trees for node in tree.get_all_nodes()]

    @property
    def all_nodes_uuid(self) -> List[str]:
        """
        获取所有树中的所有节点

        Returns:
            所有节点的资源实例列表
        """
        return [
            node.res_content.uuid
            for tree in self.trees
            for node in tree.get_all_nodes()
        ]

    def find_by_uuid(self, target_uuid: str) -> Optional[ResourceDictInstance]:
        """
        在所有树中通过uuid查找节点

        Args:
            target_uuid: 目标uuid

        Returns:
            找到的节点资源实例，如果没找到返回None
        """
        for tree in self.trees:
            result = tree.find_by_uuid(target_uuid)
            if result:
                return result
        return None

    def replace_resource_uuids(self, uuid_mapping: Mapping[str, str]) -> int:
        """离线迁移工具：原子替换资源 UUID，并同步树与 Site 引用。

        正常 create/import/load 路径禁止调用；微后端 UUID 是最终身份，不存在
        ``cloud_uuid`` 二次替换。映射只作用于 ResourceDict UUID，Site 自身 UUID
        是独立身份。先在副本上完成完整模型/树校验，成功后才替换当前树内容。
        """

        if not isinstance(uuid_mapping, Mapping):
            raise ValueError("uuid_mapping 必须是对象")

        nodes = self.all_nodes
        old_uuid_set = {node.res_content.uuid for node in nodes}
        replacements: Dict[str, str] = {}
        for old_uuid, new_uuid in uuid_mapping.items():
            if not isinstance(old_uuid, str) or not old_uuid.strip():
                raise ValueError("uuid_mapping 的旧 UUID 必须是非空字符串")
            if not isinstance(new_uuid, str) or not new_uuid.strip():
                raise ValueError(f"资源 {old_uuid} 的新 UUID 必须是非空字符串")
            old_uuid = old_uuid.strip()
            new_uuid = new_uuid.strip()
            if old_uuid in old_uuid_set and old_uuid != new_uuid:
                replacements[old_uuid] = new_uuid

        if not replacements:
            return 0

        final_uuids = [
            replacements.get(node.res_content.uuid, node.res_content.uuid)
            for node in nodes
        ]
        if len(final_uuids) != len(set(final_uuids)):
            raise ValueError("UUID 替换后会产生重复资源 UUID")

        candidate_payload = self.dump()
        for tree_payload in candidate_payload:
            for resource in tree_payload:
                resource["uuid"] = replacements.get(resource["uuid"], resource["uuid"])
                parent_uuid = resource.get("parent_uuid")
                if parent_uuid:
                    resource["parent_uuid"] = replacements.get(parent_uuid, parent_uuid)
                for site in resource.get("sites") or []:
                    owner_uuid = site.get("material_uuid")
                    if owner_uuid:
                        site["material_uuid"] = replacements.get(owner_uuid, owner_uuid)
                    occupant_uuid = site.get("occupied_material_uuid")
                    if occupant_uuid:
                        site["occupied_material_uuid"] = replacements.get(
                            occupant_uuid, occupant_uuid
                        )

        candidate = ResourceTreeSet.load(candidate_payload)
        candidate_by_uuid = {
            node.res_content.uuid: node.res_content for node in candidate.all_nodes
        }
        for node in nodes:
            old_uuid = node.res_content.uuid
            node.res_content = candidate_by_uuid[replacements.get(old_uuid, old_uuid)]
        for tree in self.trees:
            tree._validate_tree()
        return len(replacements)

    def merge_remote_resources(
        self, remote_tree_set: "ResourceTreeSet"
    ) -> "ResourceTreeSet":
        """
        将远端物料同步到本地物料中（以子树为单位）

        同步规则：
        1. 一级节点（根节点）：如果不存在的物料，引入整个子树
        2. 一级设备下的二级物料：如果不存在，引入整个子树
        3. 二级设备下的三级物料：如果不存在，引入整个子树
        如果存在则跳过并提示

        Args:
            remote_tree_set: 远端的资源树集合

        Returns:
            合并后的资源树集合（self）
        """
        # 构建本地映射：一级 device id -> 根节点实例
        local_device_map: Dict[str, ResourceDictInstance] = {}
        for root_node in self.root_nodes:
            if root_node.res_content.type == "device":
                local_device_map[root_node.res_content.id] = root_node

        # 记录需要添加的新根节点（不属于任何 device 的物料）
        new_root_nodes: List[ResourceDictInstance] = []

        # 遍历远端根节点
        for remote_root in remote_tree_set.root_nodes:
            remote_root_id = remote_root.res_content.id
            remote_root_type = remote_root.res_content.type

            if remote_root_type == "device":
                # 情况1: 一级是 device
                if remote_root_id not in local_device_map:
                    if remote_root_id != "host_node":
                        logger.warning(
                            f"Device '{remote_root_id}' 在本地不存在，跳过该 device 下的物料同步"
                        )
                    continue

                local_device = local_device_map[remote_root_id]

                # 构建本地一级 device 下的子节点映射
                local_children_map = {
                    child.res_content.name: child for child in local_device.children
                }

                # 遍历远端一级 device 的子节点
                for remote_child in remote_root.children:
                    remote_child_name = remote_child.res_content.name
                    remote_child_type = remote_child.res_content.type

                    if remote_child_type == "device":
                        # 情况2: 二级是 device
                        if remote_child_name not in local_children_map:
                            logger.warning(
                                f"Device '{remote_root_id}/{remote_child_name}' 在本地不存在，跳过"
                            )
                            continue

                        local_sub_device = local_children_map[remote_child_name]

                        # 构建本地二级 device 下的子节点映射
                        local_sub_children_map = {
                            child.res_content.name: child
                            for child in local_sub_device.children
                        }

                        # 遍历远端二级 device 的子节点（三级物料）
                        added_count = 0
                        for remote_material in remote_child.children:
                            remote_material_name = remote_material.res_content.name

                            # 情况3: 三级物料
                            if remote_material_name not in local_sub_children_map:
                                # 引入整个子树
                                remote_material.res_content.parent = (
                                    local_sub_device.res_content
                                )
                                local_sub_device.children.append(remote_material)
                                added_count += 1
                            else:
                                logger.info(
                                    f"物料 '{remote_root_id}/{remote_child_name}/{remote_material_name}' "
                                    f"已存在，跳过"
                                )

                        if added_count > 0:
                            logger.info(
                                f"Device '{remote_root_id}/{remote_child_name}': "
                                f"从远端同步了 {added_count} 个物料子树"
                            )
                    else:
                        # 二级物料已存在，比较三级子节点是否缺失
                        local_material = local_children_map[remote_child_name]
                        local_material_children_map = {
                            child.res_content.name: child
                            for child in local_material.children
                        }
                        added_count = 0
                        for remote_sub in remote_child.children:
                            remote_sub_name = remote_sub.res_content.name
                            if remote_sub_name not in local_material_children_map:
                                remote_sub.res_content.parent = (
                                    local_material.res_content
                                )
                                local_material.children.append(remote_sub)
                                added_count += 1
                            else:
                                logger.info(
                                    f"物料 '{remote_root_id}/{remote_child_name}/{remote_sub_name}' "
                                    f"已存在，跳过"
                                )
                        if added_count > 0:
                            logger.info(
                                f"物料 '{remote_root_id}/{remote_child_name}': "
                                f"从远端同步了 {added_count} 个子物料"
                            )
            else:
                # 情况1: 一级节点是物料（不是 device）
                # 检查是否已存在
                existing = False
                for local_root in self.root_nodes:
                    if local_root.res_content.name == remote_root.res_content.name:
                        existing = True
                        logger.info(
                            f"根节点物料 '{remote_root.res_content.name}' 已存在，跳过"
                        )
                        break

                if not existing:
                    # 引入整个子树
                    new_root_nodes.append(remote_root)
                    logger.info(f"添加远端独立物料根节点子树: '{remote_root_id}'")

        # 将新的根节点添加到本地树集合
        if new_root_nodes:
            for new_root in new_root_nodes:
                self.trees.append(ResourceTreeInstance(new_root))

        return self

    def dump(self) -> List[List[ResourceDictType]]:
        """
        将 ResourceTreeSet 序列化为嵌套列表格式

        序列化时：
        - parent 自动转换为 parent_uuid（在 ResourceDict.model_dump 中处理）
        - children 不会被序列化（exclude=True）

        Returns:
            List[List[Dict]]: 每个内层列表代表一棵树的扁平化资源字典列表
        """
        result = []
        for tree in self.trees:
            # 获取树的所有节点并序列化
            tree_nodes = [
                node.res_content.model_dump(by_alias=True)
                for node in tree.get_all_nodes()
            ]
            result.append(tree_nodes)
        return result

    @classmethod
    def load(cls, data: List[List[Dict[str, Any]]]) -> "ResourceTreeSet":
        """
        从序列化的嵌套列表格式反序列化为 ResourceTreeSet

        Args:
            data: List[List[Dict]]: 序列化的数据，每个内层列表代表一棵树

        Returns:
            ResourceTreeSet: 反序列化后的资源树集合
        """
        nested_lists = []
        for tree_data in data:
            nested_lists.extend(ResourceTreeSet.from_raw_dict_list(tree_data).trees)
        return cls(nested_lists)


def prepare_resource_creation_payloads(
    resources: List[Dict[str, Any]],
) -> Tuple[ResourceTreeSet, List[Dict[str, Any]]]:
    """校验微后端 runtime create 返回的规范资源树。

    返回值中的 payload 保留 ROS ``children`` 传输字段，但其余资源字段均来自
    ``ResourceDict`` 的规范序列化。该函数不生成资源/Site UUID，也不展开
    ``available_sites``；调用方必须先通过微后端获得最终实例快照。
    """

    normalized_inputs = copy.deepcopy(resources)
    for resource in normalized_inputs:
        config = resource.get("config")
        if isinstance(config, dict):
            config.pop("available_sites", None)
        resource.pop("available_sites", None)
        if not resource.get("uuid") and not (resource.get("data") or {}).get(
            "unilabos_uuid"
        ):
            raise ValueError(
                f"资源 {resource.get('id', resource.get('name'))} 缺少微后端分配的 UUID"
            )
        if resource.get("sites_initialized") is not True:
            raise ValueError(
                f"资源 {resource.get('id', resource.get('name'))} 缺少微后端权威 Site 快照"
            )
        if resource.get("sites") is None:
            resource["sites"] = []

    resource_tree = ResourceTreeSet.from_raw_dict_list(normalized_inputs)
    canonical_by_uuid = {
        node.res_content.uuid: node.res_content.model_dump(by_alias=True)
        for node in resource_tree.all_nodes
    }
    prepared: List[Dict[str, Any]] = []
    for original, normalized in zip(resources, normalized_inputs):
        canonical = canonical_by_uuid.get(str(normalized.get("uuid") or ""))
        if canonical is None:
            raise ValueError(
                f"物料 {normalized.get('id', normalized.get('name'))} 创建预备失败"
            )
        payload = copy.deepcopy(canonical)
        payload["children"] = copy.deepcopy(original.get("children", []))
        prepared.append(payload)
    return resource_tree, prepared


def prepare_resource_tree_for_creation(resources: ResourceTreeSet) -> int:
    """校验启动资源树已经来自微后端规范快照，不修改任何实例事实。"""

    count = 0
    for node in resources.all_nodes:
        resource = node.res_content
        if not resource.sites_initialized:
            raise ValueError(f"资源 {resource.id} 尚未由微后端生成权威 Site 快照")
        if resource.sites is None:
            raise ValueError(f"资源 {resource.id} 的 sites 必须是数组")
        count += 1
    for tree in resources.trees:
        tree._validate_tree()
    return count


class DeviceNodeResourceTracker(object):
    def __init__(self):
        self.resources = []
        self.resource2parent_resource = {}
        self.uuid_to_resources = {}
        pass

    def prefix_path(self, resource):
        resource_prefix_path = "/"
        resource_parent = getattr(resource, "parent", None)
        while resource_parent is not None:
            resource_prefix_path = f"/{resource_parent.name}" + resource_prefix_path
            resource_parent = resource_parent.parent

        return resource_prefix_path

    def map_uuid_to_resource(self, resource, uuid_map: Dict[str, str]):
        for old_uuid, new_uuid in uuid_map.items():
            if old_uuid != new_uuid:
                if old_uuid in self.uuid_to_resources:
                    instance = self.uuid_to_resources.pop(old_uuid)
                    if isinstance(resource, dict):
                        resource["uuid"] = new_uuid
                    else:  # 实例的
                        setattr(instance, "unilabos_uuid", new_uuid)
                    self.uuid_to_resources[new_uuid] = instance
                    print(f"更新uuid映射: {old_uuid} -> {new_uuid} | {instance}")

    def _get_resource_attr(
        self, resource, attr_name: str, uuid_attr: Optional[str] = None
    ):
        """
        获取资源的属性值，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            attr_name: dict类型使用的属性名
            uuid_attr: instance类型使用的属性名（用于uuid字段），默认与attr_name相同

        Returns:
            属性值，不存在则返回None
        """
        if uuid_attr is None:
            uuid_attr = attr_name

        if isinstance(resource, dict):
            value = resource.get(attr_name)
            if value or attr_name != "uuid":
                return value
            data = resource.get("data")
            if isinstance(data, dict):
                return data.get("unilabos_uuid")
            return None
        else:
            return getattr(resource, uuid_attr, None)

    @classmethod
    def set_resource_uuid(cls, resource, new_uuid: str):
        """
        设置资源的 uuid，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            new_uuid: 新的uuid值
        """
        if isinstance(resource, dict):
            resource["uuid"] = new_uuid
            data = resource.get("data")
            if isinstance(data, dict) and "unilabos_uuid" in data:
                data["unilabos_uuid"] = new_uuid
        else:
            setattr(resource, "unilabos_uuid", new_uuid)

    @staticmethod
    def set_resource_extra(resource, extra: dict):
        """
        设置资源的 extra，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            extra: extra字典值
        """
        if isinstance(resource, dict):
            c_extra = resource.get("extra", {})
            c_extra.update(extra)
            resource["extra"] = c_extra
        else:
            c_extra = getattr(resource, "unilabos_extra", {})
            c_extra.update(extra)
            setattr(resource, "unilabos_extra", c_extra)

    def _traverse_and_process(self, resource, process_func) -> int:
        """
        递归遍历资源树，对每个节点执行处理函数

        Args:
            resource: 资源对象（可以是list、dict或实例）
            process_func: 处理函数，接收resource参数，返回处理的节点数量

        Returns:
            处理的节点总数量
        """
        if isinstance(resource, list):
            return sum(self._traverse_and_process(r, process_func) for r in resource)

        # 先递归处理所有子节点
        count = 0
        children = (
            resource.get("children", [])
            if isinstance(resource, dict)
            else getattr(resource, "children", [])
        )
        for child in children:
            count += self._traverse_and_process(child, process_func)

        # 处理当前节点
        count += process_func(resource)
        return count

    def loop_set_uuid(self, resource, name_to_uuid_map: Dict[str, str]) -> int:
        """
        递归遍历资源树，根据 name 设置所有节点的 uuid

        Args:
            resource: 资源对象（可以是dict或实例）
            name_to_uuid_map: name到uuid的映射字典，{name: uuid}

        Returns:
            更新的资源数量
        """

        def process(res):
            resource_name = self._get_resource_attr(res, "name")
            if resource_name and resource_name in name_to_uuid_map:
                new_uuid = name_to_uuid_map[resource_name]
                self.set_resource_uuid(res, new_uuid)
                self.uuid_to_resources[new_uuid] = res
                logger.trace(f"设置资源UUID: {resource_name} -> {new_uuid}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def loop_find_with_uuid(self, resource, target_uuid: str):
        """
        递归遍历资源树，根据 uuid 查找并返回对应的资源

        Args:
            resource: 资源对象（可以是list、dict或实例）
            target_uuid: 要查找的uuid

        Returns:
            找到的资源对象，未找到则返回None
        """
        found_resource = None

        def process(res):
            nonlocal found_resource
            if found_resource is not None:
                return 0  # 已找到，跳过后续处理
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid and current_uuid == target_uuid:
                found_resource = res
                logger.trace(f"找到资源UUID: {target_uuid}")
                return 1
            return 0

        self._traverse_and_process(resource, process)
        return found_resource

    def loop_set_extra(self, resource, name_to_extra_map: Dict[str, dict]) -> int:
        """
        递归遍历资源树，根据 name 设置所有节点的 extra

        Args:
            resource: 资源对象（可以是dict或实例）
            name_to_extra_map: name到extra的映射字典，{name: extra}

        Returns:
            更新的资源数量
        """

        def process(res):
            resource_name = self._get_resource_attr(res, "name")
            if resource_name and resource_name in name_to_extra_map:
                extra = name_to_extra_map[resource_name]
                self.set_resource_extra(res, extra)
                if len(extra):
                    logger.trace(f"设置资源Extra: {resource_name} -> {extra}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def loop_update_uuid(self, resource, uuid_map: Dict[str, str]) -> int:
        """
        离线迁移兼容：递归遍历资源树并更新所有节点 UUID。

        正常 Edge 创建/加载链路禁止调用；微后端返回的 UUID 即最终身份。

        Args:
            resource: 资源对象（可以是dict或实例）
            uuid_map: uuid映射字典，{old_uuid: new_uuid}

        Returns:
            更新的资源数量
        """

        def replace_site_references(res) -> None:
            if isinstance(res, dict):
                parent_uuid = res.get("parent_uuid")
                if parent_uuid in uuid_map:
                    res["parent_uuid"] = uuid_map[parent_uuid]
                site_collections = [res.get("sites")]
                extra = res.get("extra") or {}
            else:
                site_collections = []
                extra = getattr(res, "unilabos_extra", {}) or {}
            if isinstance(extra, dict):
                site_collections.append(extra.get(EXTRA_SITES))

            for sites in site_collections:
                if isinstance(sites, dict):
                    site_values = sites.values()
                elif isinstance(sites, list):
                    site_values = sites
                else:
                    continue
                for site in site_values:
                    if not isinstance(site, dict):
                        continue
                    owner_uuid = site.get("material_uuid")
                    if owner_uuid in uuid_map:
                        site["material_uuid"] = uuid_map[owner_uuid]
                    occupant_uuid = site.get("occupied_material_uuid")
                    if occupant_uuid in uuid_map:
                        site["occupied_material_uuid"] = uuid_map[occupant_uuid]

        def process(res):
            replace_site_references(res)
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            replaced = 0
            if current_uuid and current_uuid in uuid_map:
                new_uuid = uuid_map[current_uuid]
                if current_uuid != new_uuid:
                    self.set_resource_uuid(res, new_uuid)
                    # 更新uuid_to_resources映射
                    if current_uuid in self.uuid_to_resources:
                        self.uuid_to_resources.pop(current_uuid)
                    self.uuid_to_resources[new_uuid] = res
                    logger.trace(f"更新uuid: {current_uuid} -> {new_uuid}")
                    replaced = 1
            return replaced

        return self._traverse_and_process(resource, process)

    def loop_gather_uuid(self, resource) -> List[str]:
        """
        递归遍历资源树，收集所有节点的uuid

        Args:
            resource: 资源对象（可以是dict或实例）

        Returns:
            收集到的uuid列表
        """
        uuid_list = []

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid:
                uuid_list.append(current_uuid)
            return 0

        self._traverse_and_process(resource, process)
        return uuid_list

    def _collect_uuid_mapping(self, resource):
        """
        递归收集资源的 uuid 映射到 uuid_to_resources

        Args:
            resource: 资源对象（可以是dict或实例）
        """

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid:
                old = self.uuid_to_resources.get(current_uuid)
                self.uuid_to_resources[current_uuid] = res
                logger.trace(
                    f"收集资源UUID映射: {current_uuid} -> {res} {'' if old is None else f'(覆盖旧值: {old})'}"
                )
                return 1
            return 0

        self._traverse_and_process(resource, process)

    def _remove_uuid_mapping(self, resource) -> int:
        """
        递归清除资源的 uuid 映射

        Args:
            resource: 资源对象（可以是dict或实例）
        """

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid and current_uuid in self.uuid_to_resources:
                self.uuid_to_resources.pop(current_uuid)
                logger.trace(f"移除资源UUID映射: {current_uuid} -> {res}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def parent_resource(self, resource):
        if id(resource) in self.resource2parent_resource:
            return self.resource2parent_resource[id(resource)]
        else:
            return resource

    def add_resource(self, resource):
        """
        添加资源到追踪器

        Args:
            resource: 资源对象（可以是dict或实例）
        """
        root_uuids = {}
        for r in self.resources:
            res_uuid = (
                r.get("uuid")
                if isinstance(r, dict)
                else getattr(r, "unilabos_uuid", None)
            )
            if res_uuid:
                root_uuids[res_uuid] = r
            if id(r) == id(resource):
                return

        # 这里只做uuid的根节点比较
        if isinstance(resource, dict):
            res_uuid = resource.get("uuid")
        else:
            res_uuid = getattr(resource, "unilabos_uuid", None)
        if res_uuid in root_uuids:
            old_res = root_uuids[res_uuid]
            # self.remove_resource(old_res)
            logger.warning(f"资源{resource}已存在，旧资源: {old_res}")
        self.resources.append(resource)
        # 递归收集uuid映射
        self._collect_uuid_mapping(resource)

    def remove_resource(self, resource) -> bool:
        """
        从追踪器中移除资源

        Args:
            resource: 资源对象（可以是dict或实例）

        Returns:
            bool: 如果成功移除返回True，资源不存在返回False
        """
        # 从 resources 列表中移除
        resource_id = id(resource)
        removed = False
        for i, r in enumerate(self.resources):
            if id(r) == resource_id:
                self.resources.pop(i)
                removed = True
                break

        # 递归清除uuid映射
        count = self._remove_uuid_mapping(resource)
        if not count:
            logger.warning(f"尝试移除不存在的资源: {resource}")
            return False

        # 清除 resource2parent_resource 中与该资源相关的映射
        # 需要清除：1) 该资源作为 key 的映射 2) 该资源作为 value 的映射
        keys_to_remove = []
        for key, value in self.resource2parent_resource.items():
            if id(value) == resource_id:
                keys_to_remove.append(key)

        if resource_id in self.resource2parent_resource:
            keys_to_remove.append(resource_id)

        for key in keys_to_remove:
            self.resource2parent_resource.pop(key, None)

        logger.trace(f"[ResourceTracker] 成功移除资源: {resource}")
        return True

    def clear_resource(self):
        """清空所有资源"""
        self.resources = []
        self.uuid_to_resources.clear()
        self.resource2parent_resource.clear()

    def figure_resource(
        self,
        query_resource: Union[List[Union[dict, "PLRResource"]], dict, "PLRResource"],
        try_mode=False,
    ) -> Union[
        List[Union[dict, "PLRResource", List[Union[dict, "PLRResource"]]]],
        dict,
        "PLRResource",
    ]:
        if isinstance(query_resource, list):
            return [self.figure_resource(r, try_mode) for r in query_resource]
        elif (
            isinstance(query_resource, dict)
            and "id" not in query_resource
            and "name" not in query_resource
            and "uuid" not in query_resource
        ):  # 临时处理，要删除的，driver有太多类型错误标注
            return [self.figure_resource(r, try_mode) for r in query_resource.values()]

        # 优先尝试通过 uuid 查找
        res_uuid = None
        if isinstance(query_resource, dict):
            res_uuid = query_resource.get("uuid")
        else:
            res_uuid = getattr(query_resource, "unilabos_uuid", None)

        # 如果有 uuid，优先使用 uuid 查找
        if res_uuid:
            res_list = []
            for r in self.resources:
                if isinstance(query_resource, dict):
                    res_list.extend(
                        self.loop_find_resource(r, object, "uuid", res_uuid)
                    )
                else:
                    res_list.extend(
                        self.loop_find_resource(
                            r, type(query_resource), "unilabos_uuid", res_uuid
                        )
                    )

            if not try_mode:
                assert len(res_list) > 0, (
                    f"没有找到资源 (uuid={res_uuid})，请检查资源是否存在"
                )
                assert len(res_list) == 1, (
                    f"通过uuid={res_uuid} 找到多个资源，请检查资源是否唯一: {res_list}"
                )
            else:
                return [i[1] for i in res_list]

            self.resource2parent_resource[id(query_resource)] = res_list[0][0]
            self.resource2parent_resource[id(res_list[0][1])] = res_list[0][0]
            return res_list[0][1]

        # 回退到 id/name 查找
        res_id = (
            query_resource.id  # type: ignore
            if hasattr(query_resource, "id")
            else (
                query_resource.get("id") if isinstance(query_resource, dict) else None
            )
        )
        res_name = (
            query_resource.name  # type: ignore
            if hasattr(query_resource, "name")
            else (
                query_resource.get("name") if isinstance(query_resource, dict) else None
            )
        )
        res_identifier = res_id if res_id else res_name
        identifier_key = "id" if res_id else "name"
        resource_cls_type = type(query_resource)
        if res_identifier is None:
            logger.warning(
                f"resource {query_resource} 没有id、name或uuid，暂不能对应figure"
            )
        res_list = []
        for r in self.resources:
            if isinstance(query_resource, dict):
                res_list.extend(
                    self.loop_find_resource(
                        r, object, identifier_key, query_resource[identifier_key]
                    )
                )
            else:
                res_list.extend(
                    self.loop_find_resource(
                        r,
                        resource_cls_type,
                        identifier_key,
                        getattr(query_resource, identifier_key),
                    )
                )
        if not try_mode:
            assert len(res_list) > 0, (
                f"没有找到资源 {query_resource}，请检查资源是否存在"
            )
            assert len(res_list) == 1, (
                f"{query_resource} 找到多个资源，请检查资源是否唯一: {res_list}"
            )
        else:
            return [i[1] for i in res_list]
        # 后续加入其他对比方式
        self.resource2parent_resource[id(query_resource)] = res_list[0][0]
        self.resource2parent_resource[id(res_list[0][1])] = res_list[0][0]
        return res_list[0][1]

    def loop_find_resource(
        self,
        resource,
        target_resource_cls_type,
        identifier_key,
        compare_value,
        parent_res=None,
    ) -> List[Tuple[Any, Any]]:
        res_list = []
        # print(resource, target_resource_cls_type, identifier_key, compare_value)
        children = []
        if not isinstance(resource, dict):
            children = getattr(resource, "children", [])
        else:
            children = resource.get("children")
            if children is not None:
                children = (
                    list(children.values()) if isinstance(children, dict) else children
                )
        for child in children:
            res_list.extend(
                self.loop_find_resource(
                    child,
                    target_resource_cls_type,
                    identifier_key,
                    compare_value,
                    resource,
                )
            )
        if issubclass(type(resource), target_resource_cls_type):
            if type(resource) == dict:
                # 对于字典类型，直接检查 identifier_key
                if identifier_key in resource:
                    if resource[identifier_key] == compare_value:
                        res_list.append((parent_res, resource))
            else:
                # 对于实例类型，需要特殊处理 uuid 字段
                # 如果查找的是 unilabos_uuid，使用 getattr
                if identifier_key == "uuid":
                    identifier_key = "unilabos_uuid"
                if hasattr(resource, identifier_key):
                    if getattr(resource, identifier_key) == compare_value:
                        res_list.append((parent_res, resource))
        return res_list

    def filter_find_list(self, res_list, compare_std_dict):
        new_list = []
        for res in res_list:
            for k, v in compare_std_dict.items():
                if hasattr(res, k):
                    if getattr(res, k) == v:
                        new_list.append(res)
        return new_list


if __name__ == "__main__":
    from pylabrobot.resources import corning_6_wellplate_16point8ml_flat

    # 测试 from_plr_resources 和 to_plr_resources 的往返转换
    print("=" * 60)
    print("测试 PLR 资源转换往返")
    print("=" * 60)

    # 1. 创建一个 PLR 资源并设置 UUID
    original_plate = corning_6_wellplate_16point8ml_flat("test_plate")

    # 使用 DeviceNodeResourceTracker 设置 UUID
    tracker = DeviceNodeResourceTracker()
    name_to_uuid = {}

    # 递归生成 name_to_uuid 映射
    def build_uuid_map(resource):
        name_to_uuid[resource.name] = str(uuid.uuid4())
        for child in resource.children:
            build_uuid_map(child)

    build_uuid_map(original_plate)

    # 使用 tracker 的 loop_set_uuid 方法设置 UUID
    tracker.loop_set_uuid(original_plate, name_to_uuid)

    print(f"\n1. 原始 PLR 资源: {original_plate.name}")
    print(f"   - UUID: {getattr(original_plate, 'unilabos_uuid', 'N/A')}")
    print(f"   - 子节点数量: {len(original_plate.children)}")
    if original_plate.children:
        print(f"   - 第一个子节点: {original_plate.children[0].name}")
        print(
            f"   - 第一个子节点 UUID: {getattr(original_plate.children[0], 'unilabos_uuid', 'N/A')}"
        )

    # 2. 将 PLR 资源转换为 ResourceTreeSet
    resource_tree_set = ResourceTreeSet.from_plr_resources([original_plate])
    print(f"\n2. 转换为 ResourceTreeSet:")
    print(f"   - 树的数量: {len(resource_tree_set.trees)}")
    print(f"   - 根节点: {resource_tree_set.root_nodes[0].res_content.name}")
    print(f"   - 所有节点数量: {len(resource_tree_set.all_nodes)}")

    # 3. 将 ResourceTreeSet 转换回 PLR 资源
    plr_resources = resource_tree_set.to_plr_resources()
    converted_plate = plr_resources[0]
    print(f"\n3. 转换回 PLR 资源: {converted_plate.name}")
    print(f"   - 子节点数量: {len(converted_plate.children)}")
    if converted_plate.children:
        print(f"   - 第一个子节点: {converted_plate.children[0].name}")

    # 4. 验证 unilabos_uuid 属性
    print(f"\n4. 验证 unilabos_uuid 设置:")
    if hasattr(converted_plate, "unilabos_uuid"):
        print(f"   - 根节点 UUID: {getattr(converted_plate, 'unilabos_uuid')}")
        if converted_plate.children and hasattr(
            converted_plate.children[0], "unilabos_uuid"
        ):
            print(
                f"   - 第一个子节点 UUID: {getattr(converted_plate.children[0], 'unilabos_uuid')}"
            )
    else:
        print("   - 警告: unilabos_uuid 未设置")

    # 5. 验证 UUID 保持不变
    print(f"\n5. 验证 UUID 在往返过程中保持不变:")
    original_uuid = getattr(original_plate, "unilabos_uuid")
    converted_uuid = getattr(converted_plate, "unilabos_uuid")
    print(f"   - 原始 UUID: {original_uuid}")
    print(f"   - 转换后 UUID: {converted_uuid}")
    print(f"   - UUID 保持不变: {original_uuid == converted_uuid}")

    # 6. 再次往返转换，验证稳定性
    resource_tree_set_2 = ResourceTreeSet.from_plr_resources([converted_plate])
    plr_resources_2 = resource_tree_set_2.to_plr_resources()
    print(f"\n6. 第二次往返转换:")
    print(f"   - 资源名称: {plr_resources_2[0].name}")
    print(f"   - 子节点数量: {len(plr_resources_2[0].children)}")
    print(
        f"   - UUID 依然保持: {getattr(plr_resources_2[0], 'unilabos_uuid') == original_uuid}"
    )

    print("\n" + "=" * 60)
    print("✅ 测试完成! 所有转换正常工作")
    print("=" * 60)
