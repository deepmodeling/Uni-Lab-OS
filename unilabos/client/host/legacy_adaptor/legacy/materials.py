"""旧后端物料通知（``add|update|remove_material``）向微后端权威的翻译。

旧 Backend 的前端直接改物料镜像，然后按 uuid 通知 Edge；Edge 曾从旧后端
``/edge/material/query`` 把节点拉回来再挂到设备上。现在权威是微后端：

1. 从旧后端拉节点（旧形状：``position``、``position_3d``、``liquids``…），
   经 :mod:`~unilabos.server.backend.legacy_adaptor.legacy.graph` 投影成
   ``ResourceDict`` 输入；
2. add   → 权威 ``ensure``（保留旧后端 uuid）→ 设备 ``add`` 分发；
   update → 权威 ``update``（diff/apply）→ 设备 ``update`` 分发；
   remove → 设备 ``remove`` 分发 → 权威 ``remove``；
3. 分发目标沿用旧协议的 ``device_id``（空则 host_node），设备迁移
   （``device_old_id != device_id``）拆成 remove + add。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from unilabos.backend.hostlink.downlink import notify_resource_tree_update
from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.protocol.materials import ACTOR_BACKEND
from unilabos.server.backend.legacy_adaptor.legacy.graph import (
    normalize_legacy_material_nodes,
)
from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


def _group_notice(
    action: str, items: Iterable[Any]
) -> Dict[Tuple[str, str], List[str]]:
    """按 ``(device_id, action)`` 分组；update 的设备迁移拆成 remove + add。"""

    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    host = BasicConfig.host_node_name or "host_node"
    for item in items:
        if not isinstance(item, Mapping):
            continue
        material_uuid = str(item.get("uuid") or "").strip()
        if not material_uuid:
            continue
        device_id = str(item.get("device_id") or "").strip() or host
        if action == "update":
            old_device_id = str(item.get("device_old_id") or "").strip() or host
            if old_device_id != device_id:
                groups[(old_device_id, "remove")].append(material_uuid)
                groups[(device_id, "add")].append(material_uuid)
                logger.info(
                    "[LegacyMaterials] 跨站转移 %s: %s -> %s",
                    material_uuid[:8],
                    old_device_id,
                    device_id,
                )
                continue
        groups[(device_id, action)].append(material_uuid)
    return groups


def _legacy_client() -> Any:
    from unilabos.server.backend.legacy_adaptor.legacy.http import LegacyBackendHTTPClient

    return LegacyBackendHTTPClient()


def _pull_trees(client: Any, uuids: List[str]) -> Any:
    """从旧后端拉取节点并装配成 ``ResourceTreeSet``（保留 uuid）。"""

    from unilabos.resources.resource_tracker import ResourceTreeSet

    raw_nodes = client.query_material_tree(uuids, with_children=True)
    if not raw_nodes:
        return ResourceTreeSet([])
    return ResourceTreeSet.from_raw_dict_list(normalize_legacy_material_nodes(raw_nodes))


def apply_legacy_material_notice(
    action: str,
    items: Iterable[Any],
    *,
    client: Any = None,
    gateway: Any = None,
) -> Dict[str, Any]:
    """处理一条旧后端物料通知；返回各分组的分发结果。"""

    from unilabos.resources import materials

    groups = _group_notice(action, items)
    if not groups:
        return {}
    http = client or _legacy_client()
    gw = gateway or materials.resolve_materials_gateway()
    results: Dict[str, Any] = {}
    for (device_id, group_action), uuids in groups.items():
        key = f"{device_id}:{group_action}"
        try:
            if group_action in ("add", "update"):
                tree_set = _pull_trees(http, uuids)
                if not tree_set.trees:
                    logger.warning(
                        "[LegacyMaterials] 旧后端未返回 %s 的节点，跳过 %s",
                        uuids,
                        group_action,
                    )
                    results[key] = None
                    continue
                if group_action == "add":
                    # 账本来源记为云端/旧后端，前端据此区分于本地 Edge 写点。
                    materials.ensure(
                        tree_set,
                        gateway=gw,
                        actor_type=ACTOR_BACKEND,
                        actor_uuid=str(HTTPConfig.remote_addr or "").strip() or None,
                    )
                else:
                    materials.update(
                        tree_set,
                        source_device_id=device_id,
                        gateway=gw,
                    )
                notified = notify_resource_tree_update(device_id, group_action, uuids)
            else:
                notified = notify_resource_tree_update(device_id, "remove", uuids)
                try:
                    materials.remove(uuids, source_device_id=device_id, gateway=gw)
                except Exception as exc:  # noqa: BLE001 - 权威可能已无该物料
                    logger.info("[LegacyMaterials] 权威删除 %s 跳过: %s", uuids, exc)
            results[key] = notified
            logger.info(
                "[LegacyMaterials] %s x%d -> %s: %s",
                group_action,
                len(uuids),
                device_id,
                {True: "完成", False: "失败", None: "设备不可达"}.get(notified, notified),
            )
        except Exception:  # noqa: BLE001 - 单组失败不影响其它分组
            logger.exception("[LegacyMaterials] %s 处理失败", key)
            results[key] = False
    return results


__all__ = ["apply_legacy_material_notice"]
