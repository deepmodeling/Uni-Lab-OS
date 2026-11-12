"""面向设备代码的统一物料 helper。

这里集中放置 PLR 物料的高层操作：

- ``create``：向 materials authority 申请创建物料树并取回权威 UUID
  （支持 registry 资源类名直接创建；``node=`` 自动登记进设备 tracker）；
- ``ensure``：保证权威中存在与输入 uuid 一致的物料树（开机对齐 / 扣减落库）；
- ``get``：按 uuid 或 resource id 取物料树（未命中抛错）；
- ``search``：按名称精确搜索物料树（未命中返回 []）；
- ``resolve``：把单个 ResourceSlot 原始入参解析成带 uuid 的 PLR 实例；
- ``assign``：把权威已创建的物料挂载到设备的目标父物料下（append 门面）；
- ``update``：把带权威 uuid 的（部分）物料树 diff/apply 回 authority；
- ``remove``：按 uuid 或 resource id 删除权威物料树；
- ``snapshot``：把整棵已登记 PLR 根树原子 diff/apply 到 authority；
- ``transfer``：先由 authority 切换挂载关系，再同步来源与目标设备快照；
- ``apply_substances``：把液体或固体内容物写入物料或指定孔位；
- ``resolve_site_spot``：把 Site/slot 标识解析为 PLR spot；
- ``from_str`` / ``to_str``：资源树与 JSON 字符串的统一互转；
- ``parse_resource_slot``：把 ResourceSlot 的任意 wire 形态剥离成规范输入。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from uuid import UUID, uuid4

from pylabrobot.resources import ItemizedResource
from pylabrobot.resources import Resource as PLRResource

from unilabos.resources.resource_tracker import ResourceTreeInstance, ResourceTreeSet
from unilabos.resources.adapters.plr_materials import (
    CreatedPLRMaterials,
    MaterialGateway,
    create_plr_materials,
    material_tree_to_resource_tree,
    resource_tree_to_create,
)
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    ACTOR_DEVICE,
    ACTOR_EDGE,
    MaterialMove,
    MaterialTransfer,
    MaterialTransferItem,
    MaterialTreeRead,
)
from unilabos.resources.objects.resource import EXTRA_BOUND_DEVICE
from unilabos.utils.log import trace


LIQUID_UNIT = "ul"
SOLID_UNIT = "ug"
SELF_SLOT = -1

SLOT_KIND_PLR = "plr"
SLOT_KIND_REFERENCE = "reference"
SLOT_KIND_TREE = "tree"


def from_str(
    payload: Union[str, bytes, bytearray, Dict[str, Any], List[Any]],
) -> ResourceTreeSet:
    """把资源 JSON（字符串或已 loads 的对象）解析成 :class:`ResourceTreeSet`。

    自动识别三种形态：

    - 单节点对象 ``{...}``；
    - 扁平节点列表 ``[{...}, ...]``（可包含多棵树）；
    - ``ResourceTreeSet.dump()`` 的分组形态 ``[[{...}], ...]``。
    """

    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError("资源 JSON 必须是对象或非空数组")
    if all(isinstance(item, list) for item in payload):
        return ResourceTreeSet.load(payload)
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("资源节点数组必须全部是对象")
    return ResourceTreeSet.from_raw_dict_list(payload)


def to_str(
    resources: Union[ResourceTreeSet, PLRResource, Sequence[PLRResource]],
) -> str:
    """把资源树序列化成 JSON 字符串（``ResourceTreeSet.dump()`` 分组形态）。

    与 :func:`from_str` 互逆；接受 ``ResourceTreeSet``、单个 PLR 根或 PLR 根列表。
    """

    if isinstance(resources, ResourceTreeSet):
        tree_set = resources
    else:
        roots = (
            list(resources)
            if isinstance(resources, (list, tuple))
            else [resources]
        )
        tree_set = ResourceTreeSet.from_plr_resources(roots)
    return json.dumps(tree_set.dump(), ensure_ascii=False)


def parse_resource_slot(value: Any) -> Tuple[str, Any]:
    """把 ResourceSlot 的任意 wire 形态剥离成规范输入。

    返回 ``(kind, payload)``：

    - ``("plr", resource)``：已是本进程 PLR 实例，直接透传；
    - ``("reference", ref)``：``{uuid/id/name}`` 引用，由调用方决定本地匹配
      还是回权威拉取；
    - ``("tree", tree_set)``：完整资源树（:class:`ResourceTreeSet`）。

    字符串输入先 ``json.loads``（与 HostLink 原生 JSON 行为一致）；
    非 JSON 对象/数组的裸字符串按 uuid 引用处理。
    """

    if isinstance(value, PLRResource):
        return SLOT_KIND_PLR, value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("ResourceSlot 字符串不能为空")
        if stripped[0] in "[{":
            value = json.loads(stripped)
        else:
            value = {"uuid": stripped}
    if isinstance(value, dict):
        if not any(value.get(key) for key in ("uuid", "id", "name")):
            raise ValueError("ResourceSlot 引用缺少 uuid、id 或 name")
        return SLOT_KIND_REFERENCE, value
    if isinstance(value, list):
        return SLOT_KIND_TREE, from_str(value)
    raise TypeError(
        "ResourceSlot 必须是资源实例、JSON 字符串、{uuid/id/name} 引用或扁平资源树"
    )


def material_uuid(value: Any, role: str = "") -> str:
    """读取已经由 materials authority 分配的物料 UUID。

    接受 PLR 实例（``unilabos_uuid``）、资源 dict 或裸 uuid 字符串。
    """

    if isinstance(value, str):
        result: Any = value
    elif isinstance(value, dict):
        result = value.get("uuid") or value.get("unilabos_uuid")
        if not result and isinstance(value.get("data"), dict):
            result = value["data"].get("unilabos_uuid")
    else:
        result = getattr(value, "unilabos_uuid", None)
    normalized = str(result or "").strip()
    if not normalized:
        prefix = f"{role}物料 " if role else "物料 "
        raise ValueError(f"{prefix}{value!r} 缺少微后端 UUID")
    return normalized


async def transfer(
    plr_resources: Any | Sequence[Any],
    target_device_id: str,
    target_resources: Any | Sequence[Any],
    sites: Any | Sequence[Any | None] | None = None,
    *,
    source_device_id: str,
    source_device_uuid: str = "",
    mutation: InventoryMutation | None = None,
    gateway: MaterialGateway | None = None,
) -> dict[str, Any]:
    """通过微后端权威完成一次物料转移（换位与跨设备统一语义）。

    本函数只提交意图；位置持久化以及来源 unload、目标 load 的顺序全部由
    ``MaterialsService.transfer_material`` 负责。同设备换位即
    ``source_device_id == target_device_id``，编排完全一致。

    - 物料与目标父物料均接受 PLR 实例或 uuid 字符串；
    - ``sites`` 每项是目标 Site 的选择器（Site uuid / label / 数字索引），
      省略或 None 表示不指定 Site；单值自动匹配单物料。
    """

    resources = (
        list(plr_resources)
        if isinstance(plr_resources, (list, tuple))
        else [plr_resources]
    )
    targets = (
        list(target_resources)
        if isinstance(target_resources, (list, tuple))
        else [target_resources]
    )
    material_uuids = [material_uuid(value, "来源") for value in resources]
    target_uuids = [material_uuid(value, "目标") for value in targets]
    if sites is None:
        site_selectors: list[Any] = [None] * len(material_uuids)
    elif isinstance(sites, (list, tuple)):
        site_selectors = list(sites)
    else:
        site_selectors = [sites]
    if not material_uuids:
        raise ValueError("物料转移至少需要一个来源物料")
    if not (
        len(material_uuids) == len(target_uuids) == len(site_selectors)
    ):
        raise ValueError("来源物料、目标物料和 Site 数量必须一致")

    def normalize_device_id(value: str, role: str) -> str:
        normalized = str(value or "").strip()
        if normalized.startswith("/devices/"):
            normalized = normalized[len("/devices/") :]
        normalized = normalized.strip("/")
        if not normalized:
            raise ValueError(f"{role}设备 ID 不能为空")
        return normalized

    normalized_source = normalize_device_id(source_device_id, "来源")
    normalized_target = normalize_device_id(target_device_id, "目标")
    request = MaterialTransfer(
        source_device_id=normalized_source,
        target_device_id=normalized_target,
        items=[
            MaterialTransferItem(
                material_uuid=material_id,
                target_material_uuid=target_id,
                target_site=(
                    None
                    if site is None or str(site).strip() == ""
                    else site
                ),
            )
            for material_id, target_id, site in zip(
                material_uuids,
                target_uuids,
                site_selectors,
            )
        ],
    )
    command_uuid = str(uuid4())
    transfer_mutation = mutation or InventoryMutation(
        command_uuid=command_uuid,
        effect_key=f"transfer_material:{command_uuid}",
        operation="transfer_material",
        actor_type="device",
        actor_uuid=str(source_device_uuid or normalized_source),
    )
    result = await asyncio.to_thread(
        (gateway or resolve_materials_gateway()).transfer_material,
        transfer_mutation,
        request,
    )
    return {
        "success": True,
        "command_uuid": result.command_uuid,
        "replayed": result.replayed,
        "material_uuids": result.data.material_uuids,
        "source_device_id": normalized_source,
        "target_device_id": normalized_target,
        "target_resources_uuid": result.data.target_material_uuids,
        "moves": [
            value.model_dump(mode="json", exclude_none=False)
            for value in result.data.materials
        ],
    }


def set_substance_on_target(
    target: Any,
    name: str,
    amount: float,
    is_solid: bool = False,
) -> Any:
    """把单个内容物写到目标容器或孔位。"""

    unit = SOLID_UNIT if is_solid else LIQUID_UNIT
    target_name = getattr(target, "name", target)
    substances = [(name, amount, unit)]
    if hasattr(target, "set_liquids"):
        target.set_liquids(substances)
    elif hasattr(getattr(target, "tracker", None), "set_liquids"):
        target.tracker.set_liquids(substances)
    else:
        raise ValueError(
            f"目标 {target_name} 不是容器，无法设置内容物（请检查 slots 是否指向子孔位）"
        )
    trace(
        f"[set_substance] {target_name} <- {'固体' if is_solid else '液体'} "
        f"{name}={amount}{unit}"
    )
    return target


def resolve_substance_targets(
    material: Any,
    slots: Optional[Sequence[Any]],
) -> List[Any]:
    """把物料和 slot 标识解析为实际的内容物写入目标。"""

    if not slots or list(slots) == [SELF_SLOT]:
        return [material]

    targets: List[Any] = []
    for slot in slots:
        child = None
        is_index = isinstance(slot, int) or (
            isinstance(slot, str) and slot.isdigit()
        )

        if isinstance(material, ItemizedResource):
            try:
                child = material.get_item(
                    int(slot)
                    if isinstance(slot, str) and slot.isdigit()
                    else slot
                )
            except Exception:
                child = None

        if child is None:
            try:
                child = material[
                    int(slot)
                    if isinstance(slot, str) and slot.isdigit()
                    else slot
                ]
            except Exception:
                child = None
        if child is None and is_index:
            try:
                child = material.children[int(slot)]
            except Exception:
                child = None
        if child is None:
            for candidate in getattr(material, "children", []):
                if candidate.name == slot or (
                    isinstance(slot, str)
                    and candidate.name.endswith(f"_{slot}")
                ):
                    child = candidate
                    break

        if child is None:
            raise ValueError(
                f"无法在物料 {getattr(material, 'name', material)} 中定位子孔位 {slot}"
            )
        targets.append(child)
    return targets


def resolve_site_spot(parent: Any, site: Any) -> Optional[int]:
    """把 slot 标识（字符串，如 "A1"/"0"）解析成父级 ``_ordering`` 上的
    spot 索引。优先按 label 匹配；非 label 且 isdigit 时按 0-based 索引。
    内部调用宽容 int（等价于数字字符串）。"""

    if site is None or (isinstance(site, str) and not site):
        return None
    if isinstance(site, int):
        return site
    ordering = getattr(parent, "_ordering", None)
    keys = list(ordering.keys()) if ordering else []
    if site in keys:
        return keys.index(site)
    if isinstance(site, str) and site.isdigit():
        return int(site)
    try:
        target = resolve_substance_targets(parent, [site])[0]
        target_name = getattr(target, "name", None)
        for index, key in enumerate(keys):
            if target_name and (
                target_name == key or target_name.endswith(f"_{key}")
            ):
                return index
    except Exception:
        pass
    return None


def apply_substances(
    material: Any,
    names: Sequence[str],
    amounts: Sequence[float],
    slots: Optional[Sequence[Any]] = None,
    is_solid: Optional[Sequence[bool]] = None,
    broadcast: bool = False,
) -> List[Any]:
    """把一批液体或固体写入物料自身或指定子孔位。"""

    targets = resolve_substance_targets(material, slots)
    normalized_names = list(names)
    normalized_amounts = list(amounts)

    if (
        broadcast
        and len(normalized_names) == 1
        and len(normalized_amounts) == 1
        and len(targets) > 1
    ):
        normalized_names *= len(targets)
        normalized_amounts *= len(targets)

    if not (
        len(targets) == len(normalized_names) == len(normalized_amounts)
    ):
        raise ValueError(
            "增加内容物入参长度不一致："
            f"targets={len(targets)} names={len(normalized_names)} "
            f"amounts={len(normalized_amounts)}"
        )

    solid_flags = list(is_solid or [])
    for index, (target, name, amount) in enumerate(
        zip(targets, normalized_names, normalized_amounts)
    ):
        set_substance_on_target(
            target,
            name,
            amount,
            solid_flags[index] if index < len(solid_flags) else False,
        )
    return targets


def resolve_materials_gateway() -> MaterialGateway:
    """按进程角色选择链路；Slave 永远经 HostLink，不直连 HTTP。

    Host 侧的链路选择只发生在启动装配处（``setup_host_server_stack``：给了外部
    地址走 HTTP client，否则用进程内嵌的 LocalMaterialsClient），这里只取它公布
    的唯一 gateway。取不到即微后端未装配，属致命配置错误。
    """

    from unilabos.config.config import BasicConfig

    if not BasicConfig.is_host_mode:
        from unilabos.backend.hostlink.client import get_hostlink_client
        from unilabos.client.materials import HostLinkMaterialsClient

        client = get_hostlink_client()
        if client is None:
            raise RuntimeError("Slave 尚未连接 HostLink，无法访问物料权威")
        return HostLinkMaterialsClient(client)

    from unilabos.server.backend.composition import get_materials_gateway

    gateway = get_materials_gateway()
    if gateway is None:
        raise RuntimeError("Host 尚未装配 materials authority（微后端未启动）")
    return gateway


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _material_tree_set(
    gateway: MaterialGateway,
    root_material_uuid: str,
    with_children: bool,
) -> ResourceTreeSet:
    tree_set = material_tree_to_resource_tree(gateway.get_tree(root_material_uuid))
    if not with_children:
        for tree in tree_set.trees:
            tree.root_node.children = []
    return tree_set


def get(
    refs: Union[str, Sequence[str]],
    *,
    with_children: bool = True,
    gateway: MaterialGateway | None = None,
) -> ResourceTreeSet:
    """按 uuid 或 resource id（dir）向权威取物料树；任一 ref 未命中直接抛错。

    ref 是 UUID 格式按 uuid 查，否则按 resource id 查；两种引用可混用。
    """

    gw = gateway or resolve_materials_gateway()
    result = ResourceTreeSet([])
    for ref in [refs] if isinstance(refs, str) else list(refs):
        text = str(ref or "").strip()
        if not text:
            raise ValueError("materials.get 的 ref 不能为空")
        resolved = (
            text
            if _looks_like_uuid(text)
            else gw.get_material_by_resource_id(text).material.material_uuid
        )
        result.trees.extend(_material_tree_set(gw, resolved, with_children).trees)
    return result


def owner_device_of(
    value: Any,
    *,
    gateway: MaterialGateway | None = None,
) -> str:
    """沿权威 parent 链找到最近的设备祖先，取其全局 resource_id 作为路由身份。

    已挂在设备下的树以设备身份为准（包括工作站的子设备），不依赖额外重复标记。
    尚未并入设备树的独立根仍由两个写点显式登记归属：

    - ``materials.create(..., node=)``：本地创建即登记（syncer / 驱动创建）；
    - ``DeviceNode.append_resource`` 挂载到设备自身（根树上台面）时登记。

    物料转移只是并入目标设备的根树，归属随新根自动继承，无需额外维护。
    host 物料动作以此自动推断来源/目标设备，调用方不必再显式传 device_id。
    """

    gw = gateway or resolve_materials_gateway()
    uuid_ = material_uuid(value, "归属推断")
    current = gw.get_material(uuid_)
    seen = {uuid_}
    while True:
        identity = current.material
        if identity.resource_type == "device":
            # 设备图落库本来就有全局 ID。不能继续爬到工作站根，把子设备的物料误送给父设备；
            # 也不能优先用物料自身的旧绑定，跨设备 transfer 后应跟随新的权威父链。
            return str(identity.resource_id)
        if not identity.parent_material_uuid:
            break
        parent_uuid = str(identity.parent_material_uuid)
        if parent_uuid in seen:
            raise ValueError(f"物料 {uuid_} 的权威 parent 链成环: {parent_uuid}")
        seen.add(parent_uuid)
        current = gw.get_material(parent_uuid)
    root = current.material
    device_id = str((root.extra or {}).get(EXTRA_BOUND_DEVICE) or "").strip()
    if not device_id:
        raise ValueError(
            f"物料 {uuid_} 所在根树 {root.material_uuid}（{root.name}）未登记所属设备；"
            "根树须经 materials.create(node=) 或挂载到设备（append_resource）后才可推断"
        )
    return device_id


def search(
    name: str,
    *,
    with_children: bool = True,
    gateway: MaterialGateway | None = None,
) -> List[ResourceTreeInstance]:
    """按名称精确搜索权威物料；未命中返回 []（get 系未命中会抛错）。"""

    gw = gateway or resolve_materials_gateway()
    trees: List[ResourceTreeInstance] = []
    for aggregate in gw.search_materials(str(name)):
        trees.extend(
            _material_tree_set(
                gw, aggregate.material.material_uuid, with_children
            ).trees
        )
    return trees


def resolve(
    value: Any,
    *,
    gateway: MaterialGateway | None = None,
) -> Any | None:
    """把单个 ResourceSlot 原始入参解析成带权威 uuid 的 PLR 实例。

    编排层（host 动作 / 脚本）的统一收口：

    - ``None`` / 空字符串：返回 None；
    - PLR 实例：透传；
    - 扁平树（handle @flatten 输出）：装配成单根 PLR；
    - ``{uuid/id/name}`` 引用：uuid/id 走 :func:`get`，name 走 :func:`search`。

    解析结果必须恰好是一棵资源树，否则抛错。
    """

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    kind, payload = parse_resource_slot(value)
    if kind == SLOT_KIND_PLR:
        return payload
    if kind == SLOT_KIND_REFERENCE:
        ref = payload.get("uuid") or payload.get("id")
        if ref:
            tree_set = get(str(ref), gateway=gateway)
        else:
            trees = search(str(payload.get("name")), gateway=gateway)
            if not trees:
                raise ValueError(f"权威中找不到物料: {payload!r}")
            if len(trees) > 1:
                raise ValueError(f"物料名称匹配到多个权威物料: {payload!r}")
            tree_set = ResourceTreeSet(trees)
    else:
        tree_set = payload
    resources = tree_set.to_plr_resources()
    if len(resources) != 1:
        raise ValueError(
            f"单个 ResourceSlot 必须恰好包含一棵资源树，实际 {len(resources)} 棵"
        )
    return resources[0]


def instantiate_registry_resource(class_name: str, name: str) -> PLRResource:
    """按 registry 资源类名实例化本地 PLR 草稿（无 uuid，发给权威创建前的载体）。

    ``materials.create("类名", name=...)`` 与微后端库存命令 ``instantiate_material``
    （``POST /api/v1/inventory/commands``）共用此入口；随后的权威登记同样走
    create_tree，两条路径语义一致。需要 dict 形态时交给
    ``ResourceTreeSet.from_plr_resources``。只有 ``class.type == "pylabrobot"`` 的
    registry 资源类可实例化。
    """

    if not str(name or "").strip():
        raise ValueError("按资源类名创建物料时必须提供 name")
    import importlib

    from unilabos.registry.registry import lab_registry

    entry = lab_registry.resource_type_registry.get(class_name)
    class_info = entry.get("class") if isinstance(entry, dict) else None
    if not isinstance(class_info, dict) or class_info.get("type") != "pylabrobot":
        raise ValueError(f"registry 资源类不可实例化: {class_name!r}")
    module_name, _, attribute = str(class_info.get("module") or "").partition(":")
    resource_class = getattr(importlib.import_module(module_name), attribute)
    draft = resource_class(name=str(name))
    if not isinstance(draft, PLRResource):
        raise ValueError(f"registry 资源类不可实例化: {class_name!r}")
    return draft


def create(
    plr_resource: Any | Sequence[Any] | str,
    *,
    name: str = "",
    node: Any | None = None,
    unwrap_single: bool = True,
    mutation: InventoryMutation | None = None,
    gateway: MaterialGateway | None = None,
    actor_type: str | None = None,
    actor_uuid: str | None = None,
) -> Any | CreatedPLRMaterials:
    """创建一棵 PLR 物料树，不修改输入。

    两种输入形态：

    - 本地 PLR 草稿实例（不带 uuid），发给权威发号并返回权威实例；
    - registry 资源类名（str，配合 ``name=``），先按注册表实例化草稿再创建，
      等价于"让后端按类目录创建一个回来"。

    传入 ``node=``（DeviceNode）时，创建成功的权威实例自动登记进
    ``node.resource_tracker``（快照观察者随即开始监听），无需手动 add；
    同时根节点 extra 登记所属设备（``unilabos_bound_device_id``），供
    :func:`owner_device_of` 自动推断来源/目标设备。

    默认直接返回唯一的权威 PLR 根对象。需要同时读取微后端回执和
    ``ResourceTreeSet`` 时，传入 ``unwrap_single=False`` 获取
    :class:`CreatedPLRMaterials`。

    一次 create 只允许一个根；根的所有 children 仍属于同一棵创建树。

    变更来源（账本 ``actor_type`` / ``actor_uuid``，前端据此渲染来源 tag）：
    未显式给出时，带 ``node=`` 视为设备创建（``device`` + 设备 id），否则
    落 ``edge`` 兜底；传入 ``mutation=`` 时以其自带的 actor 为准。
    """

    if isinstance(plr_resource, str):
        plr_resource = instantiate_registry_resource(plr_resource, name)
    resources = (
        list(plr_resource)
        if isinstance(plr_resource, (list, tuple))
        else [plr_resource]
    )
    if not resources or resources == [None]:
        raise ValueError("创建物料时至少需要一个 PLR resource")
    if len(resources) != 1:
        raise ValueError(
            "materials.create 一次只能创建一个 PLR 根物料；"
            f"实际收到 {len(resources)} 个根"
        )
    command_uuid = str(uuid4())
    if mutation is None:
        bound_device_id = (
            str(node.device_id).split("/")[-1] if node is not None else None
        )
        resolved_actor_type = actor_type or (
            ACTOR_DEVICE if node is not None else ACTOR_EDGE
        )
        resolved_actor_uuid = actor_uuid or (
            str(getattr(node, "resource_uuid", "") or "") or bound_device_id
            if node is not None
            else None
        )
        mutation = InventoryMutation(
            command_uuid=command_uuid,
            effect_key="create_material_tree",
            operation="create_material_tree",
            actor_type=resolved_actor_type,
            actor_uuid=resolved_actor_uuid or None,
        )
    request = mutation
    created = create_plr_materials(
        gateway or resolve_materials_gateway(),
        request,
        resources,
        root_extra=(
            {EXTRA_BOUND_DEVICE: str(node.device_id).split("/")[-1]}
            if node is not None
            else None
        ),
    )
    if node is not None:
        for resource in created.resources:
            node.resource_tracker.add_resource(resource)
    if not unwrap_single:
        return created
    if len(created.resources) != 1:
        raise ValueError(
            "微后端 create 必须返回恰好一个 PLR 根物料；"
            f"实际返回 {len(created.resources)} 个"
        )
    return created.resources[0]


def assign(
    node: Any,
    resources: Any | Sequence[Any],
    *,
    parent: Any | None = None,
    slot: Any | None = None,
    site: str | None = None,
    location: Optional[Dict[str, float]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """把权威已创建的物料挂载到本设备的目标父物料下（append 门面）。

    与 Host 下发 RESOURCE_APPEND 完全同语义：内部即
    ``node.append_resource``（按 uuid 拉取/复用实例 -> 物理 assign ->
    权威 move 落父子与 Site 占用 -> 快照回写），在节点自己的执行器上
    执行并阻塞等待完成。

    - ``resources``：PLR 实例（带权威 uuid）或 uuid 字符串，单个或列表；
    - ``parent``：目标父物料（实例或名字）；None 表示挂到设备自身
      （只登记进 tracker，不做 assign）；
    - ``slot``：label 或 0-based 数字字符串（如 ``"A1"`` / ``"0"``）；
    - ``site``：权威 ``ResourceSite`` uuid（与 slot 二选一，机器路径直传）。

    本函数为阻塞式，供驱动线程 / 脚本调用；已在节点执行器内的异步代码
    请直接 ``await node.append_resource(payload)``。
    """

    from unilabos.backend.runtime.async_utils import run_node_coroutine

    items = resources if isinstance(resources, (list, tuple)) else [resources]
    resource_uuids = [material_uuid(item) for item in items]
    if parent is None:
        bind_parent_id = str(node.device_id)
    else:
        bind_parent_id = str(getattr(parent, "name", parent))
    other: Dict[str, Any] = {}
    if slot is not None and str(slot).strip() != "":
        other["slot"] = str(slot)
    if site:
        other["site"] = str(site)
    payload: Dict[str, Any] = {
        "device_id": str(node.device_id),
        "resource_uuid": resource_uuids,
        "bind_parent_id": bind_parent_id,
        "bind_location": dict(location or {"x": 0.0, "y": 0.0, "z": 0.0}),
    }
    if other:
        payload["other_calling_param"] = other
    return run_node_coroutine(node, node.append_resource(payload), timeout)


async def snapshot(
    plr_root_resource: Any,
    *,
    source_device_id: str,
    source_device_uuid: str = "",
    gateway: MaterialGateway | None = None,
) -> Any:
    """提交一棵完整 PLR 根树，所有后代会在同一 snapshot 中做 diff。

    这是显式工具入口；设备运行时通常由 ``MaterialSnapshotObserver`` 在任意
    child 的 state/assign/unassign 变化后自动调用同一严格服务入口。
    """

    from unilabos.backend.runtime.resource import AuthorityResourceService

    service = AuthorityResourceService(
        gateway or resolve_materials_gateway()
    )
    return await service.snapshot_resource_tree(
        str(source_device_id),
        str(source_device_uuid),
        plr_root_resource,
    )


def _as_tree_set(
    resources: Union[ResourceTreeSet, PLRResource, Sequence[PLRResource]],
) -> ResourceTreeSet:
    if isinstance(resources, ResourceTreeSet):
        return resources
    items = (
        list(resources) if isinstance(resources, (list, tuple)) else [resources]
    )
    return ResourceTreeSet.from_plr_resources(items)


def ensure(
    resources: Union[ResourceTreeSet, PLRResource, Sequence[PLRResource]],
    *,
    gateway: MaterialGateway | None = None,
    actor_type: str = ACTOR_EDGE,
    actor_uuid: str | None = None,
    job_uuid: str | None = None,
) -> ResourceTreeSet:
    """确保权威中存在与输入 uuid「一模一样」的物料树，返回权威形态。

    逐棵树按根 uuid 询问权威：不存在则以原 uuid 显式创建（带条件的 create，
    adopt 语义）；已存在则以权威树为准，只把图里有、权威该树下没有的子节点
    （图中新挂到既有设备 / 台面上的物料）补建并挂到图声明的父节点与位点上。
    已在权威别处的物料（运行期被移走）不动。host / slave 开机图物料对齐、出库
    扣减产物落库共用此入口；Slave 侧 gateway 自动经 HostLink。

    图里尚未实例化 Site 的设备节点（Graph Authority 不可达时的原始启动文件），
    创建请求补上注册表 ``available_sites`` 定义，由权威发放 Site uuid：设备一落库
    就带齐位点，而不是留下一行以后补不了 Site 的设备。

    ``actor_type`` / ``actor_uuid`` 落账本变更来源（前端来源 tag）：开机图对齐
    传 ``graph`` + 图 uuid，云端同步传 ``backend``，工作流出库传 ``workflow`` +
    ``job_uuid``；不传则为 ``edge`` 兜底。
    """

    from unilabos.resources.adapters.device_site import (
        fill_device_site_creates,
        registry_device_site_templates,
    )

    gw = gateway or resolve_materials_gateway()
    tree_set = _as_tree_set(resources)
    result = ResourceTreeSet([])
    device_site_templates: Dict[str, Any] | None = None

    def mutation(operation: str, target_uuid: str) -> InventoryMutation:
        command_uuid = str(uuid4())
        return InventoryMutation(
            command_uuid=command_uuid,
            effect_key=f"ensure_{operation}:{target_uuid}:{command_uuid}",
            operation=operation,
            actor_type=actor_type,
            actor_uuid=actor_uuid or None,
            job_uuid=job_uuid or None,
        )

    def create(subtree: ResourceTreeInstance) -> MaterialTreeRead:
        nonlocal device_site_templates
        request = resource_tree_to_create(ResourceTreeSet([subtree]), adopt_uuid=True)
        if any(node.identity.resource_type == "device" for node in request.nodes):
            if device_site_templates is None:
                device_site_templates = registry_device_site_templates()
            fill_device_site_creates(request, device_site_templates)
        root_uuid = str(subtree.root_node.res_content.uuid)
        return gw.create_tree(mutation("create_material_tree", root_uuid), request).data

    for tree in tree_set.trees:
        root_uuid = str(tree.root_node.res_content.uuid or "").strip()
        if not root_uuid:
            raise ValueError("materials.ensure 要求每棵树的根节点都带 uuid")
        try:
            gw.get_material(root_uuid)
            exists = True
        except Exception:
            exists = False
        if not exists:
            result.trees.extend(material_tree_to_resource_tree(create(tree)).trees)
            continue
        authority = _material_tree_set(gw, root_uuid, True)
        if _ensure_missing_descendants(gw, tree, authority, create, mutation):
            authority = _material_tree_set(gw, root_uuid, True)
        result.trees.extend(authority.trees)
    return result


def _ensure_missing_descendants(
    gw: MaterialGateway,
    local: ResourceTreeInstance,
    authority: ResourceTreeSet,
    create: Any,
    mutation: Any,
) -> bool:
    """把图中有、权威该树下没有的子树补建并挂到父节点/位点上；返回是否有变更。

    父节点优先遍历：一个缺失节点连同其整个子树一次创建（adopt uuid），随后 move 到
    图声明的父节点；父节点 Site 快照里标为被它占用的位点，按 label 映射到权威 Site。
    uuid 已在权威别处（运行期被移到其它设备 / 台面）的物料不重建也不挪回。
    """

    known: dict[str, Any] = {
        node.res_content.uuid: node.res_content for node in authority.all_nodes
    }
    changed = False
    for node in local.get_all_nodes():
        resource = node.res_content
        uuid = str(resource.uuid or "")
        parent_uuid = str(resource.uuid_parent or "")
        if uuid in known or parent_uuid not in known:
            # 父节点也缺失时，它已随祖先子树一起创建（否则本节点也不该在这里出现）
            continue
        try:
            gw.get_material(uuid)
            trace(
                f"[materials.ensure] 图中 {resource.id}({uuid}) 已在权威别处，"
                f"以权威为准不挪回 {parent_uuid}"
            )
            continue
        except Exception:
            pass
        created = material_tree_to_resource_tree(create(ResourceTreeInstance(node)))
        known.update(
            (item.res_content.uuid, item.res_content) for item in created.all_nodes
        )
        site_uuid = _authority_site_occupied_by(
            resource.parent, known[parent_uuid], uuid
        )
        gw.move_material(
            mutation("move_material", uuid),
            MaterialMove(
                material_uuid=uuid,
                parent_material_uuid=parent_uuid,
                destination_site_uuid=site_uuid,
            ),
        )
        trace(
            f"[materials.ensure] 图中新增物料 {resource.id}({uuid}) 已建并挂到 "
            f"{parent_uuid}" + (f" 位点 {site_uuid}" if site_uuid else "")
        )
        changed = True
    return changed


def _authority_site_occupied_by(
    local_parent: Any, authority_parent: Any, occupant_uuid: str
) -> Optional[str]:
    """图中父节点 Site 快照里被 occupant 占用的位点，按 label 映射成权威 Site uuid。"""

    for site in getattr(local_parent, "sites", None) or []:
        if site.occupied_material_uuid != occupant_uuid:
            continue
        label = str(site.label).casefold()
        for candidate in getattr(authority_parent, "sites", None) or []:
            if str(candidate.label).casefold() == label:
                return str(candidate.uuid)
        raise ValueError(
            f"图中 {occupant_uuid} 占用父节点位点 {site.label}，但权威父节点没有该位点"
        )
    return None


def update(
    *items: Any,
    source_device_id: str = "",
    source_device_uuid: str = "",
    gateway: MaterialGateway | None = None,
) -> ResourceTreeSet:
    """把已带权威 uuid 的物料 diff/apply 回权威——update 的唯一汇聚点。

    两种调用形态：

    - ``materials.update(node, *物料)``：设备上下文。身份与网关取自 node
      绑定的 ResourceService（Slave 自动经 HostLink）；
      ``node.update_resource`` 即本形态的 async 包装，驱动的同步代码
      可直接调用本函数。
    - ``materials.update(*物料, source_device_id=...)``：脱离设备的脚本，
      走全局网关。

    物料直接传：单个 PLR 实例、多个实例（多参）、实例列表或
    ``ResourceTreeSet`` 均可，自动展平；重复节点在服务层按 uuid 去重。
    返回更新后的权威树。
    """

    from unilabos.backend.runtime.node import DeviceNode
    from unilabos.backend.runtime.resource import AuthorityResourceService

    node = items[0] if items and isinstance(items[0], DeviceNode) else None
    payload_items = items[1:] if node is not None else items
    if not payload_items:
        raise ValueError("materials.update 至少需要一个物料")
    if len(payload_items) == 1:
        payload: Any = payload_items[0]
    else:
        payload = []
        for item in payload_items:
            if isinstance(item, (list, tuple)):
                payload.extend(item)
            else:
                payload.append(item)
    if node is not None:
        return node._require_resource_service().update_resources_sync(
            str(node.device_id), str(node.resource_uuid), payload
        )
    service = AuthorityResourceService(gateway or resolve_materials_gateway())
    return service.update_resources_sync(
        str(source_device_id), str(source_device_uuid), payload
    )


def remove(
    refs: Union[str, Sequence[str]],
    *,
    source_device_id: str = "",
    source_device_uuid: str = "",
    gateway: MaterialGateway | None = None,
) -> List[str]:
    """按 uuid 或 resource id 删除权威物料树，返回实际删除的根 uuid。"""

    from unilabos.backend.runtime.resource import AuthorityResourceService

    gw = gateway or resolve_materials_gateway()
    uuids: List[str] = []
    for ref in [refs] if isinstance(refs, str) else list(refs):
        text = str(ref or "").strip()
        if not text:
            raise ValueError("materials.remove 的 ref 不能为空")
        uuids.append(
            text
            if _looks_like_uuid(text)
            else gw.get_material_by_resource_id(text).material.material_uuid
        )
    service = AuthorityResourceService(gw)
    return service.delete_resources_sync(
        str(source_device_id), str(source_device_uuid), uuids
    )


__all__ = [
    "LIQUID_UNIT",
    "SELF_SLOT",
    "SLOT_KIND_PLR",
    "SLOT_KIND_REFERENCE",
    "SLOT_KIND_TREE",
    "SOLID_UNIT",
    "apply_substances",
    "assign",
    "create",
    "ensure",
    "from_str",
    "get",
    "instantiate_registry_resource",
    "material_uuid",
    "owner_device_of",
    "remove",
    "resolve",
    "search",
    "parse_resource_slot",
    "resolve_site_spot",
    "resolve_materials_gateway",
    "resolve_substance_targets",
    "set_substance_on_target",
    "snapshot",
    "to_str",
    "transfer",
    "update",
]
