"""从设备注册表生成 node-link 设备图骨架（``unilab graph create``）。

对每个 ``@device`` 设备类生成一个单实例节点（实例 id 即注册表 id），
``available_sites`` 模板展开为带实例 uuid 的 Site 快照，与手工维护的
demo 图同构；生成结果可直接 ``unilab -g`` 启动或 ``graph upload`` 入库。
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any, Dict, List, Mapping, Optional, Sequence

from unilabos.resources.objects.site import (
    SiteDefinition,
    normalize_available_sites,
    site_definition_extra,
)

#: 画布网格布局：每行节点数与间距（毫米级坐标语义与 demo 图一致）。
_GRID_COLUMNS = 4
_GRID_SPACING = 400.0

#: 骨架不为这些注册表条目生成节点：host 由启动流程自身装配。
_EXCLUDED_DEVICE_IDS = frozenset({"host_node"})


def _site_snapshot(
    definition: Any,
    *,
    template_name: str,
    material_uuid: str,
) -> Dict[str, Any]:
    # normalize_available_sites 产出 dict；显式过一遍模型保证字段齐全。
    if not isinstance(definition, SiteDefinition):
        definition = SiteDefinition.model_validate(definition)
    dumped = definition.model_dump(mode="json")
    return {
        "schema_version": 1,
        "uuid": str(uuid_module.uuid4()),
        "template_name": template_name,
        "material_uuid": material_uuid,
        "index": dumped["index"],
        "label": dumped["label"],
        "visible": dumped["visible"],
        "occupied_material_uuid": None,
        "pose": dumped["pose"],
        "allowed_resource_categories": dumped["allowed_resource_categories"],
        "parent_link": dumped["parent_link"],
        "description": dumped["description"],
        "meta_data": dumped["meta_data"],
        "extra": site_definition_extra(dumped),
    }


def _device_node(
    device_id: str,
    entry: Mapping[str, Any],
    *,
    position: Dict[str, float],
) -> Dict[str, Any]:
    node_uuid = str(uuid_module.uuid4())
    node: Dict[str, Any] = {
        "id": device_id,
        "uuid": node_uuid,
        "name": str(entry.get("display_name") or "").strip() or device_id,
        "parent": None,
        "type": "device",
        "class": device_id,
        "template_name": device_id,
        "pose": {"position": position},
        "config": {},
        "data": {},
        "extra": {},
    }
    definitions = normalize_available_sites(entry.get("available_sites"))
    if definitions:
        node["sites_initialized"] = True
        node["sites"] = [
            _site_snapshot(
                definition,
                template_name=device_id,
                material_uuid=node_uuid,
            )
            for definition in definitions
        ]
    return node


def build_graph_skeleton(
    registry: Any,
    *,
    include: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """按注册表设备类型生成 node-link 图骨架。

    Args:
        registry: 已构建的 Registry 实例（``device_type_registry`` 为准）。
        include: 只生成这些设备 id 的节点；空/None 表示全部外部设备。
    """

    device_types: Mapping[str, Any] = registry.device_type_registry
    wanted = {item.strip() for item in (include or []) if item and item.strip()}
    unknown = wanted - set(device_types)
    if unknown:
        raise ValueError(
            "注册表中不存在这些设备 id: " + ", ".join(sorted(unknown))
        )

    nodes: List[Dict[str, Any]] = []
    index = 0
    for device_id in sorted(device_types):
        if device_id in _EXCLUDED_DEVICE_IDS:
            continue
        if wanted and device_id not in wanted:
            continue
        entry = device_types[device_id]
        if not isinstance(entry, Mapping):
            continue
        position = {
            "x": (index % _GRID_COLUMNS) * _GRID_SPACING,
            "y": (index // _GRID_COLUMNS) * _GRID_SPACING,
            "z": 0.0,
        }
        nodes.append(_device_node(device_id, entry, position=position))
        index += 1

    if not nodes:
        raise ValueError("注册表中没有可生成节点的设备类型；请检查 --devices 目录")
    return {"nodes": nodes, "links": []}


__all__ = ["build_graph_skeleton"]
