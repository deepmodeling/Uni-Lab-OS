"""把本地资源树一次性投影为库存权威（Inventory Authority）事实。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.local_template_identity import (
    synchronize_local_template_identities,
)
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

_SITE_TYPES = frozenset({"well", "tipspot", "tip_spot", "tip-spot"})
_SOURCE_KEY = "resource_graph_bootstrap_source"
_FINGERPRINT_KEY = "resource_graph_bootstrap_fingerprint"
_FINGERPRINT_VERSION_KEY = "resource_graph_bootstrap_fingerprint_version"
_FINGERPRINT_VERSION = "2"
_HOST_EXECUTOR_ID = "host_node"

logger = logging.getLogger(__name__)


class ResourceGraphBootstrapError(RuntimeError):
    """本地资源图无法安全建立首次库存事实。"""


def bootstrap_local_resource_graph(
    *,
    store: InventoryStore,
    resource_tree_set: Any,
    registry_snapshot: RegistryTemplateSnapshot,
    source_id: str,
    material_rendering_by_template: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """预校验并原子提交本地资源图的物料（Material）与库位（Site）。

    参数：``store`` 是当前主机唯一库存 SQLite；``resource_tree_set`` 提供产品
    ``dump()`` 快照；``registry_snapshot`` 冻结同代资源模板定义；``source_id``
    标识资源图来源；``material_rendering_by_template`` 是工作区编译后、仅指向
    OS 公开 HTTP 路由的模型快照。返回：``imported`` 或 ``unchanged`` 幂等
    回执。异常：身份、拓扑、数值、模板、模型路径、既有权威或指纹冲突时
    抛出 ``ResourceGraphBootstrapError``；
    物料、位置、库位与指纹始终在同一事务提交或全部回滚。
    """

    # ``source_name`` 是持久来源身份和 UUID5 命名空间的一部分；目录位置不参与。
    source_name = Path(str(source_id or "").strip()).name
    if not source_name:
        raise ResourceGraphBootstrapError("资源图来源不能为空")
    try:
        raw_trees = resource_tree_set.dump()
    except (AttributeError, TypeError, ValueError) as error:
        raise ResourceGraphBootstrapError("资源树集合缺少可用 dump 快照") from error
    # ``raw_trees`` 还需包含 OS 内建 Host 平台执行器；它有正式资源模板，但不由
    # 用户资源树显式声明，仍必须在同一库存事务获得实际设备物料身份。
    raw_trees = _with_implicit_host_executor(
        raw_trees,
        registry_snapshot=registry_snapshot,
        source_name=source_name,
    )
    aliases = _template_aliases(registry_snapshot)
    # ``projection`` 在任何模板或库存写入前完成结构验证，避免非法图留下部分事实。
    projection = _compile_projection(
        raw_trees,
        source_name,
        aliases,
        material_rendering_by_template=material_rendering_by_template,
    )
    try:
        resolve_template_uuid = synchronize_local_template_identities(
            inventory_store=store,
            registry_snapshot=registry_snapshot,
        )
        _resolve_projection_templates(projection, resolve_template_uuid)
        fingerprint = _fingerprint(source_name, registry_snapshot, projection)
        status = _commit_projection(store, source_name, fingerprint, projection)
    except ResourceGraphBootstrapError:
        raise
    except Exception as error:
        raise ResourceGraphBootstrapError(f"本地资源图启动投影失败: {error}") from error
    return {
        "status": status,
        "source_id": source_name,
        "fingerprint": fingerprint,
        "material_count": len(projection["materials"]),
        "site_count": len(projection["sites"]),
    }


def _with_implicit_host_executor(
    raw_trees: object,
    *,
    registry_snapshot: RegistryTemplateSnapshot,
    source_name: str,
) -> object:
    """在资源图缺失时追加 OS 内建 Host 平台执行器节点。

    参数：``raw_trees`` 是未信任资源树快照；``registry_snapshot`` 证明
    ``host_node`` 资源模板存在；``source_name`` 生成稳定运行时关系身份。返回：
    模板不存在或资源图已显式声明 Host 时返回原输入，否则返回追加一棵独立
    Host 树的新列表。异常：不提前接管非法资源树诊断，结构错误仍由编译阶段处理。
    """

    host_definitions = [
        definition
        for definition in registry_snapshot.detached_definitions()
        if definition.get("id") == _HOST_EXECUTOR_ID
        and definition.get("registry_type") == "device"
    ]
    if len(host_definitions) != 1:
        return raw_trees
    if not isinstance(raw_trees, Sequence) or isinstance(raw_trees, (str, bytes)):
        return raw_trees
    # ``node_ids`` 仅用于避免重复追加；任何非法成员仍交给 ``_compile_projection``。
    node_ids = {
        node.get("id")
        for tree in raw_trees
        if isinstance(tree, Sequence) and not isinstance(tree, (str, bytes))
        for node in tree
        if isinstance(node, Mapping)
    }
    if _HOST_EXECUTOR_ID in node_ids:
        return raw_trees
    host_definition = host_definitions[0]
    # ``host_node`` 没有物理位置和库位，但相对位置表要求完整有限数值；零尺寸与
    # 单位缩放表达虚拟平台执行器，不冒充实验台上的物理占位。
    host_node = {
        "id": _HOST_EXECUTOR_ID,
        "uuid": _stable_uuid(source_name, "runtime", _HOST_EXECUTOR_ID),
        "name": str(host_definition.get("display_name") or "Host Node"),
        "description": str(host_definition.get("description") or "Host Node"),
        "parent_uuid": None,
        "type": "device",
        "class": _HOST_EXECUTOR_ID,
        "pose": {
            "position": {"x": 0, "y": 0, "z": 0},
            "size": {"width": 0, "height": 0, "depth": 0},
            "scale": {"x": 1, "y": 1, "z": 1},
            "rotation": {"x": 0, "y": 0, "z": 0},
        },
        "config": {"category": "platform-executor", "virtual": True},
        "data": {},
        "barcode": "",
    }
    return [*raw_trees, [host_node]]


def _template_aliases(snapshot: RegistryTemplateSnapshot) -> dict[str, str]:
    """建立注册表别名到资源模板业务 ID 的唯一映射。

    参数：``snapshot`` 是单代注册表快照。返回：业务 ID、显式源码身份及全代唯一
    实现类身份与全代唯一社区包短 ID 的映射；歧义别名不会进入返回值。异常：空
    业务身份，或业务 ID/显式源码身份相互冲突时抛出 ``ResourceGraphBootstrapError``。
    """

    # ``aliases`` 保存作者显式业务身份，以及稍后证明全代唯一的遗留实现类别名。
    aliases: dict[str, str] = {}
    # ``class_owners`` 汇总同代每个 Python 实现类的所有业务模板所有者。
    class_owners: dict[str, set[str]] = {}
    package_short_owners: dict[str, set[str]] = {}
    for definition in snapshot.detached_definitions():
        template_name = str(definition.get("id") or "").strip()
        class_definition = definition.get("class")
        if not template_name:
            raise ResourceGraphBootstrapError("资源模板业务 ID 不能为空")
        # 业务 ID 与 ``source_fqid`` 都是作者明确选择的一一身份；冲突必须关闭。
        for candidate in (template_name, definition.get("source_fqid")):
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            alias = candidate.strip()
            previous = aliases.get(alias)
            if previous is not None and previous != template_name:
                raise ResourceGraphBootstrapError(f"资源模板别名不唯一: {alias}")
            aliases[alias] = template_name
        class_module = (
            class_definition.get("module")
            if isinstance(class_definition, Mapping)
            else None
        )
        if isinstance(class_module, str) and class_module.strip():
            class_owners.setdefault(class_module.strip(), set()).add(template_name)
        if template_name.startswith("community."):
            package_short_owners.setdefault(
                template_name.rsplit(".", 1)[-1], set()
            ).add(template_name)
    for short_alias, owners in package_short_owners.items():
        if len(owners) == 1 and short_alias not in aliases:
            aliases[short_alias] = next(iter(owners))
    for class_alias, owners in class_owners.items():
        # 共享实现类没有唯一业务语义；保留业务 ID，丢弃该遗留便利别名。
        if len(owners) != 1 or class_alias in aliases:
            continue
        aliases[class_alias] = next(iter(owners))
    return aliases


def _compile_projection(
    raw_trees: object,
    source_name: str,
    aliases: Mapping[str, str],
    *,
    material_rendering_by_template: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """把可疑资源树快照编译成无数据库依赖的规范投影。

    参数：``raw_trees`` 是嵌套树列表；``source_name`` 提供稳定命名空间；
    ``aliases`` 校验资源类已进入注册表；``material_rendering_by_template``
    按模板业务身份提供公开模型快照。返回：物料、位置和库位候选集合。
    异常：集合形状、重复身份、模板、模型路径、父关系或数值非法时关闭式失败。
    """

    if not isinstance(raw_trees, Sequence) or isinstance(raw_trees, (str, bytes)):
        raise ResourceGraphBootstrapError("资源树快照必须是树列表")
    nodes: list[dict[str, Any]] = []
    for raw_tree in raw_trees:
        if not isinstance(raw_tree, Sequence) or isinstance(raw_tree, (str, bytes)):
            raise ResourceGraphBootstrapError("资源树成员必须是节点列表")
        for raw_node in raw_tree:
            nodes.append(_json_object(raw_node, "资源树节点"))
    if not nodes:
        raise ResourceGraphBootstrapError("资源树不得为空")
    # ``node_by_runtime_uuid`` 只解析本次快照关系，不升级为持久物料身份。
    node_by_runtime_uuid: dict[str, dict[str, Any]] = {}
    node_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = _required_text(node.get("id"), "node.id")
        runtime_uuid = _required_text(node.get("uuid"), f"node {node_id} uuid")
        if node_id in node_by_id or runtime_uuid in node_by_runtime_uuid:
            raise ResourceGraphBootstrapError("资源树节点 ID/运行时 UUID 必须唯一")
        node_by_id[node_id] = node
        node_by_runtime_uuid[runtime_uuid] = node

    material_nodes = [node for node in nodes if not _is_site_node(node)]
    if not material_nodes:
        raise ResourceGraphBootstrapError("资源树至少需要一个物料节点")
    material_uuid_by_runtime = {
        _required_text(node.get("uuid"), "material.uuid"): _stable_uuid(
            source_name,
            "material",
            _required_text(node.get("id"), "material.id"),
        )
        for node in material_nodes
    }
    material_runtime_ids = set(material_uuid_by_runtime)
    site_runtime_ids = {
        _required_text(node.get("uuid"), "site.uuid")
        for node in nodes
        if _is_site_node(node)
    }
    materials: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    for node in material_nodes:
        node_id = _required_text(node.get("id"), "material.id")
        runtime_uuid = _required_text(node.get("uuid"), "material.uuid")
        graph_class = _required_text(node.get("class"), f"Material {node_id} class")
        template_name = aliases.get(graph_class)
        if template_name is None:
            raise ResourceGraphBootstrapError(
                f"Material {node_id} 资源模板身份未进入注册表: {graph_class}"
            )
        parent_runtime = _optional_text(node.get("parent_uuid"))
        if parent_runtime in site_runtime_ids:
            site_parent = node_by_runtime_uuid[parent_runtime]
            parent_runtime = _optional_text(site_parent.get("parent_uuid"))
        if parent_runtime is not None and parent_runtime not in material_runtime_ids:
            raise ResourceGraphBootstrapError(f"Material {node_id} 父关系悬空")
        material_uuid = material_uuid_by_runtime[runtime_uuid]
        pose = _pose(node)
        # ``material_config`` 是前端读模型；模型资产仅发布 OS 公开 URL。
        material_config = _material_config_with_rendering(
            node,
            template_name=template_name,
            material_rendering_by_template=material_rendering_by_template,
        )
        materials.append(
            {
                "uuid": material_uuid,
                "template_name": template_name,
                "template_uuid": "",
                "parent_uuid": material_uuid_by_runtime.get(parent_runtime or ""),
                "class": graph_class,
                "barcode": str(node.get("barcode") or ""),
                "name": _required_text(node.get("name") or node_id, "material.name"),
                "description": _optional_text(node.get("description")),
                "meta_data": {
                    "source": "resource-tree-set",
                    "source_graph": source_name,
                    "source_node_id": node_id,
                    "source_runtime_uuid": runtime_uuid,
                },
                "config": material_config,
                "data": _json_object(node.get("data"), "material.data"),
            }
        )
        positions.append(
            {
                "uuid": _stable_uuid(source_name, "relative-position", node_id),
                "material_uuid": material_uuid,
                **pose,
            }
        )

    sites: list[dict[str, Any]] = []
    site_order_by_owner: dict[str, int] = {}
    occupied_materials: set[str] = set()
    for node in nodes:
        if not _is_site_node(node):
            continue
        node_id = _required_text(node.get("id"), "site.id")
        runtime_uuid = _required_text(node.get("uuid"), "site.uuid")
        owner_runtime = _optional_text(node.get("parent_uuid"))
        owner_uuid = material_uuid_by_runtime.get(owner_runtime or "")
        if owner_uuid is None:
            raise ResourceGraphBootstrapError(f"库位（Site）{node_id} 父物料悬空")
        occupant_runtime = [
            candidate_runtime
            for candidate_runtime, candidate in node_by_runtime_uuid.items()
            if _optional_text(candidate.get("parent_uuid")) == runtime_uuid
            and candidate_runtime in material_runtime_ids
        ]
        if len(occupant_runtime) > 1:
            raise ResourceGraphBootstrapError(f"库位（Site）{node_id} 有多个占用物料")
        occupant_uuid = (
            material_uuid_by_runtime[occupant_runtime[0]] if occupant_runtime else None
        )
        if occupant_uuid is not None and occupant_uuid in occupied_materials:
            raise ResourceGraphBootstrapError("一个物料不能占用多个库位（Site）")
        if occupant_uuid is not None:
            occupied_materials.add(occupant_uuid)
        sort_order = site_order_by_owner.get(owner_uuid, 0)
        site_order_by_owner[owner_uuid] = sort_order + 1
        sites.append(
            {
                "uuid": _stable_uuid(source_name, "site", node_id),
                "material_uuid": owner_uuid,
                "name": _required_text(node.get("name") or node_id, "site.name"),
                "sort_order": sort_order,
                "occupied_material_uuid": occupant_uuid,
                "description": _optional_text(node.get("description")),
                "meta_data": {
                    "source": "resource-tree-set",
                    "source_node_id": node_id,
                    "source_runtime_uuid": runtime_uuid,
                },
                "allowed_template_names": _site_content_types(node, aliases),
                "allowed_template_uuids": [],
                **_site_pose(node),
            }
        )
    for owner_node in material_nodes:
        owner_node_id = _required_text(owner_node.get("id"), "material.id")
        owner_runtime = _required_text(owner_node.get("uuid"), "material.uuid")
        owner_uuid = material_uuid_by_runtime[owner_runtime]
        config = _json_object(owner_node.get("config"), "material.config")
        declared_sites = config.get("sites", [])
        if not isinstance(declared_sites, Sequence) or isinstance(
            declared_sites, (str, bytes)
        ):
            raise ResourceGraphBootstrapError("配置式库位（Site）sites 必须是数组")
        for raw_site in declared_sites:
            site = _json_object(raw_site, "config.sites[]")
            site_name = _required_text(
                site.get("name") or site.get("label"), "config site.name"
            )
            occupant_node_id = _optional_text(site.get("occupied_by"))
            occupant_node = node_by_id.get(occupant_node_id or "")
            if occupant_node_id is not None and occupant_node is None:
                raise ResourceGraphBootstrapError(
                    f"库位（Site）{site_name} 占用物料悬空"
                )
            occupant_runtime = (
                _required_text(occupant_node.get("uuid"), "occupied material.uuid")
                if occupant_node is not None
                else None
            )
            occupant_uuid = material_uuid_by_runtime.get(occupant_runtime or "")
            if occupant_node is not None and (
                occupant_uuid is None
                or _optional_text(occupant_node.get("parent_uuid")) != owner_runtime
            ):
                raise ResourceGraphBootstrapError(
                    f"库位（Site）{site_name} 占用物料必须是父物料的直接子物料"
                )
            if occupant_uuid is not None and occupant_uuid in occupied_materials:
                raise ResourceGraphBootstrapError("一个物料不能占用多个库位（Site）")
            if occupant_uuid is not None:
                occupied_materials.add(occupant_uuid)
            sort_order = site_order_by_owner.get(owner_uuid, 0)
            site_order_by_owner[owner_uuid] = sort_order + 1
            site_pose = {
                "pose": {
                    "position": site.get("position"),
                    "size": site.get("size"),
                }
            }
            sites.append(
                {
                    "uuid": _stable_uuid(
                        source_name, "site", f"{owner_node_id}:{site_name}"
                    ),
                    "material_uuid": owner_uuid,
                    "name": site_name,
                    "sort_order": sort_order,
                    "occupied_material_uuid": occupant_uuid,
                    "description": _optional_text(site.get("description")),
                    "meta_data": {
                        "source": "resource-tree-set-config",
                        "source_node_id": owner_node_id,
                    },
                    "allowed_template_names": _site_content_types(
                        {"config": {"content_type": site.get("content_type", [])}},
                        aliases,
                    ),
                    "allowed_template_uuids": [],
                    **_site_pose(site_pose),
                }
            )
    return {"materials": materials, "relative_positions": positions, "sites": sites}


def _material_config_with_rendering(
    node: Mapping[str, Any],
    *,
    template_name: str,
    material_rendering_by_template: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """编译物料外形与公开 3D 模型读快照。

    参数：``node`` 是资源树物料节点；``template_name`` 是已解析的资源
    模板（ResourceTemplate）业务身份；``material_rendering_by_template`` 是工作区
    公开模型快照。返回：无共享引用的物料配置。异常：既有渲染字段非对象、
    模型快照非对象或资产不指向 OS 公开路由时关闭式失败。
    """

    config = _json_object(node.get("config"), "material.config")
    raw_rendering = config.get("rendering")
    if raw_rendering is not None and not isinstance(raw_rendering, Mapping):
        raise ResourceGraphBootstrapError("material.config.rendering 必须是 JSON 对象")
    rendering = _json_object(raw_rendering, "material.config.rendering")

    # ``kind`` 优先保留资源图显式声明，否则使用物料自身类别匹配外形库。
    explicit_kind = rendering.get("kind") or rendering.get("type")
    category = config.get("category") or config.get("type")
    if isinstance(explicit_kind, str) and explicit_kind.strip():
        rendering["kind"] = explicit_kind.strip()
    elif isinstance(category, str) and category.strip():
        rendering["kind"] = category.strip()

    model_snapshot = None
    if material_rendering_by_template is not None:
        if not isinstance(material_rendering_by_template, Mapping):
            raise ResourceGraphBootstrapError("物料模型快照索引必须是对象")
        model_snapshot = material_rendering_by_template.get(template_name)
    if model_snapshot is not None and "model" not in rendering:
        if not isinstance(model_snapshot, Mapping):
            raise ResourceGraphBootstrapError("物料模型快照必须是对象")
        model = _json_object(model_snapshot, "material.rendering.model")
        # ``model_path`` 是浏览器唯一允许的资产入口，不允许 local_bridge。
        model_path = model.get("path")
        if not isinstance(model_path, str) or not model_path.startswith(
            "/api/v1/material-models/"
        ):
            raise ResourceGraphBootstrapError(
                "工作区物料模型必须通过 OS 公开 HTTP 路由发布"
            )
        rendering["model"] = model
    if rendering:
        config["rendering"] = rendering
    return config


def _resolve_projection_templates(
    projection: dict[str, list[dict[str, Any]]],
    resolve_template_uuid: Any,
) -> None:
    """把已校验业务模板名解析为本代稳定 UUID。

    参数：``projection`` 是可写候选；``resolve_template_uuid`` 是单代只读解析器。
    返回：无，原地补齐模板 UUID。异常：任一身份缺失时关闭式失败。
    """

    for material in projection["materials"]:
        template_uuid = resolve_template_uuid(material["template_name"])
        if not template_uuid:
            raise ResourceGraphBootstrapError("物料资源模板 UUID 未解析")
        material["template_uuid"] = template_uuid
    for site in projection["sites"]:
        resolved = [
            resolve_template_uuid(name) for name in site["allowed_template_names"]
        ]
        if any(not value for value in resolved):
            raise ResourceGraphBootstrapError("库位（Site）允许模板 UUID 未解析")
        site["allowed_template_uuids"] = sorted(set(resolved))


def _commit_projection(
    store: InventoryStore,
    source_name: str,
    fingerprint: str,
    projection: Mapping[str, list[dict[str, Any]]],
) -> str:
    """在单一 SQLite 事务中提交投影与幂等指纹。

    参数：存储、来源、指纹和完整投影。返回：``imported`` 或 ``unchanged``。
    异常：既有库存未由同一指纹创建或 SQL 约束失败时回滚并关闭式失败。
    """

    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    try:
        with store.transaction() as connection:
            material_count = int(
                connection.execute("SELECT COUNT(*) FROM material").fetchone()[0]
            )
            stored_source = _meta(connection, _SOURCE_KEY)
            stored_fingerprint = _meta(connection, _FINGERPRINT_KEY)
            stored_fingerprint_version = _meta(
                connection,
                _FINGERPRINT_VERSION_KEY,
            )
            if (
                material_count
                or stored_source is not None
                or stored_fingerprint is not None
            ):
                if stored_source == source_name and stored_fingerprint == fingerprint:
                    if stored_fingerprint_version is None:
                        connection.execute(
                            "INSERT INTO lab_meta(meta_key,meta_value) VALUES (?,?)",
                            (_FINGERPRINT_VERSION_KEY, _FINGERPRINT_VERSION),
                        )
                    return "unchanged"
                if (
                    stored_source == source_name
                    and stored_fingerprint_version is None
                    and _projection_matches_persisted_rows(connection, projection)
                ):
                    # 第 1 版指纹错误包含整个设备动作注册表。仅当当前库存基础行与
                    # 候选投影逐字段相同时，原子升级为只覆盖库存图的第 2 版指纹。
                    connection.execute(
                        "UPDATE lab_meta SET meta_value=? WHERE meta_key=?",
                        (fingerprint, _FINGERPRINT_KEY),
                    )
                    connection.execute(
                        "INSERT INTO lab_meta(meta_key,meta_value) VALUES (?,?)",
                        (_FINGERPRINT_VERSION_KEY, _FINGERPRINT_VERSION),
                    )
                    return "unchanged"
                raise ResourceGraphBootstrapError(
                    "既有库存权威与资源图来源或指纹（fingerprint）冲突"
                )
            for material in projection["materials"]:
                connection.execute(
                    """
                    INSERT INTO material(
                        uuid,create_time,update_time,deleted_at,description,meta_data,
                        resource_template_uuid,parent_uuid,class,barcode,name,config,data
                    ) VALUES (?,?,?,NULL,?,?,?,NULL,?,?,?,?,?)
                    """,
                    (
                        material["uuid"],
                        now,
                        now,
                        material["description"],
                        _dump(material["meta_data"]),
                        material["template_uuid"],
                        material["class"],
                        material["barcode"],
                        material["name"],
                        _dump(material["config"]),
                        _dump(material["data"]),
                    ),
                )
                connection.execute(
                    "INSERT INTO material_inventory(material_uuid,legacy_template_id) VALUES (?,?)",
                    (material["uuid"], material["template_uuid"]),
                )
            for material in projection["materials"]:
                if material["parent_uuid"] is not None:
                    connection.execute(
                        "UPDATE material SET parent_uuid=? WHERE uuid=?",
                        (material["parent_uuid"], material["uuid"]),
                    )
            for position in projection["relative_positions"]:
                connection.execute(
                    """
                    INSERT INTO relative_position(
                        uuid,create_time,update_time,deleted_at,description,meta_data,
                        material_uuid,position_x,position_y,position_z,depth,length,width,
                        scale_x,scale_y,scale_z,rotation_x,rotation_y,rotation_z
                    ) VALUES (?,?,?,NULL,NULL,'{}',?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        position["uuid"],
                        now,
                        now,
                        position["material_uuid"],
                        position["position_x"],
                        position["position_y"],
                        position["position_z"],
                        position["depth"],
                        position["length"],
                        position["width"],
                        position["scale_x"],
                        position["scale_y"],
                        position["scale_z"],
                        position["rotation_x"],
                        position["rotation_y"],
                        position["rotation_z"],
                    ),
                )
            for site in projection["sites"]:
                connection.execute(
                    """
                    INSERT INTO site(
                        uuid,create_time,update_time,deleted_at,description,meta_data,
                        material_uuid,name,sort_order,allowed_resource_template_uuids,
                        occupied_material_uuid,position_x,position_y,position_z,
                        depth,length,width
                    ) VALUES (?,?,?,NULL,?,?,?,?,?,?,NULL,?,?,?,?,?,?)
                    """,
                    (
                        site["uuid"],
                        now,
                        now,
                        site["description"],
                        _dump(site["meta_data"]),
                        site["material_uuid"],
                        site["name"],
                        site["sort_order"],
                        _dump(site["allowed_template_uuids"]),
                        site["position_x"],
                        site["position_y"],
                        site["position_z"],
                        site["depth"],
                        site["length"],
                        site["width"],
                    ),
                )
            for site in projection["sites"]:
                if site["occupied_material_uuid"] is not None:
                    connection.execute(
                        "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
                        (site["occupied_material_uuid"], site["uuid"]),
                    )
            connection.executemany(
                "INSERT INTO lab_meta(meta_key,meta_value) VALUES (?,?)",
                (
                    (_SOURCE_KEY, source_name),
                    (_FINGERPRINT_KEY, fingerprint),
                    (_FINGERPRINT_VERSION_KEY, _FINGERPRINT_VERSION),
                ),
            )
    except ResourceGraphBootstrapError:
        raise
    except sqlite3.IntegrityError as error:
        raise ResourceGraphBootstrapError("资源图投影违反库存唯一性或外键") from error
    except sqlite3.Error as error:
        raise ResourceGraphBootstrapError("资源图投影事务失败") from error
    return "imported"


