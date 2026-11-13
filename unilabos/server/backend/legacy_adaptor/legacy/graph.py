"""旧后端 / 旧示例图形状 → 当前 node-link 契约的入站转换。

Edge 与微后端之间只讲当前契约（``template_name``、``pose.position`` /
``pose.position3d``、Site ``occupied_material_uuid``、边 ``source`` / ``target`` +
``sourceHandle`` / ``targetHandle``）。旧云端 Backend 导出的图（``/edge/material/
download``、``/edge/material/query``）以及 dev 时代的示例图长着另一副样子：

- 只写 ``class`` 不写 ``template_name``；
- 根级 ``position``（与 ``pose.position`` 同义）、``pose.position_3d``；
- 派生的 ``children`` id 列表；
- PLR 平铺 Site 用占用物料 **name** 的 ``occupied_by`` 表达占用；
- 旧 PLR ``serialize_state`` 液体字段（``liquid_history`` 名字列表、``["", null]``
  空液体、``{"liquid_type", "liquid_volume"}`` 对象、``pending_liquids``），以及 dev
  时代容器的 ``data.liquid=[{"liquid_type", "liquid_volume"}]``；
- 边只有 ``source_uuid`` / ``target_uuid`` 与下划线 ``source_handle`` /
  ``target_handle``，顶层键叫 ``edges``。

这些形状只在**接收旧数据的边界**被识别并一次性转换：``-g`` 启动文件与
``unilab graph upload`` 的读取入口（:func:`upgrade_legacy_graph_payload`）、旧后端
物料通知的拉取（:func:`normalize_legacy_material_nodes`）。Graph Authority
（含其 ``load_graph_payload`` 运行时装配）、物料权威只接受当前契约，不认识这里列出的任何旧字段；剥离旧后端支持
时删除本模块及其调用点即可。
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()

#: 旧后端 / 旧图在 ``pose`` 里使用的向量字段别名。
_LEGACY_POSE_VECTOR_ALIASES = {"position_3d": "position3d", "position3D": "position3d"}
#: 旧边的 handle 字段名 → 当前契约。
_LEGACY_LINK_HANDLE_ALIASES = {
    "source_handle": "sourceHandle",
    "target_handle": "targetHandle",
}


def _same_vector(left: Any, right: Any) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return left == right
    try:
        return all(
            float(left.get(axis, 0.0) or 0.0) == float(right.get(axis, 0.0) or 0.0)
            for axis in ("x", "y", "z")
        )
    except (TypeError, ValueError):
        return False


def _is_legacy_liquid_entry(entry: Any) -> bool:
    if isinstance(entry, Mapping):
        return True
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        return False
    name, amount = entry[0], entry[1]
    return amount is None or (isinstance(name, str) and not name.strip())


def _has_dev_era_liquid(node: Mapping[str, Any]) -> bool:
    """节点是否只有 dev 时代的 ``data.liquid`` 内容物、还没有当前契约的内容物字段。"""

    data = node.get("data")
    if not isinstance(data, Mapping):
        return False
    entries = data.get("liquid")
    if not isinstance(entries, list) or not any(
        isinstance(entry, Mapping) and "liquid_type" in entry for entry in entries
    ):
        return False
    return not any(
        scope.get(key) is not None
        for scope in (node, data)
        for key in ("liquids", "substances")
    )


def _normalize_legacy_tracker_state(node: Dict[str, Any], label: Any) -> None:
    """旧 PLR ``serialize_state`` 输出的液体字段 → 当前 tracker 根字段形状。

    - ``liquid_history``：旧版只记液体名（``["water"]``），当前是
      ``(name, delta, unit)`` 三元组——旧值无法还原增量，直接丢弃；
    - ``liquids`` / ``substances``：``["", null]`` 表示空容器，过滤掉空名/空量
      条目；二元组补默认单位 ``ul``；
    - ``pending_liquids``：旧 tracker 的暂存字段，当前模型无对应语义，丢弃；
    - ``data.liquid``：dev 时代容器的 ``[{"liquid_type", "liquid_volume"}]``。节点还没有
      当前契约的内容物（``liquids`` / ``substances``）时转成 ``data.liquids``（随后按上面
      的规则清洗、补单位，再由 ``ResourceDict`` 提升为 ``substances``）。``data.liquid``
      本身原样保留：内置协议编译器仍从物理拓扑图的节点 data 读取它。
    """

    data = node.get("data")
    if not isinstance(data, dict):
        return
    data.pop("pending_liquids", None)
    if _has_dev_era_liquid(node):
        data["liquids"] = copy.deepcopy(data["liquid"])
    for scope in (node, data):
        history = scope.get("liquid_history")
        if isinstance(history, list) and any(
            not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3)
            for entry in history
        ):
            scope.pop("liquid_history", None)
        for key in ("liquids", "substances"):
            entries = scope.get(key)
            if not isinstance(entries, list):
                continue
            cleaned = []
            for entry in entries:
                if isinstance(entry, Mapping):
                    # 更早的前端把液体写成 {"liquid_type", "liquid_volume"} 对象。
                    entry = (
                        entry.get("liquid_type", entry.get("name")),
                        entry.get("liquid_volume", entry.get("amount", entry.get("volume"))),
                        entry.get("unit", "ul"),
                    )
                if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
                    raise ValueError(
                        f"资源 {label} 的 {key} 条目必须是 (name, amount[, unit])"
                    )
                name, amount = entry[0], entry[1]
                if amount is None or (isinstance(name, str) and not name.strip()):
                    continue
                # 保持 JSON 数组形态：该节点还会原样落进 Graph Authority payload。
                cleaned.append([name, amount, entry[2] if len(entry) == 3 else "ul"])
            if cleaned:
                scope[key] = cleaned
            else:
                scope.pop(key, None)


def _site_containers(node: Mapping[str, Any]) -> List[List[Any]]:
    """节点根级与 ``config`` 里的 Site 列表（PLR 平铺图纸两处都可能写）。"""

    found: List[List[Any]] = []
    config = node.get("config")
    for container in (node, config if isinstance(config, Mapping) else None):
        if isinstance(container, Mapping) and isinstance(container.get("sites"), list):
            found.append(container["sites"])
    return found


def _normalize_legacy_sites(
    node: Dict[str, Any],
    children_by_parent: Optional[Mapping[str, Mapping[str, str]]],
) -> None:
    """旧 PLR 平铺 Site 的 ``occupied_by``（占用物料 **name**）→ ``occupied_material_uuid``。

    旧图的 ``occupied_by`` 经常是陈旧值（物料早已转移到别处但 Site 没清），
    所以只接受**当前确实挂在该节点下**的同名子物料（``children_by_parent``：
    owner uuid / id → {child name/id: child uuid}）；其它情形一律移到
    ``meta_data.legacy_fields`` 供排查，不阻断加载。未提供映射时同样归档。
    """

    children: Dict[str, str] = {}
    if children_by_parent:
        for owner_key in (node.get("uuid"), node.get("id"), node.get("name")):
            if owner_key and str(owner_key) in children_by_parent:
                children = dict(children_by_parent[str(owner_key)])
                break
    for sites in _site_containers(node):
        for site in sites:
            if not isinstance(site, dict) or "occupied_by" not in site:
                continue
            occupied_by = site.pop("occupied_by")
            if occupied_by in (None, ""):
                continue
            resolved = children.get(str(occupied_by))
            if resolved and not site.get("occupied_material_uuid"):
                site["occupied_material_uuid"] = resolved
            elif not resolved:
                metadata = site.get("meta_data")
                if not isinstance(metadata, dict):
                    metadata = {}
                legacy = metadata.get("legacy_fields")
                metadata["legacy_fields"] = {
                    **(legacy if isinstance(legacy, dict) else {}),
                    "occupied_by": occupied_by,
                }
                site["meta_data"] = metadata


def legacy_graph_children_index(
    nodes: Iterable[Mapping[str, Any]],
) -> Dict[str, Dict[str, str]]:
    """构建 owner（uuid 与 id 各一条）→ {child name/id → child uuid}，供 ``occupied_by`` 解析。

    父子关系优先看 ``parent_uuid``，缺失时按 ``parent``（id）经节点 id 反查。
    没有 uuid 的子节点无法成为占用者（占用关系以 uuid 表达）。
    """

    nodes = list(nodes)
    id_to_uuid: Dict[str, str] = {}
    for node in nodes:
        node_uuid = str(node.get("uuid") or "")
        if not node_uuid:
            continue
        for key in ("id", "name"):
            value = node.get(key)
            if value:
                id_to_uuid.setdefault(str(value), node_uuid)
    index: Dict[str, Dict[str, str]] = {}
    for node in nodes:
        node_uuid = str(node.get("uuid") or "")
        if not node_uuid:
            continue
        parent_uuid = str(node.get("parent_uuid") or "")
        parent_id = str(node.get("parent") or "")
        if not parent_uuid:
            parent_uuid = id_to_uuid.get(parent_id, "")
        owner_keys = {key for key in (parent_uuid, parent_id) if key}
        for owner_key in owner_keys:
            bucket = index.setdefault(owner_key, {})
            for key in ("id", "name"):
                value = node.get(key)
                if value:
                    bucket.setdefault(str(value), node_uuid)
    return index


def normalize_legacy_graph_node(
    node: Dict[str, Any],
    *,
    children_by_parent: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    """把一个旧形状节点就地转成当前契约，返回同一个 ``node``。

    - ``template_name`` 缺失时取旧图的 ``class``；
    - 派生的 ``children`` 列表丢弃（层级只由 ``parent`` / ``parent_uuid`` 表达）；
    - 根级 ``position`` 折叠进 ``pose.position``，两者同时存在且不一致时拒绝；
    - ``pose.position_3d`` → ``pose.position3d``；
    - 旧 PLR 液体字段 → 当前 tracker 根字段形状；
    - 平铺 Site 的 ``occupied_by`` → ``occupied_material_uuid``
      （需传 ``children_by_parent``，见 :func:`legacy_graph_children_index`）。

    绝不覆盖已有的新字段；对已是当前契约的节点是恒等变换。
    """

    if not str(node.get("template_name") or "").strip():
        legacy_class = str(node.get("class") or "").strip()
        if legacy_class:
            node["template_name"] = legacy_class
    node.pop("children", None)

    label = node.get("id", node.get("name"))
    _normalize_legacy_tracker_state(node, label)
    _normalize_legacy_sites(node, children_by_parent)

    root_position = node.pop("position", None)
    raw_pose = node.get("pose")
    if raw_pose is not None and not isinstance(raw_pose, Mapping):
        raise ValueError(f"资源 {label} 的 pose 必须是对象")
    pose: Dict[str, Any] = dict(raw_pose) if isinstance(raw_pose, Mapping) else {}
    for legacy_key, canonical_key in _LEGACY_POSE_VECTOR_ALIASES.items():
        if legacy_key not in pose:
            continue
        legacy_value = pose.pop(legacy_key)
        current = pose.get(canonical_key)
        if current is not None and not _same_vector(current, legacy_value):
            raise ValueError(
                f"资源 {label} 的 pose.{canonical_key} 与旧字段 pose.{legacy_key} 冲突"
            )
        pose.setdefault(canonical_key, legacy_value)
    if root_position is not None:
        if not isinstance(root_position, Mapping):
            raise ValueError(f"资源 {label} 的根字段 position 必须是对象")
        coords = {
            axis: root_position[axis]
            for axis in ("x", "y", "z")
            if root_position.get(axis) is not None
        }
        current = pose.get("position")
        if current is None:
            if coords:
                pose["position"] = coords
        elif coords and not _same_vector(current, coords):
            raise ValueError(f"资源 {label} 的根字段 position 与 pose.position 冲突")
    if pose or raw_pose is not None:
        node["pose"] = pose
    return node


def normalize_legacy_graph_link(
    link: Mapping[str, Any], *, uuid_to_id: Mapping[str, str]
) -> Optional[Dict[str, Any]]:
    """把一条旧形状边转成当前契约；端点无法落到节点集合时返回 ``None``。

    旧后端的边只有 ``source_uuid`` / ``target_uuid``，按 ``uuid_to_id`` 补出
    ``source`` / ``target``；``source_handle`` / ``target_handle`` 改成驼峰名。
    """

    item = dict(link)
    for legacy_key, canonical_key in _LEGACY_LINK_HANDLE_ALIASES.items():
        if legacy_key in item:
            value = item.pop(legacy_key)
            if canonical_key not in item:
                item[canonical_key] = value
    for end in ("source", "target"):
        if not item.get(end):
            node_id = uuid_to_id.get(str(item.get(f"{end}_uuid") or ""))
            if node_id:
                item[end] = node_id
    if not item.get("source") or not item.get("target"):
        return None
    return item


def normalize_legacy_graph(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """整图转换：返回一份当前契约形状的新 payload（``nodes`` + ``links``）。

    其余顶层键（``directed`` / ``graph`` 等 node-link 元数据）原样保留；旧后端
    的 ``edges`` 键并入 ``links``。端点不在节点集合中的边丢弃并记日志。
    """

    graph = copy.deepcopy(dict(payload))
    nodes = [dict(node) for node in graph.get("nodes") or [] if isinstance(node, Mapping)]
    children = legacy_graph_children_index(nodes)
    for node in nodes:
        normalize_legacy_graph_node(node, children_by_parent=children)

    raw_links = graph.get("links")
    legacy_edges = graph.pop("edges", None)
    if raw_links is None:
        raw_links = legacy_edges
    uuid_to_id: Dict[str, str] = {}
    for node in nodes:
        node_uuid = str(node.get("uuid") or "")
        node_id = str(node.get("id") or node.get("name") or "")
        if node_uuid and node_id:
            uuid_to_id.setdefault(node_uuid, node_id)
    links: List[Dict[str, Any]] = []
    for link in raw_links or []:
        if not isinstance(link, Mapping):
            continue
        converted = normalize_legacy_graph_link(link, uuid_to_id=uuid_to_id)
        if converted is None:
            logger.warning("[LegacyGraph] 边 %s 的端点不在节点集合中，已忽略", dict(link))
            continue
        links.append(converted)
    graph["nodes"] = nodes
    graph["links"] = links
    return graph


def legacy_graph_markers(payload: Mapping[str, Any]) -> Dict[str, int]:
    """统计 payload 中出现的旧字段（标记 → 次数）；空字典表示已是当前契约。"""

    markers: Dict[str, int] = {}

    def hit(name: str) -> None:
        markers[name] = markers.get(name, 0) + 1

    for node in payload.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        if node.get("class") and not str(node.get("template_name") or "").strip():
            hit("class 无 template_name")
        if "position" in node:
            hit("根级 position")
        # children 是派生字段，读取时本就丢弃；空列表不携带任何旧格式信息，
        # 只有真的用它表达父子关系才算旧格式（否则受管进程图每次启动都告警）。
        if node.get("children"):
            hit("children 列表")
        pose = node.get("pose")
        if isinstance(pose, Mapping) and any(key in pose for key in _LEGACY_POSE_VECTOR_ALIASES):
            hit("pose.position_3d")
        if any(
            isinstance(site, Mapping) and "occupied_by" in site
            for sites in _site_containers(node)
            for site in sites
        ):
            hit("Site occupied_by")
        data = node.get("data")
        scopes = [node, data] if isinstance(data, Mapping) else [node]
        if isinstance(data, Mapping) and (
            "pending_liquids" in data or _has_dev_era_liquid(node)
        ):
            hit("旧液体字段")
        elif any(
            (
                isinstance(scope.get("liquid_history"), list)
                and any(not isinstance(entry, (list, tuple)) for entry in scope["liquid_history"])
            )
            or any(
                isinstance(scope.get(key), list)
                and any(_is_legacy_liquid_entry(entry) for entry in scope[key])
                for key in ("liquids", "substances")
            )
            for scope in scopes
        ):
            hit("旧液体字段")

    if "edges" in payload and "links" not in payload:
        hit("edges 键")
    for link in payload.get("links") or payload.get("edges") or []:
        if not isinstance(link, Mapping):
            continue
        if any(key in link for key in _LEGACY_LINK_HANDLE_ALIASES):
            hit("边 source_handle/target_handle")
        if (not link.get("source") and link.get("source_uuid")) or (
            not link.get("target") and link.get("target_uuid")
        ):
            hit("边仅有 uuid 端点")
    return markers


def upgrade_legacy_graph_payload(
    payload: Mapping[str, Any],
    *,
    source: str,
    report: Callable[[str], None],
) -> Dict[str, Any]:
    """图文件读取边界：识别到旧字段就整图转换并经 ``report`` 提示，否则原样返回。"""

    markers = legacy_graph_markers(payload)
    if not markers:
        return dict(payload)
    summary = "、".join(f"{name} ×{count}" for name, count in markers.items())
    report(f"{source} 使用旧格式图字段（{summary}），已按旧后端契约转换为当前 node-link 契约")
    return normalize_legacy_graph(payload)


def normalize_legacy_material_node(
    node: Mapping[str, Any],
    *,
    children_by_parent: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    """把旧后端 ``/edge/material/query`` 返回的单个节点规范成 ``ResourceDict`` 输入。

    在 :func:`normalize_legacy_graph_node` 之上补齐 ``ResourceDict`` 校验所需的
    容器默认值（旧后端把空对象序列化成 ``null``）。
    """

    item = normalize_legacy_graph_node(
        copy.deepcopy(dict(node)), children_by_parent=children_by_parent
    )
    for key in ("description", "model", "schema"):
        if key in item and item[key] is None:
            item.pop(key)
    for key in ("config", "data", "extra"):
        if not isinstance(item.get(key), dict):
            item[key] = {}
    if not item.get("parent_uuid"):
        item["parent_uuid"] = None
    return item


def normalize_legacy_material_nodes(
    nodes: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """批量版本：同一批节点互为父子时，``occupied_by`` 能按批内子物料解析。"""

    items = [node for node in nodes if isinstance(node, Mapping)]
    children = legacy_graph_children_index(items)
    return [
        normalize_legacy_material_node(item, children_by_parent=children) for item in items
    ]


__all__ = [
    "legacy_graph_children_index",
    "legacy_graph_markers",
    "normalize_legacy_graph",
    "normalize_legacy_graph_link",
    "normalize_legacy_graph_node",
    "normalize_legacy_material_node",
    "normalize_legacy_material_nodes",
    "upgrade_legacy_graph_payload",
]
