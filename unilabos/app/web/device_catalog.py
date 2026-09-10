"""把资源图、ROS 在线事实与注册表合同投影为统一设备目录。"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

_INTERNAL_ACTIONS = {
    "_execute_driver_command",
    "_execute_driver_command_async",
}
_JSON_TYPES = {
    "bool": "boolean",
    "boolean": "boolean",
    "dict": "object",
    "float": "number",
    "int": "integer",
    "integer": "integer",
    "list": "array",
    "number": "number",
    "object": "object",
    "str": "string",
    "string": "string",
}


def project_device_catalog(
    *,
    resources: Any,
    registry_devices: Iterable[Mapping[str, Any]],
    online_devices: Mapping[str, Any],
    generated_at: float | None = None,
) -> dict[str, Any]:
    """生成前端与 OS 共享的设备目录（Device Catalog）。

    参数说明：``resources`` 是 Host 持有的资源树集合，``registry_devices`` 是
    注册表设备类型合同，``online_devices`` 是 ROS 图当前在线实例；
    ``generated_at`` 仅供确定性测试覆盖。返回按实例 ID 排序的目录，非设备资源
    不进入结果；输入结构不可信时按空集合失败关闭。
    """

    registry = {
        str(item.get("id") or ""): item
        for item in registry_devices
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }
    items = []
    for raw in _resource_nodes(resources):
        if str(raw.get("type") or "") != "device":
            continue
        device_id = str(raw.get("id") or "").strip()
        if not device_id:
            continue
        device_type_id = str(raw.get("class") or "").strip()
        online = online_devices.get(device_id)
        online_fact = online if isinstance(online, Mapping) else {}
        definition = registry.get(device_type_id, {})
        items.append(
            {
                "id": device_id,
                "deviceTypeId": device_type_id,
                "deviceKey": str(
                    online_fact.get("device_key")
                    or f"/devices/{device_id}/{device_id}"
                ),
                "namespace": str(
                    online_fact.get("namespace") or f"/devices/{device_id}"
                ),
                "name": str(raw.get("name") or device_id),
                "online": bool(online_fact),
                "stateSchema": _state_schema(definition),
                "actions": _actions(device_id, definition),
            }
        )
    return {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": time.time() if generated_at is None else generated_at,
        "items": sorted(items, key=lambda item: item["id"]),
    }


def _resource_nodes(resources: Any) -> list[dict[str, Any]]:
    """把资源树节点安全转换为普通字典。"""

    nodes = getattr(resources, "all_nodes", None)
    if not isinstance(nodes, list):
        return []
    result = []
    for node in nodes:
        content = getattr(node, "res_content", None)
        dump = getattr(content, "model_dump", None)
        if not callable(dump):
            continue
        value = dump(by_alias=True)
        if isinstance(value, Mapping):
            result.append(dict(value))
    return result


def _device_class(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    value = definition.get("class")
    return value if isinstance(value, Mapping) else {}


def _state_schema(definition: Mapping[str, Any]) -> dict[str, Any]:
    values = _device_class(definition).get("status_types")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): {"type": _json_type(value)}
        for name, value in values.items()
        if str(name)
    }


def _json_type(value: Any) -> str:
    name = str(getattr(value, "__name__", value)).strip().lower()
    return _JSON_TYPES.get(name, "string")


def _actions(
    device_id: str,
    definition: Mapping[str, Any],
) -> list[dict[str, Any]]:
    mappings = _device_class(definition).get("action_value_mappings")
    if not isinstance(mappings, Mapping):
        return []
    result = []
    for name, raw in mappings.items():
        action_name = str(name).strip()
        if not action_name or action_name in _INTERNAL_ACTIONS:
            continue
        action = raw if isinstance(raw, Mapping) else {}
        schema = action.get("schema")
        schema_value = schema if isinstance(schema, Mapping) else {}
        properties = schema_value.get("properties")
        contracts = properties if isinstance(properties, Mapping) else {}
        result.append(
            {
                "id": action_name,
                "actionRef": f"{device_id}.{action_name}",
                "name": str(
                    action.get("display_name")
                    or action.get("displayname")
                    or action_name
                ),
                "typeName": str(
                    getattr(action.get("type"), "__name__", action.get("type"))
                    or f"{device_id}.{action_name}"
                ),
                "riskLevel": str(action.get("risk_level") or "normal"),
                "inputSchema": _contract(contracts.get("goal")),
                "outputSchema": _contract(contracts.get("result")),
                "busy": False,
                "currentJobId": None,
            }
        )
    return result


def _contract(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["project_device_catalog"]