def _projection_matches_persisted_rows(
    connection: sqlite3.Connection,
    projection: Mapping[str, list[dict[str, Any]]],
) -> bool:
    """核对旧指纹库中的库存资源图基础行是否与当前投影完全一致。

    参数：``connection`` 是当前启动事务，``projection`` 是已解析稳定
    模板 UUID 的候选。返回：物料、相对位置和库位（Site）的数量
    与持久字段全部一致时为真。异常：SQL 错误交由外层统一包装。
    """

    material_fields = (
        "uuid",
        "description",
        "meta_data",
        "resource_template_uuid",
        "parent_uuid",
        "class",
        "barcode",
        "name",
        "config",
        "data",
    )
    expected_materials = sorted(
        (
            material["uuid"],
            material["description"],
            _stable_projection_meta(material["meta_data"]),
            material["template_uuid"],
            material["parent_uuid"],
            material["class"],
            material["barcode"],
            material["name"],
            _dump(material["config"]),
            _dump(material["data"]),
        )
        for material in projection["materials"]
    )
    persisted_materials = []
    for row in connection.execute(
        f"SELECT {','.join(material_fields)} FROM material "
        "WHERE deleted_at IS NULL"
    ).fetchall():
        normalized = list(row)
        normalized[2] = _stable_projection_meta(normalized[2])
        persisted_materials.append(tuple(normalized))
    persisted_materials.sort()
    if not _same_projection_rows(
        "material",
        material_fields,
        persisted_materials,
        expected_materials,
    ):
        return False

    position_fields = (
        "uuid",
        "material_uuid",
        "position_x",
        "position_y",
        "position_z",
        "depth",
        "length",
        "width",
        "scale_x",
        "scale_y",
        "scale_z",
        "rotation_x",
        "rotation_y",
        "rotation_z",
    )
    expected_positions = sorted(
        tuple(position[field] for field in position_fields)
        for position in projection["relative_positions"]
    )
    persisted_positions = sorted(
        tuple(row)
        for row in connection.execute(
            f"SELECT {','.join(position_fields)} FROM relative_position "
            "WHERE deleted_at IS NULL"
        ).fetchall()
    )
    if not _same_projection_rows(
        "relative_position",
        position_fields,
        persisted_positions,
        expected_positions,
    ):
        return False

    site_fields = (
        "uuid",
        "description",
        "meta_data",
        "material_uuid",
        "name",
        "sort_order",
        "allowed_resource_template_uuids",
        "occupied_material_uuid",
        "position_x",
        "position_y",
        "position_z",
        "depth",
        "length",
        "width",
    )
    expected_sites = sorted(
        (
            site["uuid"],
            site["description"],
            _stable_projection_meta(site["meta_data"]),
            site["material_uuid"],
            site["name"],
            site["sort_order"],
            _dump(site["allowed_template_uuids"]),
            site["occupied_material_uuid"],
            site["position_x"],
            site["position_y"],
            site["position_z"],
            site["depth"],
            site["length"],
            site["width"],
        )
        for site in projection["sites"]
    )
    persisted_sites = []
    for row in connection.execute(
        f"SELECT {','.join(site_fields)} FROM site WHERE deleted_at IS NULL"
    ).fetchall():
        normalized = list(row)
        normalized[2] = _stable_projection_meta(normalized[2])
        persisted_sites.append(tuple(normalized))
    persisted_sites.sort()
    return _same_projection_rows(
        "site",
        site_fields,
        persisted_sites,
        expected_sites,
    )


