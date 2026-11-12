"""Graph Authority：管理命名设备图快照并导出实时拓扑。

图数据与物料权威共享 ``materials.db``：运行态节点和边分别来自
``material``、``material_link``，命名快照存于 ``lab_graph``。三个模型共用
同一连接、写锁和事务边界。

- 图名派生稳定 uuid（``GRAPH_NAMESPACE`` 的 uuid5），同名 upsert 即更新
  并递增 revision；软删除后重建复活同一 uuid；
- 读取按 uuid 与名称双通道解析（``unilab -g <uuid|名称>`` / CLI identity）；
- upsert 是唯一创建/更新入口，内含节点身份对账：草稿图（节点/Site 无
  uuid）由权威发号（按图 uuid + 节点 id 稳定派生），再次上传复用既有
  身份；携带 uuid 的图与既有记录冲突时拒绝。返回 ``summary``
  （新建/更新/移除/不变/发号统计）；
- ``live_payload`` 从物料权威实时序列化当前真实拓扑（节点 + 边），
  与快照 payload（上传时刻存档）互补；
- ``load_graph_payload`` 把权威 payload 装配成 Host 启动结构（``ResourceTreeSet``、
  backend 连线、协议编译器用的物理拓扑图），``unilab -g`` 只走这一条装配路径。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple, Optional, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from unilabos.protocol.base import canonical_hash
from unilabos.resources.objects.site import normalize_available_sites
from unilabos.server.services.materials.store import MaterialsRepository
from unilabos.server.database.tables.materials import (
    LabGraphRecord,
    MaterialLinkRecord,
)

if TYPE_CHECKING:
    import networkx as nx

    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.server.services.materials import MaterialsService

#: 图名 -> uuid5 的稳定命名空间；跨机器/跨次启动同名同 uuid。
GRAPH_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://graph-authority")


class GraphError(Exception):
    """业务错误：``code`` 供 API/CLI 映射 envelope 业务码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def graph_uuid_for_name(name: str) -> str:
    return str(uuid5(GRAPH_NAMESPACE, name.strip().lower()))


def validate_graph_payload(payload: Any) -> dict[str, Any]:
    """结构校验：node-link JSON 必须是含 ``nodes`` 数组的对象。"""

    if not isinstance(payload, dict):
        raise GraphError("invalid_payload", "graph payload 必须是 JSON 对象")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise GraphError("invalid_payload", "graph payload 必须包含 nodes 数组")
    links = payload.get("links")
    if links is not None and not isinstance(links, list):
        raise GraphError("invalid_payload", "graph links 必须是数组")
    return payload


