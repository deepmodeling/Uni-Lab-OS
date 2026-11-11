import copy
import inspect
import uuid
from pydantic import BaseModel, Field, ValidationError
from typing import Iterator, List, Tuple, Any, Dict, Mapping, Optional, cast, TYPE_CHECKING, Union

from unilabos.resources.objects.resource import (
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_JOINT_STATE,
    EXTRA_RESOURCE_META_DATA,
    EXTRA_RESOURCE_POSE,
    EXTRA_SITES,
    FRONTEND_POSE_EXTRA,
    ResourceDict,
    ResourceDictType,
    assemble_tracker_state,
)
from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.objects.state import TRACKER_STATE_KEYS
from unilabos.resources.plr_additional_res_reg import register
from unilabos.resources.plr_identity import material_children, material_serialization, prepare_plr_restore, restore_tip_counters
from unilabos.resources.objects.pose import (
    ResourceDictPositionObject,
    ResourceDictPositionSizeType,
)
from unilabos.utils.log import logger

if TYPE_CHECKING:
    from pylabrobot.resources import Resource as PLRResource


# 函数参数名常量 - 用于自动注入 sample_uuids 列表
PARAM_SAMPLE_UUIDS = "sample_uuids"

# JSON Command 中的系统参数字段名
JSON_UNILABOS_PARAM = "unilabos_param"

# 返回值中的 samples 字段名
RETURN_UNILABOS_SAMPLES = "unilabos_samples"


def iter_serialized_plr_children(
    resource: "PLRResource", serialized: Mapping[str, Any]
) -> Iterator[Tuple["PLRResource", dict]]:
    """按规范结构匹配实例；已提升的内嵌枪头也参与 UUID 映射。"""
    by_name = {}
    for child in material_children(resource):
        if child.name in by_name:
            raise ValueError(f"PLR 资源 {resource.name} 的孩子重名: {child.name}")
        by_name[child.name] = child
    seen = set()
    for child_data in serialized.get("children", []):
        name = child_data["name"]
        if name in seen or name not in by_name:
            raise ValueError(f"PLR 资源 {resource.name} 的序列化孩子无法唯一匹配: {name}")
        seen.add(name)
        yield by_name[name], child_data


def require_plr_config_type(config: Mapping[str, Any], *, uuid: str, template_name: str) -> str:
    """PLR 实现类型只接受 config.type，禁止从业务字段推断。"""
    class_name = config.get("type")
    context = f"物料 uuid={uuid!r}, template_name={template_name!r}"
    if not isinstance(class_name, str) or not class_name.strip():
        raise ValueError(f"{context}: config.type 必须是非空 PLR 类名字符串")
    if find_plr_resource_class(class_name) is None:
        raise ValueError(f"{context}: config.type={class_name!r} 对应的 PLR 资源类不存在")
    return class_name


def find_plr_resource_class(class_name: str) -> Optional[type]:
    """按类名查 PLR 资源类；本进程尚未 import 时按注册表条目懒加载后再查。

    外部设备包（``--devices`` / 驱动包）的注册表由 AST 扫描得到，不 import 模块；
    Host 侧 host_node 动作按 uuid 从权威拉取 Slave 侧物料（如外部包自定义的 Deck）
    时，类只在注册表条目里有 ``module:ClassName`` 路径。先按类名精确匹配条目，
    再兜底 import 全部 pylabrobot 类型资源条目的模块（函数式 ``@resource`` 返回的
    自定义类在条目里看不到类名）。找不到返回 None，由调用方决定如何报错。
    """

    from pylabrobot.resources import Resource as PLRResource
    from pylabrobot.utils.object_parsing import find_subclass

    found = find_subclass(class_name, PLRResource)
    if found is not None:
        return found
    try:
        from unilabos.registry.registry import lab_registry
    except Exception:  # noqa: BLE001 - 注册表不可用时只能按已加载类查
        return None
    if lab_registry is None:
        return None
    exact: List[str] = []
    fallback: List[str] = []
    for entry in lab_registry.resource_type_registry.values():
        cls = entry.get("class") if isinstance(entry, dict) else None
        module = str(cls.get("module") or "") if isinstance(cls, dict) else ""
        if not isinstance(cls, dict) or cls.get("type") != "pylabrobot" or ":" not in module:
            continue
        module_path, _, name = module.rpartition(":")
        (exact if name == class_name else fallback).append(module_path)
    import importlib

    for module_path in dict.fromkeys([*exact, *fallback]):
        try:
            importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - 单个模块导入失败不影响继续查找
            logger.debug(f"按注册表懒加载资源模块 {module_path} 失败: {exc}")
            continue
        found = find_subclass(class_name, PLRResource)
        if found is not None:
            return found
    return None


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