def _same_projection_rows(
    table: str,
    fields: tuple[str, ...],
    persisted: list[tuple[Any, ...]],
    expected: list[tuple[Any, ...]],
) -> bool:
    """比较一类投影行，并仅记录首个结构差异用于启动诊断。"""

    if persisted == expected:
        return True
    persisted_by_uuid = {str(row[0]): row for row in persisted}
    expected_by_uuid = {str(row[0]): row for row in expected}
    missing = sorted(set(expected_by_uuid) - set(persisted_by_uuid))
    extra = sorted(set(persisted_by_uuid) - set(expected_by_uuid))
    changed_uuid = next(
        (
            row_uuid
            for row_uuid in sorted(set(persisted_by_uuid) & set(expected_by_uuid))
            if persisted_by_uuid[row_uuid] != expected_by_uuid[row_uuid]
        ),
        "",
    )
    changed_fields: list[str] = []
    if changed_uuid:
        actual_row = persisted_by_uuid[changed_uuid]
        expected_row = expected_by_uuid[changed_uuid]
        changed_fields = [
            field
            for index, field in enumerate(fields)
            if actual_row[index] != expected_row[index]
        ]
    logger.warning(
        "旧资源图指纹迁移核对失败 table=%s persisted=%d expected=%d "
        "missing=%s extra=%s changed_uuid=%s changed_fields=%s",
        table,
        len(persisted),
        len(expected),
        missing[:3],
        extra[:3],
        changed_uuid,
        changed_fields,
    )
    return False