def _node_template_name(node: Mapping[str, Any]) -> str:
    """推导 Site 归属的模板名：根字段 template_name > config.type（deck 等 PLR 物料）。"""

    config = node.get("config") if isinstance(node.get("config"), Mapping) else {}
    for candidate in (node.get("template_name"), config.get("type")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _is_plr_flat_site(site: Mapping[str, Any]) -> bool:
    """PLR 平铺 Site（图纸形态）没有 canonical 身份字段。"""

    return not any(
        key in site for key in ("schema_version", "uuid", "material_uuid", "template_name")
    )


def _canonicalize_site(
    site: Mapping[str, Any],
    *,
    ordinal: int,
    node_uuid: str,
    template_name: str,
    previous_by_label: Mapping[str, Mapping[str, Any]],
    node_id: str,
) -> dict[str, Any]:
    """把草稿 Site（PLR 平铺或缺身份的 canonical）补齐为权威 ResourceSite 形状。

    身份字段发号规则与节点一致：优先复用既有快照同 label 的 uuid，
    否则按节点 uuid + label 稳定派生。
    """

    label = str(site.get("label") or "").strip()
    if not label:
        raise GraphError(
            "invalid_payload", f"节点 {node_id} 的 Site[{ordinal}] 缺少 label"
        )
    # 占用关系只认 uuid；按占用物料 name 表达的 occupied_by 不属于当前契约。
    occupied = site.get("occupied_by")
    if occupied is not None:
        raise GraphError(
            "invalid_payload",
            f"节点 {node_id} 的 Site {label} 使用了 occupied_by；"
            "占用关系请提供 occupied_material_uuid",
        )

    previous = previous_by_label.get(label) or {}
    site_uuid = (
        str(site.get("uuid") or "").strip()
        or str(previous.get("uuid") or "").strip()
        or str(uuid5(UUID(node_uuid), f"site:{label}"))
    )

    if _is_plr_flat_site(site):
        position = site.get("position") if isinstance(site.get("position"), Mapping) else {}
        size = site.get("size") if isinstance(site.get("size"), Mapping) else {}
        point = {axis: float(position.get(axis, 0.0) or 0.0) for axis in ("x", "y", "z")}
        pose: dict[str, Any] = {
            "size": {
                "width": float(size.get("width", 0.0) or 0.0),
                "height": float(size.get("height", 0.0) or 0.0),
                "depth": float(size.get("depth", 0.0) or 0.0),
            },
            "position": dict(point),
            "position3d": dict(point),
        }
        allowed = list(site.get("content_type") or site.get("allowed_resource_categories") or [])
        canonical: dict[str, Any] = {
            "schema_version": 1,
            "uuid": site_uuid,
            "template_name": template_name,
            "material_uuid": node_uuid,
            "index": site.get("index", ordinal),
            "label": label,
            "visible": bool(site.get("visible", True)),
            "occupied_material_uuid": site.get("occupied_material_uuid"),
            "pose": pose,
            "allowed_resource_categories": allowed,
            "parent_link": str(site.get("parent_link") or label),
            "description": str(site.get("description") or ""),
            "meta_data": dict(site.get("meta_data") or {}),
            "extra": dict(site.get("extra") or {}),
        }
        return canonical

    canonical = copy.deepcopy(dict(site))
    canonical.setdefault("schema_version", 1)
    canonical["uuid"] = site_uuid
    canonical["material_uuid"] = str(canonical.get("material_uuid") or "").strip() or node_uuid
    canonical["template_name"] = (
        str(canonical.get("template_name") or "").strip() or template_name
    )
    canonical.setdefault("index", ordinal)
    canonical.setdefault("label", label)
    return canonical


def reconcile_graph_payload(
    payload: Mapping[str, Any],
    previous_payload: Optional[Mapping[str, Any]],
    graph_uuid: str,
    device_site_templates: Optional[Mapping[str, Sequence[Any]]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """节点身份对账：草稿发号、既有身份复用、冲突拒绝，并产出 diff 摘要。

    - 节点 uuid：payload 携带 > 既有快照同 id 节点 > ``uuid5(图 uuid, "node:<id>")``
      稳定派生（同一草稿反复导入结果一致）；
    - payload 与既有快照同 id 节点 uuid 不一致 → ``identity_conflict`` 拒绝；
    - 层级只认 ``parent``/``parent_uuid``；派生的 ``children`` 列表剥离不入库；
    - 只接受当前 node-link 契约：根级 ``position``、Site ``occupied_by`` 等旧字段
      直接拒绝（旧后端导出图请先经 legacy 适配层转换）；
    - Site 身份（uuid/material_uuid/template_name）随所属节点补齐；
      ``config.sites`` 的 PLR 平铺图纸转为根级 canonical 快照；
    - ``device_site_templates``（注册表 ``template_name -> available_sites``）
      提供时，未声明 sites 的设备节点在此实例化模板快照（含权威空快照
      ``sites_initialized=True, sites=[]``），满足 Edge「不得本地补齐」契约；
    - summary: created/updated/removed/unchanged 节点 id 列表 +
      ``uuid_assigned`` 发号计数。
    """

    normalized = copy.deepcopy(dict(payload))
    nodes = normalized.get("nodes") or []
    previous_by_id: dict[str, Mapping[str, Any]] = {}
    previous_by_uuid: dict[str, Mapping[str, Any]] = {}
    for node in (previous_payload or {}).get("nodes") or []:
        node_id = str(node.get("id") or "").strip()
        node_uuid = str(node.get("uuid") or "").strip()
        if node_id:
            previous_by_id.setdefault(node_id, node)
        if node_uuid:
            previous_by_uuid[node_uuid] = node

    for node in nodes:
        if not isinstance(node, dict):
            raise GraphError("invalid_payload", "graph 节点必须是对象")
        node_id = str(node.get("id") or node.get("name") or "").strip()
        if not node_id:
            raise GraphError("invalid_payload", "graph 节点缺少 id")
        if "position" in node:
            raise GraphError(
                "invalid_payload",
                f"节点 {node_id} 的根字段 position 不受支持，请使用 pose.position",
            )
    # 不同父节点下的子物料可以同名（多块板各有 A1 孔）：这类节点以携带的
    # uuid 为身份；id 唯一的节点仍按 id 与既有快照对账。
    id_counts: dict[str, int] = {}
    for node in nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        id_counts[node_id] = id_counts.get(node_id, 0) + 1

    def previous_for(node: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        node_uuid = str(node.get("uuid") or "").strip()
        if id_counts.get(node_id, 0) > 1:
            return previous_by_uuid.get(node_uuid) if node_uuid else None
        return previous_by_id.get(node_id)

    seen_uuids: set[str] = set()
    uuid_assigned = 0
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []

    for node in nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        incoming_uuid = str(node.get("uuid") or "").strip()
        if id_counts[node_id] > 1 and not incoming_uuid:
            raise GraphError("invalid_payload", f"graph 节点 id 重复: {node_id}")

        previous = previous_for(node)
        previous_uuid = str((previous or {}).get("uuid") or "").strip()
        if incoming_uuid and previous_uuid and incoming_uuid != previous_uuid:
            raise GraphError(
                "identity_conflict",
                f"节点 {node_id} 的 uuid ({incoming_uuid}) 与权威已登记 uuid "
                f"({previous_uuid}) 不一致；如需重建身份请先删除图或移除 uuid 字段",
            )
        node_uuid = incoming_uuid or previous_uuid
        if not node_uuid:
            node_uuid = str(uuid5(UUID(graph_uuid), f"node:{node_id}"))
            uuid_assigned += 1
        if node_uuid in seen_uuids:
            raise GraphError("invalid_payload", f"graph 节点 uuid 重复: {node_uuid}")
        seen_uuids.add(node_uuid)
        node["uuid"] = node_uuid

    for node in nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        node_uuid = str(node["uuid"])
        previous = previous_for(node)

        # 父子关系只由 parent/parent_uuid 表达；children 是派生列表，不入库。
        node.pop("children", None)

        # ── Site 身份补齐（config.sites 平铺图纸 → 根级 canonical） ──
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        raw_sites = node.get("sites")
        config_sites = config.get("sites") if isinstance(config, dict) else None
        if raw_sites is None and isinstance(config_sites, list):
            raw_sites = config_sites
            config.pop("sites", None)
        if (
            raw_sites is None
            and device_site_templates is not None
            and str(node.get("type") or "") == "device"
        ):
            # 设备节点未声明 Site：按注册表模板在创建入口实例化（微后端边界）。
            template_key = _node_template_name(node)
            template_sites = normalize_available_sites(
                device_site_templates.get(template_key)
            )
            if template_sites:
                raw_sites = template_sites
            else:
                # 权威空快照：设备无模板位点时同样必须显式初始化。
                node["sites"] = []
                node["sites_initialized"] = True
        if isinstance(raw_sites, list) and raw_sites:
            template_name = _node_template_name(node)
            if not template_name:
                raise GraphError(
                    "invalid_payload",
                    f"节点 {node_id} 声明了 sites 但缺少 template_name/class/config.type，"
                    "无法确定 Site 模板归属",
                )
            previous_by_label = {
                str(site.get("label") or ""): site
                for site in (previous or {}).get("sites") or []
                if isinstance(site, Mapping)
            }
            before = [copy.deepcopy(site) for site in raw_sites]
            node["sites"] = [
                _canonicalize_site(
                    site,
                    ordinal=ordinal,
                    node_uuid=node_uuid,
                    template_name=template_name,
                    previous_by_label=previous_by_label,
                    node_id=node_id,
                )
                for ordinal, site in enumerate(raw_sites, start=1)
            ]
            if node["sites"] != before:
                uuid_assigned += sum(
                    1 for raw in before if not str(raw.get("uuid") or "").strip()
                )
            node["sites_initialized"] = True
            if not str(node.get("template_name") or "").strip():
                node["template_name"] = template_name

    # ── 占用关系对账：子物料按 parent + 位置匹配写入 Site 占用。──
    # 权威快照必须自带占用（树重建/deserialize 时 child 尚未注入 uuid，
    # 依赖 Site 的 expected_occupant 回填），创建入口是唯一发号边界。
    for node in nodes:
        sites = node.get("sites")
        if not isinstance(sites, list) or not sites:
            continue
        node_id = str(node.get("id") or node.get("name") or "")
        node_uuid = str(node["uuid"])
        for child in nodes:
            child_parent_uuid = str(child.get("parent_uuid") or "")
            if child_parent_uuid:
                if child_parent_uuid != node_uuid:
                    continue
            elif str(child.get("parent") or "") != node_id:
                continue
            child_position = ((child.get("pose") or {}).get("position")) or {}
            child_key = tuple(
                float(child_position.get(axis, 0.0) or 0.0) for axis in ("x", "y", "z")
            )
            for site in sites:
                if site.get("occupied_material_uuid"):
                    continue
                site_position = ((site.get("pose") or {}).get("position")) or {}
                site_key = tuple(
                    float(site_position.get(axis, 0.0) or 0.0) for axis in ("x", "y", "z")
                )
                if site_key == child_key:
                    site["occupied_material_uuid"] = child["uuid"]
                    break

    # ── diff 分类（发号与占用全部就绪后计算）──
    matched_previous_uuids: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id") or node.get("name") or "")
        previous = previous_for(node)
        if previous is None:
            created.append(node_id)
            continue
        matched_previous_uuids.add(str(previous.get("uuid") or ""))
        if canonical_hash(node) == canonical_hash(dict(previous)):
            unchanged.append(node_id)
        else:
            updated.append(node_id)

    removed = sorted(
        str(node.get("id") or "")
        for node_uuid, node in previous_by_uuid.items()
        if node_uuid not in matched_previous_uuids
    )
    summary = {
        "created": created,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "uuid_assigned": uuid_assigned,
    }
    return normalized, summary


def _link_key(link: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(link.get("source") or link.get("source_uuid") or ""),
        str(link.get("target") or link.get("target_uuid") or ""),
        str(link.get("sourceHandle") or ""),
        str(link.get("targetHandle") or ""),
    )


def adopt_graph_payload(
    payload: Mapping[str, Any], previous_payload: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """「权威优先」合并：既有节点/连线一律沿用权威版本，只把权威没有的补进来。

    与 ``materials.ensure`` 对启动物料的处理一致——启动文件是创建入口，不是运行
    真相：文件对既有节点的修改（pose / config / 删除）不生效；文件里带的 uuid 与
    权威同 id 节点不一致，或与权威另一节点撞 uuid，都是身份冲突，拒绝。

    返回 (合并后的 payload, {"adopted": [...], "kept": [...], "added": [...]})：
    ``adopted`` 文件里也有的权威节点、``kept`` 只在权威里的节点、``added`` 只在
    文件里的节点（随后由 reconcile 发号 / 实例化模板 Site）。
    """

    previous_nodes = [
        node for node in (previous_payload.get("nodes") or []) if isinstance(node, Mapping)
    ]
    previous_by_id: dict[str, list[Mapping[str, Any]]] = {}
    previous_by_uuid: dict[str, Mapping[str, Any]] = {}
    for node in previous_nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        node_uuid = str(node.get("uuid") or "").strip()
        if node_id:
            previous_by_id.setdefault(node_id, []).append(node)
        if node_uuid:
            previous_by_uuid[node_uuid] = node

    local_nodes = [node for node in (payload.get("nodes") or []) if isinstance(node, Mapping)]
    adopted: list[str] = []
    added_nodes: list[dict[str, Any]] = []
    matched_previous: set[str] = set()
    for node in local_nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        if not node_id:
            raise GraphError("invalid_payload", "graph 节点缺少 id")
        node_uuid = str(node.get("uuid") or "").strip()
        candidates = previous_by_id.get(node_id, [])
        match: Optional[Mapping[str, Any]] = None
        if node_uuid and node_uuid in previous_by_uuid:
            match = previous_by_uuid[node_uuid]
            if str(match.get("id") or match.get("name") or "").strip() != node_id:
                raise GraphError(
                    "identity_conflict",
                    f"节点 {node_id} 的 uuid ({node_uuid}) 已属于权威节点 "
                    f"{match.get('id')}；启动文件与权威身份不一致",
                )
        elif len(candidates) == 1:
            match = candidates[0]
            previous_uuid = str(match.get("uuid") or "").strip()
            if node_uuid and previous_uuid and node_uuid != previous_uuid:
                raise GraphError(
                    "identity_conflict",
                    f"节点 {node_id} 的 uuid ({node_uuid}) 与权威已登记 uuid "
                    f"({previous_uuid}) 不一致；如需重建身份请先删除图或移除 uuid 字段",
                )
        elif len(candidates) > 1:
            raise GraphError(
                "invalid_payload",
                f"权威中 id={node_id} 有多个节点，启动文件必须带 uuid 才能对应",
            )
        if match is not None:
            adopted.append(node_id)
            matched_previous.add(str(match.get("uuid") or ""))
        else:
            added_nodes.append(copy.deepcopy(dict(node)))

    kept = [
        str(node.get("id") or node.get("name") or "")
        for node in previous_nodes
        if str(node.get("uuid") or "") not in matched_previous
    ]

    previous_links = [
        link for link in (previous_payload.get("links") or []) if isinstance(link, Mapping)
    ]
    known_links = {_link_key(link) for link in previous_links}
    added_links = [
        copy.deepcopy(dict(link))
        for link in (payload.get("links") or [])
        if isinstance(link, Mapping) and _link_key(link) not in known_links
    ]

    merged: dict[str, Any] = copy.deepcopy(dict(previous_payload))
    for key, value in payload.items():
        if key not in merged and key not in ("nodes", "links"):
            merged[key] = copy.deepcopy(value)
    merged["nodes"] = [copy.deepcopy(dict(node)) for node in previous_nodes] + added_nodes
    merged["links"] = [copy.deepcopy(dict(link)) for link in previous_links] + added_links
    return merged, {
        "adopted": adopted,
        "kept": kept,
        "added": [str(node.get("id") or node.get("name") or "") for node in added_nodes],
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def link_payload(record: MaterialLinkRecord, uuid_to_id: Mapping[str, str]) -> dict[str, Any]:
    """把一行 material_link 还原成 node-link 的 link 对象。"""

    link: dict[str, Any] = {
        "source": uuid_to_id.get(
            record.source_material_uuid, record.source_material_uuid
        ),
        "target": uuid_to_id.get(
            record.target_material_uuid, record.target_material_uuid
        ),
        "source_uuid": record.source_material_uuid,
        "target_uuid": record.target_material_uuid,
    }
    if record.link_type:
        link["type"] = record.link_type
    if record.source_handle:
        link["sourceHandle"] = record.source_handle
    if record.target_handle:
        link["targetHandle"] = record.target_handle
    for key, value in record.extra_json.items():
        link.setdefault(key, value)
    return link


class GraphService(MaterialsRepository):
    """管理图快照，并从物料权威生成实时拓扑。

    ``database`` 通常接收 :class:`MaterialsService` 实例，以共享
    ``materials.db`` 的连接和写锁；独立测试也可传入数据库路径。
    """

    def __init__(
        self,
        database: "str | Path | MaterialsRepository",
        materials: "Optional[MaterialsService]" = None,
    ):
        super().__init__(database)
        if materials is None:
            from unilabos.server.services.materials.core import MaterialsService

            if isinstance(database, MaterialsService):
                materials = database
        self._materials = materials

    # ── 读形态 ──────────────────────────────────────────────

    @staticmethod
    def _read(record: LabGraphRecord) -> dict[str, Any]:
        return {
            "uuid": record.uuid,
            "name": record.name,
            "description": record.description,
            "tags": list(record.tags),
            "meta_data": dict(record.meta_data),
            "payload": record.payload,
            "revision": record.revision,
            "create_time": record.create_time,
            "update_time": record.update_time,
        }

    @staticmethod
    def _summary(record: LabGraphRecord) -> dict[str, Any]:
        nodes = record.payload.get("nodes")
        return {
            "uuid": record.uuid,
            "name": record.name,
            "description": record.description,
            "tags": list(record.tags),
            "meta_data": dict(record.meta_data),
            "revision": record.revision,
            "create_time": record.create_time,
            "update_time": record.update_time,
            "node_count": len(nodes) if isinstance(nodes, list) else 0,
        }

    def _resolve(self, identity: str) -> LabGraphRecord:
        identity = (identity or "").strip()
        if not identity:
            raise GraphError("invalid_input", "graph 标识不能为空")
        record = MaterialsRepository.get_graph(self, identity)
        if record is None:
            record = self.find_graph_by_name(identity)
        if record is None:
            raise GraphError("not_found", f"graph 不存在: {identity}")
        return record

    # ── CRUD ───────────────────────────────────────────────

    def upsert_graph(
        self,
        *,
        name: str,
        payload: Any,
        uuid: Optional[str] = None,
        tags: Sequence[Any] = (),
        description: Optional[str] = None,
        meta_data: Optional[Mapping[str, Any]] = None,
        device_site_templates: Optional[Mapping[str, Sequence[Any]]] = None,
        on_existing: str = "replace",
    ) -> dict[str, Any]:
        """创建/更新图快照（唯一入口）：先做节点身份对账再落库。

        草稿图（节点/Site 无 uuid）由本方法发号；``device_site_templates``
        提供时设备节点的模板 Site 也在此实例化。返回值携带 ``summary``
        （created/updated/removed/unchanged 节点 id 与 uuid_assigned 计数，以及
        ``existing`` 表示落库前权威是否已有该图）。与既有快照内容一致时不递增 revision。

        ``on_existing`` 决定权威已有同名/同 uuid 图时怎么对待传入 payload：

        - ``"replace"``（前端 / CLI 上传即编辑）：以传入 payload 为准，既有节点按 id
          复用身份，文件里删掉的节点随之移除；
        - ``"adopt"``（``unilab -g <文件>`` 启动）：以权威为准，只把权威没有的节点 /
          连线补进来，既有节点沿用权威版本；带 uuid 且与权威不一致即身份冲突拒绝。
          与 ``materials.ensure`` 对启动物料的语义一致。
        """

        if on_existing not in ("replace", "adopt"):
            raise GraphError("invalid_input", f"on_existing 取值非法: {on_existing!r}")
        name = (name or "").strip()
        if not name:
            raise GraphError("invalid_input", "graph 名称不能为空")
        validated = validate_graph_payload(payload)
        graph_uuid = (uuid or "").strip() or graph_uuid_for_name(name)

        with self.write():
            # 含软删除记录：同名/同 uuid 重建走复活而不是主键冲突。
            existing = MaterialsRepository.get_graph(self, graph_uuid, include_deleted=True)
            if existing is None:
                # uuid 未命中时回退名称唯一键，避免显式 uuid 与既有同名记录冲突。
                existing = self.find_graph_by_name(name)

            previous_payload = existing.payload if existing is not None and existing.deleted_at is None else None
            adoption: dict[str, Any] = {}
            incoming: Mapping[str, Any] = validated
            if on_existing == "adopt" and previous_payload is not None:
                incoming, adoption = adopt_graph_payload(validated, previous_payload)
            normalized, summary = reconcile_graph_payload(
                incoming,
                previous_payload,
                existing.uuid if existing is not None else graph_uuid,
                device_site_templates=device_site_templates,
            )
            summary["existing"] = previous_payload is not None
            summary.update(adoption)

            now = _now()
            if existing is not None:
                if previous_payload is not None and canonical_hash(previous_payload) == canonical_hash(normalized):
                    # 内容一致：不递增 revision，直接复用现存快照。
                    result = self._read(existing)
                    result["summary"] = summary
                    return result
                record = existing.model_copy(
                    update={
                        "update_time": now,
                        "deleted_at": None,
                        "name": name,
                        "tags": list(tags),
                        "description": description,
                        "meta_data": dict(meta_data or {}),
                        "payload": normalized,
                        "revision": existing.revision + 1,
                    }
                )
                self.update_graph(record)
            else:
                record = LabGraphRecord(
                    uuid=graph_uuid,
                    create_time=now,
                    update_time=now,
                    name=name,
                    tags=list(tags),
                    description=description,
                    meta_data=dict(meta_data or {}),
                    payload=normalized,
                    revision=1,
                )
                self.insert_graph(record)
        result = self._read(record)
        result["summary"] = summary
        return result

    def get_graph(self, identity: str) -> dict[str, Any]:
        return self._read(self._resolve(identity))

    def get_payload(self, identity: str) -> dict[str, Any]:
        return self._resolve(identity).payload

    def list_graphs(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        name: str = "",
    ) -> dict[str, Any]:
        records, total = MaterialsRepository.list_graphs(self, 
            page=page, page_size=page_size, name=name
        )
        return {
            "items": [self._summary(record) for record in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def delete_graph(self, identity: str) -> None:
        record = self._resolve(identity)
        with self.write():
            self.soft_delete_graph(record.uuid, deleted_at=_now())

    # ── 当前态导出 ──────────────────────────────────────────

    def live_payload(self) -> dict[str, Any]:
        """从物料权威实时序列化当前拓扑（node-link JSON）。

        与快照 payload 的差异：快照是上传/启动时刻的存档（可回滚、可分发），
        这里给的是运行演化后的真实实验室——节点来自 material 聚合树，
        边来自 material_link 表。
        """

        if self._materials is None:
            raise GraphError(
                "unsupported", "当前进程未装配物料服务，无法导出实时拓扑"
            )
        from unilabos.resources.adapters.plr_materials import (
            material_tree_to_resource_tree,
        )

        nodes: list[dict[str, Any]] = []
        uuid_to_id: dict[str, str] = {}
        for aggregate in self._materials.list_materials(roots_only=True):
            tree_read = self._materials.get_tree(
                aggregate.material.material_uuid
            )
            tree_set = material_tree_to_resource_tree(tree_read)
            for node in tree_set.all_nodes:
                content = node.res_content
                payload = content.model_dump(by_alias=True)
                # ResourceDict 的 parent 字段是对象引用、导出时被排除，node-link 契约里
                # 层级要靠 parent（父 id）表达；不补上，前端就把设备下的台面 / 耗材都画成
                # 独立根节点，设备卡片里看不到运行期挂上去的 sites。
                payload["parent"] = (
                    content.parent.id if content.parent is not None else None
                )
                nodes.append(payload)
                uuid_to_id[content.uuid] = content.id
        links = [
            link_payload(record, uuid_to_id)
            for record in MaterialsRepository.list_links(self)
        ]
        return {"nodes": nodes, "links": links}


# ── 运行时装配：权威 payload → Host 启动结构 ─────────────────────────
#
# ``unilab -g`` 的读取链路：本地 JSON 先经 legacy 适配层升级旧格式、再由
# :meth:`GraphService.upsert_graph` 对账发号；Host 拿到的永远是权威 payload，
# 由 :func:`load_graph_payload` 装配成资源树、backend 连线与协议编译器用的
# 物理拓扑图。GraphML 不再支持。

#: 设备与通信设备之间 ``communication`` 边可识别的通信设备模板。
_COMMUNICATION_TEMPLATES = ("serial", "io_device", "plc", "io")


class LoadedGraph(NamedTuple):
    """:func:`load_graph_payload` 的结果。"""

    #: 设备与物料资源树（Host 的 ``devices_config`` / ``resources_config``）。
    resource_tree_set: "ResourceTreeSet"
    #: backend 形态的连线（``sourceHandle`` / ``targetHandle`` / ``id``）。
    links: list[dict[str, Any]]
    #: 协议编译器（:mod:`unilabos.experiments.compile`）读取的物理拓扑图。
    physical_setup_graph: "nx.Graph"


def _graph_resource_node(node: dict[str, Any]) -> dict[str, Any]:
    """把 payload 节点就地整理成 ``ResourceDict`` 输入并返回。

    - 必须带 uuid（或 ``data.unilabos_uuid``）；``children`` 是派生字段，丢弃；
    - 缺 ``type`` 视为设备，缺 ``name`` 取 ``id``；
    - 根级 ``position`` 不属于当前契约；根级 ``x/y/z`` 折叠进 ``pose.position``；
    - ``sample_id`` 不受支持，丢弃；其余非 ``ResourceDict`` 根字段收进 ``config``。
    """

    from unilabos.resources.objects.resource import RESOURCE_ROOT_FIELDS
    from unilabos.utils.banner_print import print_status
    from unilabos.utils.log import logger

    label = node.get("id", node.get("name"))
    if not node.get("uuid"):
        transport_uuid = (node.get("data") or {}).get("unilabos_uuid")
        if not transport_uuid:
            raise ValueError(f"资源 {label} 缺少 UUID；-g strict import 必须携带资源/Site UUID")
        node["uuid"] = str(transport_uuid)
    # 父子关系只由 parent/parent_uuid 表达；children 是派生字段，读取时丢弃。
    node.pop("children", None)
    if not isinstance(node.get("config"), dict):
        node["config"] = {}
    if not node.get("type"):
        node["type"] = "device"
        print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'type', defaulting to 'device'", "warning")
    if node.get("name", None) is None:
        node["name"] = node.get("id")
        print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'name', defaulting to {node['name']}", "warning")
    if "position" in node:
        raise ValueError(f"资源 {label} 的根字段 position 不受支持，请使用 pose.position")
    legacy_position = {axis: node.pop(axis) for axis in ("x", "y", "z") if node.get(axis) is not None}
    if legacy_position:
        raw_pose = copy.deepcopy(node.get("pose") or {})
        if not isinstance(raw_pose, dict):
            raise ValueError(f"资源 {label} 的 pose 必须是对象")
        if raw_pose.get("position") not in (None, legacy_position):
            raise ValueError(f"资源 {label} 的 pose.position 与旧 x/y/z 坐标冲突")
        raw_pose["position"] = legacy_position
        node["pose"] = raw_pose
    if "sample_id" in node:
        sample_id = node.pop("sample_id")
        if sample_id:
            logger.error(f"{node} 的 sample_id 参数不受支持，已忽略: {sample_id}")
    for key in list(node.keys()):
        if key not in RESOURCE_ROOT_FIELDS:
            node["config"][key] = node.pop(key)
    return node


def graph_resource_tree(nodes: list[dict[str, Any]]) -> "ResourceTreeSet":
    """payload 节点列表 → ``ResourceTreeSet``（层级按 ``parent_uuid`` / ``parent``）。

    节点就地整理（见 :func:`_graph_resource_node`）；任何节点不合法都整体拒绝。
    """

    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.utils.banner_print import print_status

    print_status(f"{len(nodes)} Resources loaded", "info")
    return ResourceTreeSet.from_raw_dict_list([_graph_resource_node(node) for node in nodes])


def _canonicalize_link_ports(
    links: list[dict[str, Any]], resource_tree_set: "ResourceTreeSet"
) -> list[dict[str, Any]]:
    """补齐边的端点 id / uuid，并把 ``port`` 规范成 ``{端点 id: 端口}``。

    - 只带 ``source``/``target`` 或只带 ``*_uuid`` 的边按节点集合补出另一方；
      端点不在节点集合中的边丢弃；
    - ``port`` 为 ``"A"`` / ``"(A,B)"`` / 整数时转成字典，非法形态置空；
      带 ``port`` 的 ``physical`` 边视为 ``fluid``；
    - 一端端口缺失时取反向边的端口并丢弃本边；没有反向边则两端同名。
    """

    from unilabos.utils.log import logger

    id_to_uuid: dict[str, str] = {}
    uuid_to_id: dict[str, str] = {}
    for node in resource_tree_set.all_nodes:
        id_to_uuid[node.res_content.id] = node.res_content.uuid
        uuid_to_id[node.res_content.uuid] = node.res_content.id

    resolved: list[dict[str, Any]] = []
    for link in links:
        source_id = link.get("source")
        target_id = link.get("target")
        if source_id and source_id in id_to_uuid:
            link["source_uuid"] = id_to_uuid[source_id]
        if target_id and target_id in id_to_uuid:
            link["target_uuid"] = id_to_uuid[target_id]
        source_uuid = link.get("source_uuid")
        target_uuid = link.get("target_uuid")
        if source_uuid and source_uuid in uuid_to_id:
            link["source"] = uuid_to_id[source_uuid]
        if target_uuid and target_uuid in uuid_to_id:
            link["target"] = uuid_to_id[target_uuid]
        if not link.get("source") or not link.get("target"):
            logger.warning(f"[Graph] 边 {link} 的端点不在节点集合中，已忽略")
            continue
        resolved.append(link)

    for link in resolved:
        port = link.get("port")
        if port is None:
            continue
        if link.get("type", "physical") == "physical":
            link["type"] = "fluid"
        if isinstance(port, int):
            port = str(port)
        if isinstance(port, str):
            port_str = port.strip()
            if port_str.startswith("(") and port_str.endswith(")"):
                # "(A,B)"：两端端口
                parts = [part.strip() for part in port_str[1:-1].strip().split(",", 1)]
                source_port = parts[0]
                target_port = parts[1] if len(parts) > 1 else None
            else:
                # "A"：只有 source 端口
                source_port = port_str
                target_port = None
            link["port"] = {link["source"]: source_port, link["target"]: target_port}
        elif not isinstance(port, dict):
            link["port"] = {link["source"]: None, link["target"]: None}

    ports_by_edge = {(link["source"], link["target"]): link["port"] for link in resolved if link.get("port")}
    # 借用了反向边端口的边随后丢弃（端口信息已并入反向边那一侧的图边）。
    dropped: set[int] = set()
    for index, link in enumerate(resolved):
        source, target = link["source"], link["target"]
        current_port = link.get("port")
        if current_port is None or current_port.get(target) is not None:
            continue
        reverse_port = ports_by_edge.get((target, source))
        if reverse_port:
            reverse_source = reverse_port.get(source)
            if reverse_source is not None:
                current_port[target] = reverse_source
                dropped.add(index)
        else:
            current_port[target] = current_port[source]
    return [link for index, link in enumerate(resolved) if index not in dropped]


def _apply_communication_links(graph: "nx.Graph") -> None:
    """``communication`` 边把通信设备写进设备 config（串口名 / IO 端口号）。"""

    from unilabos.utils.log import logger

    for (left, right), edge in graph.edges.items():
        if edge.get("type", "physical") != "communication":
            continue
        if graph.nodes[left].get("template_name") in _COMMUNICATION_TEMPLATES:
            device_comm, device = left, right
        elif graph.nodes[right].get("template_name") in _COMMUNICATION_TEMPLATES:
            device_comm, device = right, left
        else:
            continue
        comm_template = graph.nodes[device_comm].get("template_name")
        if comm_template == "serial":
            graph.nodes[device]["config"]["port"] = device_comm
        elif comm_template == "io_device":
            logger.warning(f"Modify {device}'s io_device_port to {edge['port'][device_comm]}")
            graph.nodes[device]["config"]["io_device_port"] = int(edge["port"][device_comm])


def _backend_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范边 → backend 连线：``port`` 拆成 ``sourceHandle`` / ``targetHandle`` 并生成边 id。"""

    for edge in links:
        port = edge.pop("port", {})
        source = edge["source"]
        target = edge["target"]
        if source in port:
            edge["sourceHandle"] = port[source]
        elif "source_port" in edge:
            edge["sourceHandle"] = edge.pop("source_port")
        elif "source_handle" in edge:
            edge["sourceHandle"] = edge.pop("source_handle")
        elif edge.get("type") == "communication":
            continue
        if target in port:
            edge["targetHandle"] = port[target]
        elif "target_port" in edge:
            edge["targetHandle"] = edge.pop("target_port")
        elif "target_handle" in edge:
            edge["targetHandle"] = edge.pop("target_handle")
        elif edge.get("type") == "communication":
            continue
        edge["id"] = f"reactflow__edge-{source}-{edge['sourceHandle']}-{target}-{edge['targetHandle']}"
        for key in ("source_port", "target_port"):
            edge.pop(key, None)
    return links


def load_graph_payload(payload: Mapping[str, Any], *, machine_name: str) -> LoadedGraph:
    """把 Graph Authority 的 node-link payload 装配成 Host 启动结构。

    ``payload`` 原地使用（节点 / 边会被整理），调用方需要保留原件时自行拷贝。
    ``machine_name`` 写到未声明归属机器的设备树节点上（物料归权威管理，不写）。
    """

    import networkx as nx

    resource_tree_set = graph_resource_tree(list(payload.get("nodes") or []))
    links = _canonicalize_link_ports(
        list(payload.get("links", payload.get("edges")) or []), resource_tree_set
    )
    graph = nx.node_link_graph(
        {
            "nodes": [node.res_content.model_dump(by_alias=True) for node in resource_tree_set.all_nodes],
            "links": links,
        },
        edges="links",
        multigraph=False,
    )
    _apply_communication_links(graph)

    for tree in resource_tree_set.trees:
        if tree.root_node.res_content.type != "device":
            continue
        for node in tree.get_all_nodes():
            if not node.res_content.machine_name:
                node.res_content.machine_name = machine_name

    return LoadedGraph(resource_tree_set, _backend_links(links), graph)


__all__ = [
    "GRAPH_NAMESPACE",
    "GraphError",
    "GraphService",
    "LoadedGraph",
    "adopt_graph_payload",
    "graph_resource_tree",
    "graph_uuid_for_name",
    "link_payload",
    "load_graph_payload",
    "reconcile_graph_payload",
    "validate_graph_payload",
]
