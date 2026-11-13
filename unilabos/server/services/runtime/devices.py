"""设备目录读模型（V1 ``/devices``、``/authoring/device-catalog``、``/online-devices``、``/actions`` 等）。

dev 的设备事实分三处：设备根物料在物料权威（``resource_type=device``，``resource_id`` 即设备 ID，
``template_name`` 即注册表设备类），动作合同在注册表权威（``action_value_mappings``），在线能力在
Host 的 runtime endpoint 快照（``action_capabilities`` 的 ``device_uuid`` 为设备 ID 或物料 UUID）。
本模块把三者合成 V1 各路由的形状；不复制任何存储。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from unilabos.registry.template_identity import TemplateIdentityError, action_template_uuid
from unilabos.server.api.v1_fields import ms_to_iso

_JSON_TYPES = {
    "bool": "boolean", "boolean": "boolean", "dict": "object", "float": "number", "int": "integer",
    "integer": "integer", "list": "array", "number": "number", "object": "object", "str": "string", "string": "string",
}


class DeviceNotFound(LookupError):
    """设备或动作不在当前目录中。"""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _template_uuid(device_class: str, action_name: str) -> str:
    try:
        return action_template_uuid(device_class, action_name)
    except TemplateIdentityError:
        return ""


class DeviceDirectory:
    """一次请求的设备目录快照：物料 + 注册表 + endpoint 能力。"""

    def __init__(
        self,
        *,
        materials: Iterable[Any],
        registry_payloads: Iterable[Mapping[str, Any]],
        endpoints: Optional[Iterable[Any]],
    ) -> None:
        self.devices: List[Dict[str, Any]] = []
        for aggregate in materials:
            material = _plain(getattr(aggregate, "material", None) or _mapping(_plain(aggregate)).get("material"))
            material = _mapping(material)
            if str(material.get("resource_type") or "") != "device" or material.get("parent_material_uuid"):
                continue
            if not material.get("resource_id"):
                continue
            self.devices.append(dict(material))
        self.devices.sort(key=lambda item: str(item["resource_id"]))
        self.registry = {
            str(item.get("id") or ""): dict(item) for item in registry_payloads if str(item.get("id") or "")
        }
        self.endpoints_known = endpoints is not None
        self.endpoints = [dict(_mapping(_plain(item))) for item in (endpoints or [])]

    # ── 查询 ──────────────────────────────────────────────────────────

    def device(self, device_id: str) -> Dict[str, Any]:
        for material in self.devices:
            if device_id in {material.get("resource_id"), material.get("material_uuid")}:
                return material
        raise DeviceNotFound(f"设备不存在：{device_id}")

    def _identities(self, material: Mapping[str, Any]) -> set[str]:
        return {str(material.get("resource_id") or ""), str(material.get("material_uuid") or "")} - {""}

    def capabilities(self, material: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """该设备在在线 endpoint 上的生效动作能力。"""

        identities = self._identities(material)
        result = []
        for endpoint in self.endpoints:
            if endpoint.get("state") != "online":
                continue
            for raw in endpoint.get("action_capabilities") or []:
                capability = _mapping(raw)
                if capability.get("state", "active") == "active" and str(capability.get("device_uuid")) in identities:
                    result.append({**capability, "endpoint_uuid": endpoint.get("endpoint_uuid"),
                                   "host_uuid": endpoint.get("host_uuid")})
        return result

    def endpoint_of(self, material: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        identities = self._identities(material)
        for endpoint in self.endpoints:
            routes = [_mapping(item) for item in endpoint.get("device_routes") or []]
            capabilities = [_mapping(item) for item in endpoint.get("action_capabilities") or []]
            if any(str(item.get("device_uuid")) in identities for item in [*routes, *capabilities]):
                return endpoint
        return None

    def unavailable(self, material: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Host 因 backend 不支持而跳过的设备：停用 route 上的原因记录；正常设备为 None。"""

        identities = self._identities(material)
        for endpoint in self.endpoints:
            for raw in endpoint.get("device_routes") or []:
                route = _mapping(raw)
                if str(route.get("device_uuid")) not in identities or route.get("enabled", True):
                    continue
                record = _mapping(_mapping(route.get("config")).get("unavailable"))
                if record:
                    return dict(record)
        return None

    def online(self, material: Mapping[str, Any]) -> bool:
        endpoint = self.endpoint_of(material)
        return (
            endpoint is not None
            and endpoint.get("state") == "online"
            and self.unavailable(material) is None
        )

    def action_mappings(self, material: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        definition = self.registry.get(str(material.get("template_name") or ""), {})
        mappings = _mapping(_mapping(definition.get("class")).get("action_value_mappings"))
        return {str(name): dict(_mapping(value)) for name, value in mappings.items() if str(name).strip()}

    def action_names(self, material: Mapping[str, Any]) -> List[str]:
        """动作名：注册表声明 ∪ 在线能力（Host 可能多出 auto- 动作）。"""

        names = list(self.action_mappings(material))
        for capability in self.capabilities(material):
            name = str(capability.get("action_name") or "")
            if name and name not in names:
                names.append(name)
        return sorted(names)

    def busy(self, material: Mapping[str, Any], action_name: str) -> tuple[bool, Optional[str]]:
        for capability in self.capabilities(material):
            if capability.get("action_name") in {action_name, f"auto-{action_name}"}:
                job = capability.get("active_job_uuid")
                return capability.get("availability") == "busy", (str(job) if job else None)
        return False, None

    def action_type(self, material: Mapping[str, Any], action_name: str) -> str:
        mapping = self.action_mappings(material).get(action_name) or {}
        if mapping.get("type"):
            return str(mapping["type"])
        for capability in self.capabilities(material):
            if capability.get("action_name") == action_name and capability.get("action_type"):
                return str(capability["action_type"])
        return "UniLabJsonCommand"

    # ── V1 投影 ────────────────────────────────────────────────────────

    def backend_devices(self) -> List[Dict[str, Any]]:
        """V1 ``GET /devices``：与 Go Backend ``DeviceOverview`` 同形（binding / material / 在线 / 动作）。"""

        result = []
        for material in self.devices:
            endpoint = self.endpoint_of(material) or {}
            edge_uuid = str(endpoint.get("host_uuid") or endpoint.get("endpoint_uuid") or "local")
            material_uuid = str(material["material_uuid"])
            unavailable = self.unavailable(material)
            online = endpoint.get("state") == "online" and unavailable is None
            created = ms_to_iso(material.get("created_at_ms"))
            updated = ms_to_iso(material.get("updated_at_ms"))
            device_class = str(material.get("template_name") or "")
            actions = []
            for name in self.action_names(material):
                action = {"name": name, "type": self.action_type(material, name)}
                template_uuid = _template_uuid(device_class, name)
                if template_uuid:
                    action["workflow_node_template_uuid"] = template_uuid
                actions.append(action)
            backend_material = {
                "uuid": material_uuid,
                "create_time": created,
                "update_time": updated,
                "meta_data": dict(_mapping(material.get("meta_data"))),
                "resource_template_uuid": str(material.get("template_uuid") or ""),
                "class": device_class,
                "type": "device",
                "barcode": str(material.get("barcode") or ""),
                "name": str(material.get("display_name") or material.get("name") or material["resource_id"]),
                "config": dict(_mapping(material.get("config"))),
                "data": {},
                "revision": int(material.get("version") or 1),
            }
            if material.get("description"):
                backend_material["description"] = str(material["description"])
            if unavailable is not None:
                edge_status = "unavailable"
            else:
                edge_status = "online" if online else ("offline" if endpoint else "registered")
            entry: Dict[str, Any] = {
                "binding": {
                    "uuid": str(uuid5(NAMESPACE_URL, f"unilab:edge-device-binding:{edge_uuid}:{material_uuid}")),
                    "create_time": created,
                    "update_time": updated,
                    "meta_data": {},
                    "edge_uuid": edge_uuid,
                    "material_uuid": material_uuid,
                    "local_id": str(material["resource_id"]),
                    "name": backend_material["name"],
                },
                "material": backend_material,
                "online": online,
                "edge_status": edge_status,
                "dispatchable": online,
                "actions": actions,
            }
            if unavailable is not None:
                # dev 扩展：Host 没有启动它的原因（如设备类只支持 ros2）
                entry["unavailable"] = unavailable
            result.append(entry)
        return sorted(result, key=lambda item: item["material"]["uuid"])

    def authoring_catalog(self) -> Dict[str, Any]:
        """V1 ``GET /authoring/device-catalog``：创作工具的富设备目录。"""

        items = []
        for material in self.devices:
            device_id = str(material["resource_id"])
            definition = self.registry.get(str(material.get("template_name") or ""), {})
            status_types = _mapping(_mapping(definition.get("class")).get("status_types"))
            actions = []
            for name, action in sorted(self.action_mappings(material).items()):
                properties = _mapping(_mapping(action.get("schema")).get("properties"))
                busy, job = self.busy(material, name)
                actions.append(
                    {
                        "id": name,
                        "actionRef": f"{device_id}.{name}",
                        "name": str(action.get("display_name") or action.get("displayname") or name),
                        "typeName": str(action.get("type") or "UniLabJsonCommand"),
                        "riskLevel": str(action.get("risk_level") or "normal"),
                        "inputSchema": dict(_mapping(properties.get("goal"))),
                        "outputSchema": dict(_mapping(properties.get("result"))),
                        "busy": busy,
                        "currentJobId": job,
                    }
                )
            items.append(
                {
                    "id": device_id,
                    "materialUuid": str(material["material_uuid"]),
                    "deviceTypeId": str(material.get("template_name") or ""),
                    "deviceKey": f"/devices/{device_id}/{device_id}",
                    "namespace": f"/devices/{device_id}",
                    "name": str(material.get("display_name") or material.get("name") or device_id),
                    "online": self.online(material),
                    **({"unavailable": unavailable} if (unavailable := self.unavailable(material)) else {}),
                    "stateSchema": {
                        str(key): {"type": _JSON_TYPES.get(str(getattr(value, "__name__", value)).strip().lower(), "string")}
                        for key, value in status_types.items() if str(key)
                    },
                    "actions": actions,
                }
            )
        return {"schemaVersion": "device-catalog/v1", "source": "edge", "generatedAt": time.time(), "items": items}

    def online_devices(self) -> Dict[str, Any]:
        """V1 ``GET /online-devices``。"""

        online = {}
        for material in self.devices:
            endpoint = self.endpoint_of(material)
            if endpoint is None or not self.online(material):
                continue
            device_id = str(material["resource_id"])
            online[device_id] = {
                "device_key": f"/devices/{device_id}/{device_id}",
                "namespace": f"/devices/{device_id}",
                "machine_name": str(material.get("machine_name") or endpoint.get("instance_name") or ""),
                "uuid": str(material["material_uuid"]),
                "node_name": device_id,
                "transport": str(endpoint.get("transport") or ""),
            }
        return {"online_devices": online, "total_count": len(online), "timestamp": time.time()}

    def _action_entry(self, material: Mapping[str, Any], name: str) -> Dict[str, Any]:
        busy, job = self.busy(material, name)
        return {
            "type_name": self.action_type(material, name),
            "action_path": f"/devices/{material['resource_id']}/{name}",
            "is_busy": busy,
            "current_job_id": job[:8] if job else None,
        }

    def device_actions(self, device_id: str) -> Dict[str, Any]:
        """V1 ``GET /devices/{device_id}/actions``。"""

        material = self.device(device_id)
        actions = {name: self._action_entry(material, name) for name in self.action_names(material)}
        return {"device_id": str(material["resource_id"]), "actions": actions, "action_count": len(actions)}

    def action_schema(self, device_id: str, action_name: str) -> Dict[str, Any]:
        """V1 ``GET /devices/{device_id}/actions/{action_name}/schema``（支持 ``auto-`` 前缀回退）。"""

        material = self.device(device_id)
        mappings = self.action_mappings(material)
        mapping = mappings.get(action_name) or mappings.get(f"auto-{action_name}")
        if mapping is None:
            for capability in self.capabilities(material):
                if capability.get("action_name") in {action_name, f"auto-{action_name}"}:
                    mapping = dict(_mapping(capability.get("descriptor")))
                    break
        if not mapping or mapping.get("schema") is None:
            raise DeviceNotFound(f"动作 schema 不存在：{device_id}/{action_name}")
        busy, job = self.busy(material, action_name)
        return {
            "device_id": str(material["resource_id"]),
            "action_name": action_name,
            "schema": mapping.get("schema"),
            "goal_default": mapping.get("goal_default"),
            "action_type": str(mapping.get("type") or self.action_type(material, action_name)),
            "handles": mapping.get("handles", {}),
            "placeholder_keys": mapping.get("placeholder_keys", {}),
            "is_busy": busy,
            "current_job_id": job[:8] if job else None,
        }

    def all_actions(self) -> Dict[str, Any]:
        """V1 ``GET /actions``：所有设备的动作及忙闲。"""

        devices = {}
        total = 0
        for material in self.devices:
            actions = {name: self._action_entry(material, name) for name in self.action_names(material)}
            if not actions:
                continue
            total += len(actions)
            endpoint = self.endpoint_of(material) or {}
            devices[str(material["resource_id"])] = {
                "actions": actions,
                "action_count": len(actions),
                "machine_name": str(material.get("machine_name") or endpoint.get("instance_name") or "未知"),
            }
        return {"devices": devices, "device_count": len(devices), "total_action_count": total, "timestamp": time.time()}


def read_online_endpoints() -> Optional[List[Dict[str, Any]]]:
    """当前 Host 的 endpoint 能力快照；Host 暂不可达时返回 ``None``（目录按全部离线投影）。"""

    try:
        from unilabos.server.backend.composition import read_preflight_endpoints

        return read_preflight_endpoints()
    except Exception:  # noqa: BLE001 - 设备目录读模型不因 Host 暂不可用而失败
        return None


def current_device_directory(
    endpoints_reader: Optional[Callable[[], Optional[List[Dict[str, Any]]]]] = None,
) -> DeviceDirectory:
    """按当前进程的物料 / 注册表 / Host 能力组一份设备目录。"""

    if endpoints_reader is None:
        endpoints_reader = globals()["read_online_endpoints"]

    from unilabos.server.backend.composition import get_materials_service
    from unilabos.server.services.runtime.registry import get_registry_service

    materials = get_materials_service()
    registry = get_registry_service()
    return DeviceDirectory(
        materials=materials.list_materials(roots_only=True) if materials is not None else [],
        registry_payloads=registry.active_payloads("device") if registry is not None else [],
        endpoints=endpoints_reader(),
    )


__all__ = ["DeviceDirectory", "DeviceNotFound", "current_device_directory", "read_online_endpoints"]