def _is_site_node(node: Mapping[str, Any]) -> bool:
    """判断资源树节点是否表达库位（Site）。

    参数：``node`` 是规范 JSON 对象。返回：类型、类或配置类别命中库位集合时为真。
    异常：配置不是对象时由 JSON 校验原样关闭式失败。
    """

    config = _json_object(node.get("config"), "node.config")
    candidates = {
        str(node.get("type") or "").replace("-", "_").casefold(),
        str(node.get("class") or "").replace("-", "_").casefold(),
        str(config.get("category") or "").replace("-", "_").casefold(),
        str(config.get("type") or "").replace("-", "_").casefold(),
    }
    return bool(candidates & {value.replace("-", "_") for value in _SITE_TYPES})


def _site_content_types(
    node: Mapping[str, Any], aliases: Mapping[str, str]
) -> list[str]:
    """解析库位允许的资源模板业务身份。

    参数：节点和注册表别名映射。返回：去重且保持声明顺序的业务 ID 列表。
    异常：字段不是数组或包含未知模板时关闭式失败。
    """

    config = _json_object(node.get("config"), "site.config")
    values = config.get("content_type", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ResourceGraphBootstrapError("库位（Site）content_type 必须是数组")
    result: list[str] = []
    for value in values:
        alias = _required_text(value, "site.content_type")
        template_name = aliases.get(alias)
        if template_name is None:
            raise ResourceGraphBootstrapError(f"库位允许模板未进入注册表: {alias}")
        if template_name not in result:
            result.append(template_name)
    return result


def _pose(node: Mapping[str, Any]) -> dict[str, float]:
    """读取物料相对位置、尺寸、缩放和旋转。

    参数：资源树节点。返回：产品 ``relative_position`` 数值字段。异常：任一值
    非有限数、尺寸为负或缩放为负时抛出 ``ResourceGraphBootstrapError``；旧资源
    跟踪器（ResourceTracker）的显式零缩放按“未指定”规范化为单位缩放。
    """

    pose = _json_object(node.get("pose"), "node.pose")
    position = _json_object(pose.get("position"), "pose.position")
    size = _json_object(pose.get("size"), "pose.size")
    scale = _json_object(pose.get("scale"), "pose.scale")
    rotation = _json_object(pose.get("rotation"), "pose.rotation")
    result = {
        "position_x": _number(position.get("x"), "position.x"),
        "position_y": _number(position.get("y"), "position.y"),
        "position_z": _number(position.get("z"), "position.z"),
        "depth": _number(size.get("depth"), "size.depth"),
        "length": _number(size.get("height"), "size.height"),
        "width": _number(size.get("width"), "size.width"),
        "scale_x": _scale_number(scale.get("x", 1), "scale.x"),
        "scale_y": _scale_number(scale.get("y", 1), "scale.y"),
        "scale_z": _scale_number(scale.get("z", 1), "scale.z"),
        "rotation_x": _number(rotation.get("x"), "rotation.x"),
        "rotation_y": _number(rotation.get("y"), "rotation.y"),
        "rotation_z": _number(rotation.get("z"), "rotation.z"),
    }
    if any(result[key] < 0 for key in ("depth", "length", "width")):
        raise ResourceGraphBootstrapError("资源尺寸不得为负数")
    return result


def _site_pose(node: Mapping[str, Any]) -> dict[str, float]:
    """从资源位置中选择库位（Site）需要的坐标与尺寸。

    参数：资源树库位节点。返回：库位持久字段。异常：沿用 ``_pose`` 的数值校验。
    """

    pose = _pose(node)
    return {
        key: pose[key]
        for key in (
            "position_x",
            "position_y",
            "position_z",
            "depth",
            "length",
            "width",
        )
    }


def _fingerprint(
    source_name: str,
    _snapshot: RegistryTemplateSnapshot,
    projection: Mapping[str, Any],
) -> str:
    """计算资源图的规范 SHA-256 指纹。

    参数：来源、仅用于接口代际兼容的注册表快照，以及已解析
    投影。返回：``sha256:`` 前缀指纹。异常：非 JSON 值由
    ``json.dumps`` 原样拒绝并由公开入口包装。
    """

    # ``projection`` 已包含实际使用的稳定资源模板 UUID。设备动作
    # 合同虽会改变全注册表快照指纹，但不属于库存资源图。
    stable_projection = {
        section: [
            {
                **row,
                **(
                    {"meta_data": _stable_projection_meta_object(row["meta_data"])}
                    if "meta_data" in row
                    else {}
                ),
            }
            for row in rows
        ]
        for section, rows in projection.items()
    }
    payload = json.dumps(
        {
            "version": _FINGERPRINT_VERSION,
            "source_id": source_name,
            **stable_projection,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stable_projection_meta(value: object) -> str:
    """序列化不含进程内资源 UUID 的启动投影元数据。"""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
    else:
        decoded = value
    return _dump(_stable_projection_meta_object(decoded))


def _stable_projection_meta_object(value: object) -> dict[str, Any]:
    """移除只用于单次资源树关系解析的运行时元数据。"""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if key != "source_runtime_uuid"
    }


def _stable_uuid(source_name: str, domain: str, node_id: str) -> str:
    """生成跨重启稳定的库存身份。

    参数：资源图文件名、身份领域和节点 ID。返回：规范 UUID5 字符串。异常：无。
    """

    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"unilabos:{source_name}:{domain}:{node_id}")
    )


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    """读取一个启动元数据值。

    参数：当前事务连接与键。返回：存在时的字符串，否则 ``None``。异常：SQL 错误传播。
    """

    row = connection.execute(
        "SELECT meta_value FROM lab_meta WHERE meta_key=?", (key,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _json_object(value: object, field: str) -> dict[str, Any]:
    """复制并校验 JSON 对象。

    参数：可疑值与诊断字段名。返回：无共享引用字典。异常：非对象或非 JSON 值关闭式失败。
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ResourceGraphBootstrapError(f"{field} 必须是 JSON 对象")
    try:
        return json.loads(json.dumps(dict(value), allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ResourceGraphBootstrapError(f"{field} 不是合法 JSON") from error


def _required_text(value: object, field: str) -> str:
    """校验非空字符串。

    参数：可疑值与字段名。返回：去除首尾空白的字符串。异常：非法值关闭式失败。
    """

    if not isinstance(value, str) or not value.strip():
        raise ResourceGraphBootstrapError(f"{field} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object) -> str | None:
    """规范化可空字符串。

    参数：可疑值。返回：非空字符串或 ``None``。异常：非字符串非空值关闭式失败。
    """

    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ResourceGraphBootstrapError("可空文本字段必须是字符串")
    return value.strip() or None


def _number(value: object, field: str) -> float:
    """校验有限浮点数。

    参数：可疑数值与字段名。返回：有限浮点数。异常：布尔、无穷或非法值关闭式失败。
    """

    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ResourceGraphBootstrapError(f"{field} 必须是有限数值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ResourceGraphBootstrapError(f"{field} 必须是有限数值") from error
    if not math.isfinite(result):
        raise ResourceGraphBootstrapError(f"{field} 必须是有限数值")
    return result


def _scale_number(value: object, field: str) -> float:
    """规范化资源树缩放并兼容旧资源跟踪器零默认值。

    参数：``value`` 是可疑单轴缩放，``field`` 是诊断字段名。返回：正有限缩放；
    显式 ``0`` 返回单位缩放 ``1.0``。异常：负数、布尔值、无穷或非法数值抛出
    ``ResourceGraphBootstrapError``，确保 SQLite 的 ``scale_* > 0`` 约束成立。
    """

    result = _number(value, field)
    if result < 0:
        raise ResourceGraphBootstrapError(f"{field} 不得为负数")
    return 1.0 if result == 0 else result


def _dump(value: object) -> str:
    """序列化规范 JSON 列。

    参数：已校验 JSON 值。返回：紧凑 JSON 文本。异常：非法值由 JSON 库传播。
    """

    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


__all__ = [
    "ResourceGraphBootstrapError",
    "bootstrap_local_resource_graph",
]
