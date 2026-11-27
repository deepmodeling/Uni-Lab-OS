"""严格解析物料占位符（ResourceSlot）的父资源上下文。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from unilabos.resources.instance_identity import normalize_resource_instance_barcode


class ResourceSlotHydrationError(ValueError):
    """物料占位符（ResourceSlot）无法安全水合为完整父资源上下文。"""


_production_resource_lock = threading.RLock()
_production_resource_nodes: tuple[dict[str, Any], ...] = ()
_production_resource_lookup: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]] | None = None
PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA = "unilabos_source_runtime_uuid"


def install_production_resource_nodes(
    resource_trees: Sequence[Mapping[str, Any]]
    | Sequence[Sequence[Mapping[str, Any]]],
    material_uuids_by_barcode: Mapping[str, str],
    *,
    resource_uuids_by_local_id: Mapping[str, str] | None = None,
) -> None:
    """安装生产 Edge 的 Backend 身份资源投影。

    参数说明：``resource_trees`` 是工作区设备图提供的物理资源形状；
    ``material_uuids_by_barcode`` 是 Backend 返回的稳定物料身份；
    ``resource_uuids_by_local_id`` 承载 well/tip spot 等 Backend Site 身份。
    返回：无。

    所有运行资源必须能映射到 Backend；本投影只驻留内存，不创建本地 Scheduler
    或库存数据库。缺失身份、重复身份或父关系无法重写时失败关闭。
    """

    source_nodes = [deepcopy(dict(node)) for node in _flatten_resource_nodes(resource_trees)]
    local_resource_uuids = {
        str(local_id).strip(): str(resource_uuid).strip()
        for local_id, resource_uuid in (resource_uuids_by_local_id or {}).items()
        if str(local_id).strip() and str(resource_uuid).strip()
    }
    backend_uuid_by_local_uuid: dict[str, str] = {}
    backend_uuid_by_local_id: dict[str, str] = {}
    projected_uuid_by_index: list[str] = []
    missing_barcodes: list[str] = []
    for node in source_nodes:
        local_id = str(node.get("id") or node.get("name") or "").strip()
        if not local_id:
            raise ResourceSlotHydrationError("生产资源投影行缺少本地 ID")
        barcode = normalize_resource_instance_barcode(node.get("barcode"), local_id)
        backend_uuid = (
            local_resource_uuids.get(local_id)
            or str(material_uuids_by_barcode.get(barcode) or "").strip()
        )
        if not backend_uuid:
            missing_barcodes.append(barcode)
            projected_uuid_by_index.append("")
            continue
        if backend_uuid in projected_uuid_by_index:
            raise ResourceSlotHydrationError("生产资源投影包含重复 Backend UUID")
        projected_uuid_by_index.append(backend_uuid)
        backend_uuid_by_local_id[local_id] = backend_uuid
        local_uuid = str(node.get("uuid") or node.get("unilabos_uuid") or "").strip()
        if local_uuid:
            backend_uuid_by_local_uuid[local_uuid] = backend_uuid
    if missing_barcodes:
        raise ResourceSlotHydrationError(
            "生产资源尚未在 Backend 初始化: " + ", ".join(sorted(missing_barcodes))
        )

    projected_nodes: list[dict[str, Any]] = []
    for index, node in enumerate(source_nodes):
        projected = deepcopy(node)
        projected["uuid"] = projected_uuid_by_index[index]
        projected.pop("unilabos_uuid", None)
        local_uuid = str(
            node.get("uuid") or node.get("unilabos_uuid") or ""
        ).strip()
        projected_extra = projected.get("extra")
        if not isinstance(projected_extra, Mapping):
            projected_extra = {}
        else:
            projected_extra = deepcopy(dict(projected_extra))
        if local_uuid:
            # Backend UUID remains authoritative outside the Edge.  Preserve the
            # runtime UUID only as private projection metadata so the driver
            # boundary can recover the exact object already owned by the local
            # device/resource tracker.
            projected_extra[PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA] = local_uuid
        projected["extra"] = projected_extra
        parent_uuid = str(node.get("parent_uuid") or "").strip()
        parent_id = str(node.get("parent") or "").strip()
        projected["parent_uuid"] = (
            backend_uuid_by_local_uuid.get(parent_uuid)
            or backend_uuid_by_local_id.get(parent_id)
            or None
        )
        projected_nodes.append(projected)

    with _production_resource_lock:
        global _production_resource_nodes
        global _production_resource_lookup
        _production_resource_nodes = tuple(projected_nodes)
        _production_resource_lookup = None


def install_production_resource_lookup(
    lookup: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]] | None,
) -> None:
    """安装生产投影缺失时向 Backend 补查物料树的回调。"""

    global _production_resource_lookup
    _production_resource_lookup = lookup


def upsert_production_resource_nodes(
    resource_nodes: Sequence[Mapping[str, Any]],
) -> None:
    """把已具 Backend UUID 的动态物料并入当前生产投影。"""

    incoming = [deepcopy(dict(node)) for node in resource_nodes]
    with _production_resource_lock:
        global _production_resource_nodes
        merged = {
            _resource_uuid(node): deepcopy(node)
            for node in _production_resource_nodes
        }
        for node in incoming:
            merged[_resource_uuid(node)] = deepcopy(node)
        _production_resource_nodes = tuple(merged.values())


def query_production_resource_nodes_sync(
    query_uuids: Sequence[str],
) -> list[dict[str, Any]]:
    """从生产内存投影按 Backend UUID 查询完整资源子树。

    参数说明：``query_uuids`` 是 Backend 调度参数中的稳定物料身份。返回：按安装
    顺序展开的目标及其全部后代。投影未安装或目标不存在时抛出异常，不回退本地
    Scheduler。
    """

    normalized_uuids = [str(resource_uuid).strip() for resource_uuid in query_uuids]
    if not normalized_uuids or any(not resource_uuid for resource_uuid in normalized_uuids):
        raise ResourceSlotHydrationError("生产资源查询必须提供稳定 UUID")
    with _production_resource_lock:
        nodes = [deepcopy(node) for node in _production_resource_nodes]
        lookup = _production_resource_lookup
    if not nodes:
        raise ResourceSlotHydrationError("生产资源投影尚未安装")
    nodes_by_uuid = {_resource_uuid(node): node for node in nodes}
    missing = [resource_uuid for resource_uuid in normalized_uuids if resource_uuid not in nodes_by_uuid]
    if missing:
        if lookup is None:
            raise ResourceSlotHydrationError(
                "Backend 物料不属于当前 Edge 设备图: " + ", ".join(missing)
            )
        fetched = lookup(missing)
        if not fetched:
            raise ResourceSlotHydrationError(
                "Backend 物料不属于当前 Edge 设备图: " + ", ".join(missing)
            )
        upsert_production_resource_nodes(fetched)
        with _production_resource_lock:
            nodes = [deepcopy(node) for node in _production_resource_nodes]
        nodes_by_uuid = {_resource_uuid(node): node for node in nodes}
        still_missing = [
            resource_uuid
            for resource_uuid in normalized_uuids
            if resource_uuid not in nodes_by_uuid
        ]
        if still_missing:
            raise ResourceSlotHydrationError(
                "Backend 物料不属于当前 Edge 设备图: " + ", ".join(still_missing)
            )
    selected: set[str] = set(normalized_uuids)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            resource_uuid = _resource_uuid(node)
            parent_uuid = _optional_uuid(node.get("parent_uuid"))
            if parent_uuid in selected and resource_uuid not in selected:
                selected.add(resource_uuid)
                changed = True
    return [node for node in nodes if _resource_uuid(node) in selected]


def query_production_resource_by_local_id_sync(
    local_id: str, *, with_children: bool = True,
) -> list[dict[str, Any]]:
    """通过已注册的本地别名解析 Backend UUID；缺失或歧义时拒绝查询。"""
    normalized = str(local_id).strip()
    if not normalized:
        raise ResourceSlotHydrationError("生产资源查询必须提供本地 ID")
    with _production_resource_lock:
        matches = [node for node in _production_resource_nodes if node.get("id") == normalized]
        if len(matches) != 1:
            raise ResourceSlotHydrationError("本地资源 ID 未注册或不唯一: " + normalized)
        resource_uuid = _resource_uuid(matches[0])
        nodes = query_production_resource_nodes_sync([resource_uuid])
    return nodes if with_children else [node for node in nodes if _resource_uuid(node) == resource_uuid]


@dataclass(frozen=True, slots=True)
class ResourceSlotParentContext:
    """一次单物料父上下文查询计划。

    ``target_uuid`` 是动作实际引用的物料（Material）身份；``parent_uuid`` 是
    第一次查询明确声明的父资源身份，缺失时表示不需要第二次父树查询。
    """

    target_uuid: str
    parent_uuid: str | None


def query_resource_nodes_sync(
    resource_client: Any,
    query_uuids: Sequence[str],
    *,
    request_factory: Callable[..., Any],
) -> list[dict[str, Any]]:
    """通过 ROS 资源服务同步查询稳定 UUID 的完整子树行。

    参数说明：``resource_client`` 是资源服务客户端；``query_uuids`` 是本轮资源
    身份；``request_factory`` 根据 ``command=...`` 创建 ROS 请求。返回：JSON
    解码后的扁平资源行。

    异常说明：查询超时、空响应、空结果或非数组响应时抛出异常；本函数只封装既有
    查询协议，不改变 ``with_children`` 语义。
    """

    normalized_query_uuids = [str(resource_uuid) for resource_uuid in query_uuids]
    future = resource_client.call_async(
        request_factory(
            command=json.dumps(
                {
                    "data": {
                        "data": normalized_query_uuids,
                        "with_children": True,
                    },
                    "action": "get",
                }
            )
        )
    )
    timeout_seconds = 30.0
    elapsed_seconds = 0.0
    while not future.done() and elapsed_seconds < timeout_seconds:
        time.sleep(0.02)
        elapsed_seconds += 0.02
    if not future.done():
        raise TimeoutError(f"资源查询超时: {normalized_query_uuids}")
    response = future.result()
    if response is None:
        raise ValueError(f"资源查询返回空结果: {normalized_query_uuids}")
    decoded = json.loads(response.response)
    if not decoded:
        raise ValueError(f"资源原始查询返回空结果: {decoded}")
    if not isinstance(decoded, list):
        raise ResourceSlotHydrationError("资源查询结果必须是数组")
    return decoded


def hydrate_resource_slot_nodes_sync(
    target_uuid: str,
    direct_nodes: Sequence[Mapping[str, Any]],
    *,
    query_nodes: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """同步补查并验证单物料占位符（ResourceSlot）的父资源树。

    参数说明：``target_uuid`` 是目标物料稳定 UUID；``direct_nodes`` 是第一次查询
    结果；``query_nodes`` 按 UUID 查询完整子树。返回：无父资源时复制第一次结果，
    有父资源时返回验证后的完整父树行。

    异常说明：直接查询或父树不满足唯一性和父关系不变量时抛出
    ``ResourceSlotHydrationError``，不得回退裸物料。
    """

    context = plan_resource_slot_parent_context(target_uuid, direct_nodes)
    if context.parent_uuid is None:
        return [dict(node) for node in direct_nodes]
    parent_nodes = query_nodes([context.parent_uuid])
    validated_parent_nodes = validate_resource_slot_parent_context(
        context,
        parent_nodes,
    )
    if _use_direct_device_subtree(context, validated_parent_nodes):
        return [dict(node) for node in direct_nodes]
    return validated_parent_nodes


async def hydrate_resource_slot_tree_async(
    target_uuid: str,
    direct_tree: Any,
    *,
    query_tree: Callable[[str], Awaitable[Any]],
) -> tuple[Any, ResourceSlotParentContext]:
    """异步补查并验证单物料占位符（ResourceSlot）的父资源树。

    参数说明：``target_uuid`` 是目标物料稳定 UUID；``direct_tree`` 是第一次查询的
    ``ResourceTreeSet``；``query_tree`` 按父 UUID 查询完整子树。返回：最终可装配
    资源树及不可变父上下文计划。

    异常说明：资源树缺少 ``dump``、目标或父关系不安全时抛出异常并失败关闭。
    """

    context = plan_resource_slot_parent_context(target_uuid, direct_tree.dump())
    if context.parent_uuid is None:
        return direct_tree, context
    parent_tree = await query_tree(context.parent_uuid)
    validated_parent_nodes = validate_resource_slot_parent_context(
        context,
        parent_tree.dump(),
    )
    if _use_direct_device_subtree(context, validated_parent_nodes):
        return direct_tree, context
    return parent_tree, context


def resolve_resource_slot_target(
    target_uuid: str,
    *,
    source_root: Any,
    resolved_root: Any,
    resource_tracker: Any,
) -> Any:
    """从验证后的父资源树中返回驱动实际需要的目标物料。

    参数说明：``target_uuid`` 是物料稳定 UUID；``source_root`` 是新装配的父树根；
    ``resolved_root`` 是资源跟踪器映射后的本地父树根；``resource_tracker`` 提供递归
    查找与本地映射。返回：唯一目标物料实例，且保留其 ``parent.sites`` 关系。

    异常说明：目标缺失或映射到多个本地实例时抛出 ``ValueError``，禁止误把父资源
    当成动作物料参数。
    """

    resolved_target = _find_resource_by_uuid(
        resource_tracker,
        resolved_root,
        target_uuid,
    )
    if resolved_target is not None:
        return resolved_target
    source_target = _find_resource_by_uuid(
        resource_tracker,
        source_root,
        target_uuid,
    )
    if source_target is None:
        raise ValueError(f"父资源树未能装配目标物料 uuid={target_uuid}")
    local_targets = resource_tracker.figure_resource(source_target, try_mode=True)
    if len(local_targets) == 1:
        return local_targets[0]
    if len(local_targets) > 1:
        raise ValueError(f"目标物料转换得到多个实例: {local_targets}")
    return source_target


def plan_resource_slot_parent_context(
    target_uuid: str,
    direct_nodes: Sequence[Mapping[str, Any]] | Sequence[Sequence[Mapping[str, Any]]],
) -> ResourceSlotParentContext:
    """从第一次目标查询中生成父资源上下文计划。

    参数说明：``target_uuid`` 是物料占位符（ResourceSlot）携带的稳定物料 UUID；
    ``direct_nodes`` 是资源服务按该 UUID 返回的扁平行或树分组。返回：不可变父
    上下文计划；无 ``parent_uuid`` 时保留旧的一次查询行为。

    异常说明：目标 UUID 为空、查询结果缺少目标、重复返回目标、父 UUID 自引用或
    行结构非法时抛出 ``ResourceSlotHydrationError``，禁止猜测父资源。
    """

    normalized_target_uuid = str(target_uuid or "").strip()
    if not normalized_target_uuid:
        raise ResourceSlotHydrationError("物料占位符缺少目标物料 UUID")
    # ``target_rows`` 是第一次查询中与目标物料稳定身份完全相等的事实行。
    target_rows = [
        node
        for node in _flatten_resource_nodes(direct_nodes)
        if _resource_uuid(node) == normalized_target_uuid
    ]
    if not target_rows:
        raise ResourceSlotHydrationError("目标资源查询未包含目标物料")
    if len(target_rows) != 1:
        raise ResourceSlotHydrationError("目标资源查询返回重复的目标物料")
    parent_uuid = _optional_uuid(target_rows[0].get("parent_uuid"))
    if parent_uuid == normalized_target_uuid:
        raise ResourceSlotHydrationError("目标物料父关系冲突：父 UUID 不能指向自身")
    return ResourceSlotParentContext(
        target_uuid=normalized_target_uuid,
        parent_uuid=parent_uuid,
    )


def validate_resource_slot_parent_context(
    context: ResourceSlotParentContext,
    parent_nodes: Sequence[Mapping[str, Any]] | Sequence[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """验证第二次查询确实返回目标所属的唯一完整父资源树。

    参数说明：``context`` 固定第一次查询声明的目标与父 UUID；``parent_nodes`` 是
    按父 UUID 且 ``with_children=True`` 返回的扁平行或树分组。返回：可交给既有
    ``ResourceTreeSet`` 装配的独立字典列表。

    异常说明：计划无父 UUID、父查询为空、出现重复 UUID、多棵树、父根不匹配、
    未包含目标物料或目标父关系冲突时抛出 ``ResourceSlotHydrationError``。调用者
    必须失败关闭，不得回退第一次查询得到的裸物料。
    """

    if context.parent_uuid is None:
        raise ResourceSlotHydrationError("无父资源的物料不需要验证父资源树")
    rows = _flatten_resource_nodes(parent_nodes)
    if not rows:
        raise ResourceSlotHydrationError("父资源查询返回空结果")

    # ``rows_by_uuid`` 同时用于检测重复身份和计算资源树根。
    rows_by_uuid: dict[str, dict[str, Any]] = {}
    for node in rows:
        resource_uuid = _resource_uuid(node)
        if resource_uuid in rows_by_uuid:
            raise ResourceSlotHydrationError("父资源查询返回重复的资源 UUID")
        rows_by_uuid[resource_uuid] = dict(node)

    target_node = rows_by_uuid.get(context.target_uuid)
    if target_node is None:
        raise ResourceSlotHydrationError("父资源查询未包含目标物料")
    actual_parent_uuid = _optional_uuid(target_node.get("parent_uuid"))
    if actual_parent_uuid != context.parent_uuid:
        raise ResourceSlotHydrationError("目标物料父关系冲突")

    # ``root_uuids`` 是父查询结果中父关系未指向集合内部的树根身份。
    root_uuids = [
        resource_uuid
        for resource_uuid, node in rows_by_uuid.items()
        if _optional_uuid(node.get("parent_uuid")) not in rows_by_uuid
    ]
    if len(root_uuids) != 1:
        raise ResourceSlotHydrationError("父资源查询必须返回恰好一棵父资源树")
    if root_uuids[0] != context.parent_uuid:
        raise ResourceSlotHydrationError("父资源查询返回的树根与声明父 UUID 不一致")
    if context.parent_uuid not in rows_by_uuid:
        raise ResourceSlotHydrationError("父资源查询未包含声明的父资源")
    return list(rows_by_uuid.values())


def _use_direct_device_subtree(
    context: ResourceSlotParentContext,
    parent_nodes: Sequence[Mapping[str, Any]],
) -> bool:
    """判断目标或其直属父级是否位于设备投影边界。

    参数说明：``context`` 是已验证的父查询计划；``parent_nodes`` 是同一计划通过
    ``validate_resource_slot_parent_context`` 验证后的扁平父树。返回：目标或父根为设备时，使用经过父关系验证的目标子树。

    设备不属于 PLR Resource 投影。目标物料已经由第一次 ``with_children`` 查询
    完整取得时，父设备只用于验证归属，不能替换掉可装配的目标子树。
    """

    return any(
        _resource_uuid(node) in (context.target_uuid, context.parent_uuid)
        and str(node.get("type") or "").strip().lower() == "device"
        for node in parent_nodes
    )


def _flatten_resource_nodes(
    nodes: Sequence[Mapping[str, Any]] | Sequence[Sequence[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    """把扁平行或资源树分组规范为单一行列表。

    参数说明：``nodes`` 来自同步 JSON 响应或 ``ResourceTreeSet.dump()``。返回：
    保持原行对象的扁平列表；容器或行类型非法时抛出
    ``ResourceSlotHydrationError``。
    """

    if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence):
        raise ResourceSlotHydrationError("资源查询结果必须是数组")
    flattened: list[Mapping[str, Any]] = []
    for item in nodes:
        if isinstance(item, Mapping):
            flattened.append(item)
            continue
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            raise ResourceSlotHydrationError("资源查询结果包含非法行")
        for child in item:
            if not isinstance(child, Mapping):
                raise ResourceSlotHydrationError("资源查询结果包含非法行")
            flattened.append(child)
    return flattened


def _resource_uuid(node: Mapping[str, Any]) -> str:
    """读取资源行的稳定 UUID。

    参数说明：``node`` 是资源查询事实行。返回：优先 ``uuid``、兼容
    ``unilabos_uuid`` 的规范字符串；缺失时抛出 ``ResourceSlotHydrationError``。
    """

    resource_uuid = _optional_uuid(node.get("uuid") or node.get("unilabos_uuid"))
    if resource_uuid is None:
        raise ResourceSlotHydrationError("资源查询行缺少稳定 UUID")
    return resource_uuid


def _find_resource_by_uuid(
    resource_tracker: Any,
    root_resource: Any,
    target_uuid: str,
) -> Any | None:
    """在资源根及其子树中按稳定 UUID 查找实例。

    参数说明：``resource_tracker`` 提供递归查找；``root_resource`` 是待查父树根；
    ``target_uuid`` 是目标物料稳定 UUID。返回：命中的根或子资源，未命中返回
    ``None``。
    """

    if _object_resource_uuid(root_resource) == target_uuid:
        return root_resource
    return resource_tracker.loop_find_with_uuid(root_resource, target_uuid)


def _object_resource_uuid(resource: Any) -> str:
    """读取字典或对象资源的稳定 UUID。

    参数说明：``resource`` 是资源树中的任意节点。返回：可用稳定身份，缺失时返回
    空字符串；本辅助函数不按名称或条码猜测身份。
    """

    if isinstance(resource, Mapping):
        return str(
            resource.get("uuid")
            or resource.get("unilabos_uuid")
            or resource.get("id")
            or ""
        )
    return str(
        getattr(resource, "unilabos_uuid", None)
        or getattr(resource, "uuid", None)
        or getattr(resource, "id", None)
        or ""
    )


def _optional_uuid(value: object) -> str | None:
    """规范化可空的资源稳定 UUID。

    参数说明：``value`` 是 wire 行中的 UUID 值。返回：去除首尾空白的字符串，
    缺失或空字符串返回 ``None``；非字符串值抛出 ``ResourceSlotHydrationError``。
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ResourceSlotHydrationError("资源 UUID 必须是字符串")
    normalized = value.strip()
    return normalized or None