def repair_itemized_ordering(
    config: Dict[str, Any], serialized_children: List[Dict[str, Any]]
) -> None:
    """按 children 实际顺序原地修复 ItemizedResource ``ordering`` 键序。

    PLR ``ItemizedResource.get_item`` 按 ordering 键序的位置索引直取
    children[idx]；若 config 途中经过键排序（如历史 canonical 存储），
    键序会与 children 顺序错位，孔位标识整体错乱。ordering 的 value 是
    item name，据此按 children 实际顺序重排键序即可无损还原。
    """

    ordering = config.get("ordering")
    if not (isinstance(ordering, dict) and ordering and serialized_children):
        return
    child_positions = {
        child["name"]: index for index, child in enumerate(serialized_children)
    }
    if all(
        isinstance(item_name, str) and item_name in child_positions
        for item_name in ordering.values()
    ):
        config["ordering"] = dict(
            sorted(
                ordering.items(),
                key=lambda entry: child_positions[entry[1]],
            )
        )


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
    """从 PLR 对象或兼容序列化字段读取模板名。"""

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
    explicit_template_name = extra_template_name or serialized_template_name
    if explicit_template_name:
        return str(explicit_template_name)
    if getattr(resource, "parent", None) is not None:
        # PLR 子项的 model 经常继承载具/容器型号（例如 TipSpot.model=Container），
        # 它不是子项模板身份。组件模板统一按具体 class 命名，避免与根模板撞名。
        return resource.__class__.__name__
    return str(
        serialized.get("model")
        or getattr(resource, "model", None)
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
    native_sites = getattr(resource, "sites", None)
    if (
        not callable(site_setter)
        or not isinstance(child_locations, dict)
        or not isinstance(child_size, dict)
        or not isinstance(native_sites, (list, dict))
    ):
        return

    from pylabrobot.resources import ResourceHolder

    template_name = get_plr_template_name(resource)
    invisible_slots = getattr(resource, "invisible_slots", []) or []
    if isinstance(invisible_slots, str):
        invisible_slots = [invisible_slots]
    draft_sites: List[ResourceSite] = []
    site_values = (
        list(native_sites.values())
        if isinstance(native_sites, dict)
        else list(native_sites)
    )
    for ordinal, (site_index, location) in enumerate(child_locations.items()):
        label = str(site_index)
        native_site = site_values[ordinal] if ordinal < len(site_values) else None
        occupant = (
            native_site.resource
            if isinstance(native_site, ResourceHolder)
            else native_site
        )
        occupied_material_uuid = None
        if occupant is not None:
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
        if not content.get("config"):
            content["config"] = {}
        if not content.get("data"):
            content["data"] = {}
        if not content.get("extra"):
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
        old_size=False,
        *,
        known_random_uuid: bool = False,
    ) -> "ResourceTreeSet":
        """
        从 PLR 资源创建 ResourceTreeSet。

        ``known_random_uuid`` 只用于尚未登记的创建草稿/模板测试。开启后会为
        缺少 UUID 的 Resource 和 Carrier Site 递归生成临时 UUID；它们只是
        client_ref，必须再交给微后端 create 并使用返回的权威 UUID 树。
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
            res: "PLRResource", serialized: dict, uuid_list: list, parent_uuid: Optional[str] = None
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
            # 模板名提升到规范根字段，extra 中移除同名 sidecar。
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
            for child, child_data in iter_serialized_plr_children(res, serialized):
                build_uuid_mapping(child, child_data, uuid_list, uid)

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

            resource_state = copy.deepcopy(states[d["name"]])
            state_rotation = resource_state.pop("rotation", None)
            state_location = resource_state.pop("location", None)

            # 结构位置优先（包括 None），兼容仅在 state 中输出位置的 PLR。
            serialized_location = d.get("location", state_location)
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
                serialized_rotation = (
                    d.get("rotation")
                    or state_rotation
                    or {"x": 0, "y": 0, "z": 0}
                )
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
            else:
                serialized_rotation = d.get("rotation") or state_rotation
                if serialized_rotation is not None:
                    static_pose["rotation"] = {
                        "x": serialized_rotation["x"],
                        "y": serialized_rotation["y"],
                        "z": serialized_rotation["z"],
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
                        ]
                    )
                },
                "data": resource_state,
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
                for child_resource, child_dict in iter_serialized_plr_children(plr_resource, d)
            ]

            return current_instance

        trees = []
        for resource in resources:
            # 构建uuid列表
            uuid_list = []
            serialized_data, all_states = material_serialization(resource)
            build_uuid_mapping(
                resource, serialized_data, uuid_list, getattr(resource.parent, "unilabos_uuid", None)
            )


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
            plr_type = require_plr_config_type(
                res.config, uuid=res.uuid, template_name=res.template_name
            )

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
                "type": plr_type,
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
                "rotation": {
                    "x": res.pose.rotation.x,
                    "y": res.pose.rotation.y,
                    "z": res.pose.rotation.z,
                    "type": "Rotation",
                },
                "category": res.config.get("category", plr_type),
                "children": [
                    node_to_plr_dict(child, has_model) for child in node.children
                ],
                "parent_name": res.parent_instance_name,
            }
            repair_itemized_ordering(d, d["children"])
            if has_model:
                d["model"] = res.config.get("model", None)
            if res.sites is not None:
                site_cls = find_plr_resource_class(d["type"])
                if site_cls is not None and plr_class_accepts_serialized_sites(
                    site_cls
                ):
                    d["sites"] = sites_for_plr_deserialization(res.sites)
                # 权威快照重放以 Site 占用为准：把被占用 child 的 location 覆写
                # 为对应 Site 坐标。权威 move 只落 parent/occupied、不回写子节点
                # position，若按旧 position 反序列化，带 Site 校验的容器（如
                # PRCXI deck）会把 child 误配回旧 Site。
                children_by_uuid = {
                    child.res_content.uuid: child_dict
                    for child, child_dict in zip(node.children, d["children"])
                }
                for site in res.sites:
                    occupied = site.occupied_material_uuid
                    child_dict = children_by_uuid.get(occupied) if occupied else None
                    position = site.pose.position if site.pose is not None else None
                    if child_dict is None or position is None:
                        continue
                    child_dict["location"] = {
                        "x": position.x,
                        "y": position.y,
                        "z": position.z,
                        "type": "Coordinate",
                    }
            return d

        plr_resources = []

        for tree in self.trees:
            if skip_devices and tree.root_node.res_content.type == "device":
                continue
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
            prepare_plr_restore(plr_dict, all_states)
            try:
                sub_cls = find_plr_resource_class(plr_dict["type"])
                if sub_cls is None:
                    raise ValueError(
                        f"无法找到类型 {plr_dict['type']} 对应的 PLR 资源类。原始信息：{tree.root_node.res_content}"
                    )
                spec = inspect.signature(sub_cls)
                if "category" not in spec.parameters:
                    plr_dict.pop("category", None)
                plr_resource = sub_cls.deserialize(plr_dict, allow_marshal=True)
                # PLR 的 Resource.deserialize 仍不恢复自身 location；统一只在
                # Uni-Lab-OS 适配边界补一次，避免再改 PLR 各个子类的 deserialize。
                from pylabrobot.resources import Coordinate
                from pylabrobot.serializer import deserialize

                plr_resource.location = (
                    cast(Coordinate, deserialize(plr_dict["location"]))
                    if plr_dict["location"] is not None
                    else None
                )
                plr_resource.load_all_state(all_states)
                restore_tip_counters(plr_resource, all_states)
                restored_count = set_uuids_by_name(plr_resource, name_to_uuid)
                if restored_count != len(name_to_uuid):
                    raise ValueError("PLR 恢复后物料节点数量与权威 UUID 映射不一致")
                set_extras_by_name(plr_resource, name_to_extra)
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
        if not isinstance(data, list):
            raise TypeError("ResourceTreeSet.load 需要 list[list[dict]]")

        trees: list[ResourceTreeInstance] = []
        global_uuids: set[str] = set()
        for group_index, tree_data in enumerate(data):
            if not isinstance(tree_data, list) or not tree_data:
                raise ValueError(
                    f"物料组 {group_index} 必须是非空列表，且只表示一棵根树"
                )
            group_uuids: set[str] = set()
            for node in tree_data:
                if not isinstance(node, dict):
                    raise TypeError(
                        f"物料组 {group_index} 的节点必须是字典"
                    )
                node_uuid = str(
                    node.get("uuid")
                    or (node.get("data") or {}).get("unilabos_uuid")
                    or ""
                ).strip()
                if not node_uuid:
                    raise ValueError(
                        f"物料组 {group_index} 存在缺少微后端 UUID 的节点"
                    )
                if node_uuid in group_uuids:
                    raise ValueError(
                        f"物料组 {group_index} 存在重复 UUID: {node_uuid}"
                    )
                if node_uuid in global_uuids:
                    raise ValueError(
                        f"不同物料组之间存在重复 UUID: {node_uuid}"
                    )
                group_uuids.add(node_uuid)
            parsed = cls.from_raw_dict_list(tree_data)
            if len(parsed.trees) != 1:
                raise ValueError(
                    f"物料组 {group_index} 必须恰好包含一个根节点，"
                    f"实际为 {len(parsed.trees)} 个"
                )
            trees.append(parsed.trees[0])
            global_uuids.update(group_uuids)
        return cls(trees)


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


# ── 资源树工具函数：同时接受 PLR 实例与 dict 形态的资源 ─────────────────────
#
# 这些函数不依赖任何设备状态：批量替换 uuid、按名发号、按 uuid 查找、遍历。
# 设备侧的持有索引（DeviceNodeResourceTracker）和离线迁移工具都建立在它们之上。


def resource_uuid(resource: Any) -> Optional[str]:
    """读取资源 uuid：实例取 ``unilabos_uuid``，dict 取 ``uuid`` 或 ``data.unilabos_uuid``。"""

    if isinstance(resource, dict):
        value = resource.get("uuid")
        if value:
            return value
        data = resource.get("data")
        return data.get("unilabos_uuid") if isinstance(data, dict) else None
    return getattr(resource, "unilabos_uuid", None)


def resource_name(resource: Any) -> Optional[str]:
    if isinstance(resource, dict):
        return resource.get("name")
    return getattr(resource, "name", None)


def resource_children(resource: Any) -> List[Any]:
    if isinstance(resource, dict):
        children = resource.get("children") or []
        return list(children.values()) if isinstance(children, dict) else list(children)
    return material_children(resource)


def walk_resources(resource: Any):
    """前序遍历一棵（或一组）资源树，依次产出每个节点。"""

    if isinstance(resource, list):
        for item in resource:
            yield from walk_resources(item)
        return
    yield resource
    for child in resource_children(resource):
        yield from walk_resources(child)


def set_resource_uuid(resource: Any, new_uuid: str) -> None:
    """设置资源 uuid；dict 形态同时同步 ``data.unilabos_uuid``（若存在）。"""

    if isinstance(resource, dict):
        resource["uuid"] = new_uuid
        data = resource.get("data")
        if isinstance(data, dict) and "unilabos_uuid" in data:
            data["unilabos_uuid"] = new_uuid
    else:
        setattr(resource, "unilabos_uuid", new_uuid)


def set_resource_extra(resource: Any, extra: dict) -> None:
    """合并写入资源 extra（实例为 ``unilabos_extra``）。"""

    if isinstance(resource, dict):
        current = resource.get("extra", {})
        current.update(extra)
        resource["extra"] = current
    else:
        current = getattr(resource, "unilabos_extra", {})
        current.update(extra)
        setattr(resource, "unilabos_extra", current)


def set_uuids_by_name(resource: Any, name_to_uuid: Mapping[str, str]) -> int:
    """按 name 给整棵树的节点设置 uuid（驱动构造期注入权威身份），返回设置数量。"""

    count = 0
    for node in walk_resources(resource):
        name = resource_name(node)
        if name and name in name_to_uuid:
            set_resource_uuid(node, name_to_uuid[name])
            logger.trace(f"设置资源UUID: {name} -> {name_to_uuid[name]}")
            count += 1
    return count


def set_extras_by_name(resource: Any, name_to_extra: Mapping[str, dict]) -> int:
    count = 0
    for node in walk_resources(resource):
        name = resource_name(node)
        if name and name in name_to_extra:
            set_resource_extra(node, name_to_extra[name])
            count += 1
    return count


def find_resource_by_uuid(resource: Any, target_uuid: str) -> Optional[Any]:
    """在一棵（或一组）资源树里按 uuid 找节点；未找到返回 None。"""

    for node in walk_resources(resource):
        if resource_uuid(node) == target_uuid:
            return node
    return None


def replace_resource_uuids_in_tree(resource: Any, uuid_map: Mapping[str, str]) -> int:
    """离线迁移兼容：批量替换整棵树的 uuid，并同步 parent_uuid / Site 引用。

    正常 Edge 创建/加载链路禁止调用；微后端返回的 UUID 即最终身份。
    返回实际改变了 uuid 的节点数。
    """

    def replace_references(node: Any) -> None:
        if isinstance(node, dict):
            parent_uuid = node.get("parent_uuid")
            if parent_uuid in uuid_map:
                node["parent_uuid"] = uuid_map[parent_uuid]
            site_collections: List[Any] = [node.get("sites")]
            extra = node.get("extra") or {}
        else:
            site_collections = []
            extra = getattr(node, "unilabos_extra", {}) or {}
        if isinstance(extra, dict):
            site_collections.append(extra.get(EXTRA_SITES))
        for sites in site_collections:
            if isinstance(sites, dict):
                values = list(sites.values())
            elif isinstance(sites, list):
                values = sites
            else:
                continue
            for site in values:
                if not isinstance(site, dict):
                    continue
                for key in ("material_uuid", "occupied_material_uuid"):
                    if site.get(key) in uuid_map:
                        site[key] = uuid_map[site[key]]

    replaced = 0
    for node in walk_resources(resource):
        replace_references(node)
        current = resource_uuid(node)
        if current and current in uuid_map and uuid_map[current] != current:
            set_resource_uuid(node, uuid_map[current])
            logger.trace(f"更新uuid: {current} -> {uuid_map[current]}")
            replaced += 1
    return replaced


class DeviceNodeResourceTracker(object):
    """设备持有的物料实例索引——不是同步机制。

    设备启动时按权威树装入持有的物料，运行期由权威下行（append / tree sync /
    material sync）增删；这里只维护「这台设备现在持有哪些 PLR 实例」：

    - ``resources``：顶层持有实例（位点上的占用物、直接挂在设备上的台面），有序；
    - ``uuid_to_resources``：全树 uuid → 实例；
    - 名称索引：按 name 定位实例（同名允许，多块板各有 A1）。

    变更同步由挂在 ``_material_snapshot_observer`` 上的 MaterialSnapshotObserver
    负责（``add_resource`` 把新根交给它观察），本类不参与任何权威写入。
    """

    def __init__(self):
        self.resources: List[Any] = []
        self.uuid_to_resources: Dict[str, Any] = {}
        self._names: Dict[str, List[Any]] = {}
        self._parents: Dict[int, Any] = {}

    # ── 索引维护 ──────────────────────────────────────────────

    def _index(self, resource: Any) -> None:
        for node in walk_resources(resource):
            node_uuid = resource_uuid(node)
            if node_uuid:
                old = self.uuid_to_resources.get(node_uuid)
                if old is not None and old is not node:
                    logger.trace(f"资源UUID映射覆盖旧值: {node_uuid} {old} -> {node}")
                self.uuid_to_resources[node_uuid] = node
            name = resource_name(node)
            if name:
                bucket = self._names.setdefault(str(name), [])
                if all(item is not node for item in bucket):
                    bucket.append(node)
            for child in resource_children(node):
                self._parents[id(child)] = node

    def _unindex(self, resource: Any) -> int:
        removed = 0
        for node in walk_resources(resource):
            node_uuid = resource_uuid(node)
            if node_uuid and self.uuid_to_resources.get(node_uuid) is node:
                self.uuid_to_resources.pop(node_uuid)
                removed += 1
            name = resource_name(node)
            if name:
                bucket = [item for item in self._names.get(str(name), []) if item is not node]
                if bucket:
                    self._names[str(name)] = bucket
                else:
                    self._names.pop(str(name), None)
            self._parents.pop(id(node), None)
        return removed

    # ── 持有关系 ──────────────────────────────────────────────

    def add_resource(self, resource: Any) -> None:
        """登记一棵设备持有的物料树（根实例），并交给 snapshot observer 观察。"""

        if any(item is resource for item in self.resources):
            return
        res_uuid = resource_uuid(resource)
        if res_uuid:
            duplicate = next(
                (item for item in self.resources if resource_uuid(item) == res_uuid), None
            )
            if duplicate is not None:
                logger.warning(f"资源{resource}已存在，旧资源: {duplicate}")
        self.resources.append(resource)
        self._index(resource)
        observer = getattr(self, "_material_snapshot_observer", None)
        if observer is not None:
            observer.observe(resource)

    def remove_resource(self, resource: Any) -> bool:
        """移除一棵持有的物料树；资源本就不在索引里时返回 False。"""

        observer = getattr(self, "_material_snapshot_observer", None)
        if observer is not None:
            observer.unobserve(resource)
        for index, item in enumerate(self.resources):
            if item is resource:
                self.resources.pop(index)
                break
        if not self._unindex(resource):
            logger.warning(f"尝试移除不存在的资源: {resource}")
            return False
        logger.trace(f"[ResourceTracker] 成功移除资源: {resource}")
        return True

    def loop_set_uuid(self, resource: Any, name_to_uuid_map: Mapping[str, str]) -> int:
        """按 name 给一棵树注入权威 uuid 并刷新索引（驱动构造期使用）。"""

        count = set_uuids_by_name(resource, name_to_uuid_map)
        if count:
            self._index(resource)
        return count

    def parent_resource(self, resource: Any) -> Optional[Any]:
        """资源在持有树中的父节点；根或未登记的资源返回 None。"""

        parent = self._parents.get(id(resource))
        if parent is not None:
            return parent
        return getattr(resource, "parent", None)

    # ── 查找 ──────────────────────────────────────────────────

    def figure_resource(
        self,
        query_resource: Union[List[Union[dict, "PLRResource"]], dict, "PLRResource"],
        try_mode: bool = False,
    ) -> Union[
        List[Union[dict, "PLRResource", List[Union[dict, "PLRResource"]]]],
        dict,
        "PLRResource",
    ]:
        """把资源引用解析成本设备持有的实例。

        引用可以是带 ``uuid`` / ``id`` / ``name`` 的 dict，或一个 PLR 实例（按其
        ``unilabos_uuid``，否则按 ``name``；实例查询只匹配同类实例）。列表逐项解析；
        没有任何标识字段的 dict 视为「命名资源集合」，按其 values 逐项解析。

        ``try_mode=True`` 返回全部匹配（可为空）；否则要求恰好一个匹配。
        """

        if isinstance(query_resource, list):
            return [self.figure_resource(item, try_mode) for item in query_resource]
        if (
            isinstance(query_resource, dict)
            and "id" not in query_resource
            and "name" not in query_resource
            and "uuid" not in query_resource
        ):
            return [self.figure_resource(item, try_mode) for item in query_resource.values()]

        matches = self._matches(query_resource)
        if try_mode:
            return matches
        assert len(matches) > 0, f"没有找到资源 {query_resource}，请检查资源是否存在"
        assert len(matches) == 1, f"{query_resource} 找到多个资源，请检查资源是否唯一: {matches}"
        return matches[0]

    def _matches(self, query: Any) -> List[Any]:
        is_dict = isinstance(query, dict)
        required_type = object if is_dict else type(query)
        query_uuid = query.get("uuid") if is_dict else getattr(query, "unilabos_uuid", None)

        if query_uuid:
            candidate = self.uuid_to_resources.get(query_uuid)
            if candidate is not None and isinstance(candidate, required_type):
                return [candidate]
            # 索引未命中：驱动可能直接 assign 了子物料而没登记，退回遍历持有树
            return [
                node
                for node in walk_resources(self.resources)
                if resource_uuid(node) == query_uuid and isinstance(node, required_type)
            ]

        if is_dict:
            key = "id" if query.get("id") else "name"
            value = query.get(key)
        else:
            key = "id" if getattr(query, "id", None) else "name"
            value = getattr(query, key, None)
        if value is None:
            logger.warning(f"resource {query} 缺少 id、name 或 uuid，无法解析资源引用")
            return []
        if key == "name":
            indexed = [
                node for node in self._names.get(str(value), []) if isinstance(node, required_type)
            ]
            if indexed:
                return indexed
        return [
            node
            for node in walk_resources(self.resources)
            if isinstance(node, required_type)
            and (node.get(key) if isinstance(node, dict) else getattr(node, key, None)) == value
        ]
