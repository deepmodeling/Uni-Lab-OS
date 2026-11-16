"""通过正式后端 API 幂等初始化设备图中的实际资源实例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

import requests

from unilabos.utils.tracing import inject_trace_context, span


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
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ) -> None:
        self.backend_api = _api_base(backend_address)
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync_graph(self, graph: Mapping[str, Any]) -> InstanceSyncReport:
        """创建缺失实例；同一条码已存在时校验模板后直接复用。"""

        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise InstanceSyncError("device graph nodes are required")
        nodes = [_graph_node(node) for node in raw_nodes]
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
                    if parent_local_id not in pending:
                        raise InstanceSyncError(
                            f"node {local_id} references unknown parent {parent_local_id}"
                        )
                    continue
                template = templates.get(node["template_name"])
                if template is None:
                    raise InstanceSyncError(
                        f"resource template {node['template_name']} has not been synchronized"
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
                    existing_count += 1
                else:
                    request_body: Dict[str, Any] = {
                        "resource_template_uuid": template["uuid"],
                        "barcode": node["barcode"],
                        "name": node["name"],
                        "config": node["config"],
                        "data": node["data"],
                        "meta_data": {
                            "edge_local_id": local_id,
                            "edge_resource_type": node["type"],
                        },
                    }
                    if parent_local_id:
                        request_body["parent_uuid"] = material_uuids[parent_local_id]
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
        templates = self._list_templates()
        materials_by_barcode = {
            str(material.get("barcode") or ""): material
            for material in self._list_materials()
            if material.get("barcode")
        }
        material_uuids: Dict[str, str] = {}
        for node in nodes:
            template = templates.get(node["template_name"])
            if template is None:
                raise InstanceSyncError(
                    f"resource template {node['template_name']} has not been synchronized"
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
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor_uuid"] = cursor
            result = self._request(
                "GET",
                "/resource-templates",
                route="/api/v1/resource-templates",
                params=params,
            )
            for template in _mapping_list(result.get("items")):
                name = str(template.get("name") or "")
                template_uuid = str(template.get("uuid") or "")
                if name and template_uuid:
                    templates[name] = {"uuid": template_uuid}
            if not result.get("has_more"):
                return templates
            cursor = str(result.get("next_cursor_uuid") or "")
            if not cursor:
                raise InstanceSyncError("template pagination cursor is missing")

    def _list_materials(self) -> list[Mapping[str, Any]]:
        materials: list[Mapping[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                "/materials",
                route="/api/v1/materials",
                params={"page": page, "page_size": 100, "with_children": True},
            )
            page_materials = _mapping_list(result.get("items"))
            materials.extend(page_materials)
            total = int(result.get("total") or 0)
            if len(materials) >= total or not page_materials:
                return materials
            page += 1

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


def _graph_node(raw_node: Any) -> Dict[str, Any]:
    """图节点投影：模板名以 ``template_name`` 为准，旧图 ``class`` 仅作读取回退。"""

    if not isinstance(raw_node, Mapping):
        raise InstanceSyncError("device graph node must be an object")
    local_id = str(raw_node.get("id") or "").strip()
    template_name = str(
        raw_node.get("template_name") or raw_node.get("class") or ""
    ).strip()
    barcode = str(raw_node.get("barcode") or "").strip()
    resource_type = str(raw_node.get("type") or "").strip()
    if not local_id or not template_name or not barcode:
        raise InstanceSyncError(
            "every graph node requires id, template_name/class, and barcode"
        )
    if resource_type not in {"device", "resource"}:
        raise InstanceSyncError(
            f"node {local_id} type must be device or resource"
        )
    parent = raw_node.get("parent")
    return {
        "id": local_id,
        "name": str(raw_node.get("name") or local_id).strip(),
        "type": resource_type,
        "template_name": template_name,
        "barcode": barcode,
        "config": _object(raw_node.get("config")),
        "data": _object(raw_node.get("data")),
        "parent": str(parent).strip() if parent else "",
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


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
