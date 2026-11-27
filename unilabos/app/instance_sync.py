"""通过正式后端 API 幂等初始化设备图中的实际资源实例。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

from unilabos.app.instance_geometry import (
    InstanceGeometryError,
    default_host_relative_position,
    relative_position_from_graph_node,
)
from unilabos.app.instance_position_sync import (
    InstancePositionSyncError,
    position_update_request,
)
from unilabos.resources.instance_identity import normalize_resource_instance_barcode
from unilabos.utils.tracing import inject_trace_context, span

INSTANCE_TOKEN_ENV = "UNILAB_INSTANCE_SYNC_TOKEN"


class InstanceSyncError(RuntimeError):
    """设备图无法映射到后端模板或实例。"""


@dataclass(frozen=True)
class InstanceSyncReport:
    created_count: int
    existing_count: int
    material_uuids: Dict[str, str]


class InstanceSynchronizer:
    """把设备图节点映射到后端 Material，隐藏分页和父子创建顺序。"""

    def __init__(
        self,
        backend_address: str,
        operator_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ) -> None:
        token = str(operator_token or "").strip()
        self.backend_api = _api_base(backend_address)
        self.operator_token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync_graph(self, graph: Mapping[str, Any]) -> InstanceSyncReport:
        """按父子顺序把设备图收敛为云端物料（Material）及其部署位置。

        Args:
            graph: 包含节点、父子关系和可选几何信息的设备图。

        Returns:
            本次创建数、复用数及本地节点到云端物料 UUID 的映射。

        Raises:
            InstanceSyncError: 认证、设备图、模板、父子关系或云端响应
                不满足实例同步合同时抛出。

        设备图声明相对位置时，设备包是该部署几何的权威：新物料在创建请求中
        携带位置，既有物料只在位置不一致时通过 Edge 写接口更新；设备图未声明
        位置时不清空云端事实。
        """

        if not self.operator_token:
            raise InstanceSyncError("operator token is required for instance writes")
        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise InstanceSyncError("device graph nodes are required")
        nodes = [_graph_node(node) for node in raw_nodes]
        nodes = _attach_external_roots(nodes)
        node_ids = [node["id"] for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise InstanceSyncError("device graph contains duplicate node ids")

        templates = self._list_templates()
        existing_materials = self._list_materials()
        materials_by_barcode = {
            str(material.get("barcode") or ""): material
            for material in existing_materials
            if material.get("barcode")
        }
        # current_positions_by_material_uuid 是 Backend 的批量位置快照，只在既有物料
        # 确实声明设备包位置时读取，避免首次初始化产生无用查询。
        current_positions_by_material_uuid = (
            self._material_positions()
            if any(
                node.get("relative_position") is not None
                and node["barcode"] in materials_by_barcode
                for node in nodes
            )
            else {}
        )

        material_uuids: Dict[str, str] = {}
        pending: Dict[str, Dict[str, Any]] = {
            node["id"]: node for node in sorted(nodes, key=lambda node: node["id"])
        }
        created_count = 0
        existing_count = 0
        while pending:
            progressed = False
            for local_id, node in list(pending.items()):
                parent_local_id = node.get("parent")
                if parent_local_id and parent_local_id not in material_uuids:
                    if parent_local_id in pending:
                        continue
                    raise InstanceSyncError(
                        f"node {local_id} references unknown parent {parent_local_id}"
                    )
                template = _resolve_template(templates, node["class"])
                if template is None:
                    raise InstanceSyncError(
                        f"resource template {node['class']} has not been synchronized"
                    )
                existing = materials_by_barcode.get(node["barcode"])
                if existing is not None:
                    existing_template_uuid = str(
                        existing.get("resource_template_uuid") or ""
                    )
                    if existing_template_uuid != template["uuid"]:
                        raise InstanceSyncError(
                            f"barcode {node['barcode']} belongs to another template"
                        )
                    material_uuid = str(existing.get("uuid") or "")
                    if not material_uuid:
                        raise InstanceSyncError(
                            f"existing material {node['barcode']} has no UUID"
                        )
                    try:
                        update_request = position_update_request(
                            node,
                            existing,
                            current_positions_by_material_uuid.get(material_uuid),
                        )
                    except InstancePositionSyncError as exc:
                        raise InstanceSyncError(str(exc)) from exc
                    if update_request is not None:
                        self._request(
                            "PUT",
                            f"/edge/materials/{material_uuid}",
                            route="/api/v1/edge/materials/:material_uuid",
                            json=update_request,
                        )
                    self._request(
                        "PUT",
                        f"/edge/materials/{material_uuid}/sites/from-template",
                        route="/api/v1/edge/materials/:material_uuid/sites/from-template",
                        json={},
                    )
                    existing_count += 1
                else:
                    request_body: Dict[str, Any] = {
                        "resource_template_uuid": template["uuid"],
                        "barcode": node["barcode"],
                        "name": node["name"],
                        "config": node["config"],
                        "meta_data": {
                            "edge_local_id": local_id,
                            "edge_resource_type": node["type"],
                            "initial_state": node["data"],
                        },
                    }
                    if parent_local_id:
                        request_body["parent_uuid"] = material_uuids[parent_local_id]
                    if node.get("relative_position") is not None:
                        request_body["relative_position"] = node["relative_position"]
                    created = self._request(
                        "POST",
                        "/materials",
                        route="/api/v1/materials",
                        json=request_body,
                    )
                    material_uuid = str(created.get("uuid") or "")
                    if not material_uuid:
                        raise InstanceSyncError(
                            f"created material {node['barcode']} has no UUID"
                        )
                    created_count += 1
                    materials_by_barcode[node["barcode"]] = created
                material_uuids[local_id] = material_uuid
                del pending[local_id]
                progressed = True
            if not progressed:
                raise InstanceSyncError(
                    f"device graph parent relationship contains a cycle: {sorted(pending)}"
                )

        return InstanceSyncReport(
            created_count=created_count,
            existing_count=existing_count,
            material_uuids=material_uuids,
        )

    def check_graph(self, graph: Mapping[str, Any]) -> InstanceSyncReport:
        """只读检查模板和实例是否齐备，供生产 Edge 启动门禁使用。"""

        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise InstanceSyncError("device graph nodes are required")
        nodes = [_graph_node(node) for node in raw_nodes]
        nodes = _attach_external_roots(nodes)
        templates = self._list_templates()
        materials_by_barcode = {
            str(material.get("barcode") or ""): material
            for material in self._list_materials()
            if material.get("barcode")
        }
        material_uuids: Dict[str, str] = {}
        for node in nodes:
            template = _resolve_template(templates, node["class"])
            if template is None:
                raise InstanceSyncError(
                    f"resource template {node['class']} has not been synchronized"
                )
            material = materials_by_barcode.get(node["barcode"])
            if material is None:
                raise InstanceSyncError(
                    f"material {node['barcode']} has not been initialized"
                )
            if str(material.get("resource_template_uuid") or "") != template["uuid"]:
                raise InstanceSyncError(
                    f"barcode {node['barcode']} belongs to another template"
                )
            material_uuid = str(material.get("uuid") or "")
            if not material_uuid:
                raise InstanceSyncError(
                    f"existing material {node['barcode']} has no UUID"
                )
            material_uuids[node["id"]] = material_uuid
        return InstanceSyncReport(
            created_count=0,
            existing_count=len(material_uuids),
            material_uuids=material_uuids,
        )

    def _list_templates(self) -> Dict[str, Dict[str, str]]:
        templates: Dict[str, Dict[str, str]] = {}
        page = 1
        while True:
            result = self._request(
                "GET",
                "/resource-templates",
                route="/api/v1/resource-templates",
                params={"page": page, "page_size": 100},
            )
            for template in _mapping_list(result.get("items")):
                name = str(template.get("name") or "")
                template_uuid = str(template.get("uuid") or "")
                if name and template_uuid:
                    templates[name] = {"uuid": template_uuid}
            if not result.get("has_more"):
                return templates
            page += 1

    def _list_materials(self) -> list[Mapping[str, Any]]:
        materials: list[Mapping[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                "/materials",
                route="/api/v1/materials",
                params={
                    "page": page,
                    "page_size": 100,
                    "with_children": "true",
                },
            )
            page_materials = _mapping_list(result.get("items"))
            materials.extend(page_materials)
            total = int(result.get("total") or 0)
            if len(materials) >= total or not page_materials:
                return materials
            page += 1

    def _material_positions(self) -> Dict[str, Optional[Mapping[str, Any]]]:
        """批量读取 Backend 物料图中的当前相对位置。

        Returns:
            以物料 UUID 为键、相对位置对象或 ``None`` 为值的完整快照。

        Raises:
            InstanceSyncError: Backend 响应失败时由统一 HTTP 解码边界抛出。
        """

        result = self._request(
            "GET",
            "/materials/graph",
            route="/api/v1/materials/graph",
        )
        # positions_by_material_uuid 把批量图节点变成同步循环可直接查询的位置索引。
        positions_by_material_uuid: Dict[
            str, Optional[Mapping[str, Any]]
        ] = {}
        for graph_node in _mapping_list(result.get("nodes")):
            material = graph_node.get("material")
            if not isinstance(material, Mapping):
                continue
            material_uuid = str(material.get("uuid") or "").strip()
            if not material_uuid:
                continue
            relative_position = graph_node.get("relative_position")
            positions_by_material_uuid[material_uuid] = (
                relative_position
                if isinstance(relative_position, Mapping)
                else None
            )
        return positions_by_material_uuid

    def _request(
        self,
        method: str,
        path: str,
        *,
        route: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        url = f"{self.backend_api}{path}"
        target = urlsplit(url)
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.operator_token:
            headers["Authorization"] = f"Bearer {self.operator_token}"
        headers.setdefault("Content-Type", "application/json")
        with span(
            "edge.http.instance.sync",
            kind="client",
            attributes={
                "http.request.method": method,
                "http.route": route,
                "server.address": target.hostname or "",
            },
        ) as request_span:
            inject_trace_context(headers)
            request_method = getattr(self.session, method.lower())
            response = request_method(
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass
        return _decode_response(response, operation=f"{method} {path}")


def run_instance_sync_command(
    arguments: Mapping[str, Any],
    *,
    backend_address: str,
    environment: Optional[Mapping[str, str]] = None,
    session: Optional[requests.Session] = None,
) -> InstanceSyncReport:
    """从文件读取设备图并执行一次初始化，不启动任何设备驱动。"""

    graph_path = str(arguments.get("graph") or "").strip()
    if not graph_path:
        raise InstanceSyncError("--graph is required for instance-sync")
    try:
        with open(graph_path, encoding="utf-8") as graph_file:
            graph = json.load(graph_file)
    except (OSError, ValueError) as exc:
        raise InstanceSyncError(f"cannot read device graph {graph_path}: {exc}") from exc
    if not isinstance(graph, Mapping):
        raise InstanceSyncError("device graph root must be an object")
    if arguments.get("instance_devices_only", False):
        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list):
            raise InstanceSyncError("device graph nodes are required")
        graph = {
            **graph,
            "nodes": [
                node
                for node in raw_nodes
                if isinstance(node, Mapping) and node.get("type") == "device"
            ],
        }
    token_source = environment if environment is not None else os.environ
    operator_token = token_source.get(INSTANCE_TOKEN_ENV, "")
    synchronizer = InstanceSynchronizer(
        backend_address,
        operator_token,
        session=session,
    )
    if arguments.get("instance_check_only", False):
        return synchronizer.check_graph(graph)
    return synchronizer.sync_graph(graph)


def _graph_node(raw_node: Any) -> Dict[str, Any]:
    """把一个原始设备图节点规范化为实例同步内部节点。

    Args:
        raw_node: 设备图中的单个节点对象。

    Returns:
        包含稳定本地身份、模板名、物料字段、父级和可选相对位置的内部节点。

    Raises:
        InstanceSyncError: 节点不是对象、缺少身份或几何字段不是有限数值时抛出。
    """
    if not isinstance(raw_node, Mapping):
        raise InstanceSyncError("device graph node must be an object")
    local_id = str(raw_node.get("id") or "").strip()
    template_name = str(raw_node.get("class") or "").strip()
    raw_resource_type = str(raw_node.get("type") or "").strip()
    if not local_id or not template_name:
        raise InstanceSyncError("every graph node requires id and class")
    barcode = normalize_resource_instance_barcode(raw_node.get("barcode"), local_id)
    resource_type = "device" if raw_resource_type == "device" else "resource"
    parent = raw_node.get("parent")
    config = _object(raw_node.get("config"))
    try:
        relative_position = relative_position_from_graph_node(raw_node, config)
    except InstanceGeometryError as exc:
        raise InstanceSyncError(str(exc)) from exc
    return {
        "id": local_id,
        "name": str(raw_node.get("name") or local_id).strip(),
        "type": resource_type,
        "class": template_name,
        "barcode": barcode,
        "config": config,
        "data": _object(raw_node.get("data")),
        "parent": str(parent).strip() if parent else "",
        "relative_position": relative_position,
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _attach_external_roots(
    nodes: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """把场景根和未随图提供的父节点收口到正式 HostNode。

    Backend 的 Material 合同要求非设备实例必须有父实例。设备图通常把
    Deck 表达为无父场景根，因此在跨 Authority 初始化时显式把它挂到
    HostNode；设备根仍保持无父，不虚构物理包含关系。

    Args:
        nodes: 已规范化且尚未补齐外部根的设备图节点。

    Returns:
        必要时包含自动 Host Node、并已修正资源父级的独立节点列表。
    """

    node_ids = {node["id"] for node in nodes}
    external_roots = {
        node["parent"]
        for node in nodes
        if node.get("parent") and node["parent"] not in node_ids
    }
    orphan_resources = {
        node["id"]
        for node in nodes
        if node.get("type") != "device" and not node.get("parent")
    }
    if not external_roots and not orphan_resources:
        return nodes
    host_id = "host_node"
    if host_id not in node_ids:
        nodes = [
            {
                "id": host_id,
                "name": "Host Node",
                "type": "device",
                "class": host_id,
                "barcode": "UNILAB-GRAPH-host_node",
                "config": {},
                "data": {},
                "parent": "",
                "relative_position": default_host_relative_position(),
            },
            *nodes,
        ]
    return [
        {
            **node,
            "parent": (
                host_id
                if node["id"] in orphan_resources
                or node.get("parent") in external_roots
                else node.get("parent", "")
            ),
        }
        for node in nodes
    ]


def _resolve_template(
    templates: Mapping[str, Dict[str, str]], class_name: str
) -> Optional[Dict[str, str]]:
    """同时兼容工作区完整类名与 Backend 中的模板短名。"""

    return templates.get(class_name) or templates.get(class_name.rsplit(".", 1)[-1])


def _object(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _decode_response(response: Any, *, operation: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise InstanceSyncError(
            f"{operation} returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise InstanceSyncError(
            f"{operation} returned HTTP {response.status_code}: {payload}"
        )
    if not isinstance(payload, Mapping):
        raise InstanceSyncError(f"{operation} returned a non-object response")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise InstanceSyncError(
            f"{operation} returned business error {code}: {payload.get('error')}"
        )
    result = payload.get("data", payload)
    if not isinstance(result, Mapping):
        raise InstanceSyncError(f"{operation} returned invalid data")
    return dict(result)


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise InstanceSyncError("backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"
