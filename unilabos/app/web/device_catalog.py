"""把资源图、ROS 在线事实与注册表合同投影为统一设备目录。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from unilabos.app.device_action_capabilities import (
    project_device_action_capabilities,
)
from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_template_uuid,
)

# 设备模板只存在于内存目录，物料仍引用其确定性 UUID。设备列表必须保留这些
# 物料；内连接 ``resource_template`` 会把已注册设备全部丢掉。
DEVICE_OVERVIEW_MATERIALS_SQL = """
SELECT material.*,
       COALESCE(resource_template.resource_type, material.type) AS type,
       COALESCE(material.aggregate_version, 1) AS revision
FROM material
LEFT JOIN resource_template
    ON resource_template.uuid = material.resource_template_uuid
WHERE material.deleted_at IS NULL
ORDER BY material.uuid
"""

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
                # Local 与 Backend 运行接口都以设备 Material UUID 作为执行身份；
                # 实例名只用于 ROS/Driver 路由，不能再代替物料身份。
                "materialUuid": str(raw.get("uuid") or ""),
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


def project_backend_device_overviews(
    *,
    registration: Mapping[str, Any] | None,
    materials: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把本地 Edge 注册和库存行投影为设备页可调度的读模型。

    参数：``registration`` 是 Local Backend 最近一次 Edge 会话的脱离副本；
    ``materials`` 是规范 ``material`` 表活动行，包含尚未写入 SQLite 的设备模板
    所对应的物料。返回：按设备物料 UUID 排序的数组。``online`` 是布尔在线事实，
    ``dispatchable`` 还要求没有调度阻断原因；动作带确定性
    ``workflow_node_template_uuid``。异常：无；不完整注册项和没有权威物料行的
    绑定不会被猜测或返回。动作名无法生成稳定模板身份时省略该 UUID。
    """

    if not isinstance(registration, Mapping):
        return []
    edge_uuid = _text(registration.get("edge_uuid"))
    if not edge_uuid:
        return []
    material_by_uuid = {
        _text(material.get("uuid")): material
        for material in materials
        if isinstance(material, Mapping) and _text(material.get("uuid"))
    }
    created_at = _timestamp(registration.get("created_at"))
    updated_at = _timestamp(registration.get("updated_at"))
    connected = registration.get("connected") is True
    edge_status = (
        "online"
        if connected
        else "registered"
        if registration.get("created_at") == registration.get("updated_at")
        else "offline"
    )
    result: list[dict[str, Any]] = []
    devices = registration.get("devices")
    if not isinstance(devices, list):
        return []
    for raw_device in devices:
        if not isinstance(raw_device, Mapping):
            continue
        local_id = _text(raw_device.get("local_id"))
        material_uuid = _text(raw_device.get("material_uuid"))
        material = material_by_uuid.get(material_uuid)
        if not local_id or material is None:
            continue
        binding_uuid = str(
            uuid5(
                NAMESPACE_URL,
                f"unilab:edge-device-binding:{edge_uuid}:{material_uuid}",
            )
        )
        actions = raw_device.get("actions")
        reported_online = raw_device.get("online")
        online = connected and reported_online is not False
        block_reason = _text(raw_device.get("dispatch_block_reason"))
        binding = {
            "uuid": binding_uuid,
            "create_time": created_at,
            "update_time": updated_at,
            "meta_data": {},
            "edge_uuid": edge_uuid,
            "material_uuid": material_uuid,
            "local_id": local_id,
            "name": _text(raw_device.get("name")) or local_id,
        }
        if block_reason:
            binding["dispatch_block_reason"] = block_reason
        result.append(
            {
                "binding": binding,
                "material": _backend_material(material),
                "online": online,
                "edge_status": edge_status,
                "dispatchable": online and not block_reason,
                "actions": _backend_actions(
                    actions,
                    device_name=_text(material.get("class")),
                ),
            }
        )
    return sorted(result, key=lambda item: item["material"]["uuid"])


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
    normalized_mappings = {
        str(name).strip(): raw if isinstance(raw, Mapping) else {}
        for name, raw in mappings.items()
        if str(name).strip()
    }
    result = []
    for capability in project_device_action_capabilities(mappings):
        action_name = capability["name"]
        action = normalized_mappings[action_name]
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
                "typeName": capability["type"],
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


def _backend_material(raw: Mapping[str, Any]) -> dict[str, Any]:
    """选择并解码 Backend ``Material`` wire 字段。"""

    result = {
        "uuid": _text(raw.get("uuid")),
        "create_time": _text(raw.get("create_time")),
        "update_time": _text(raw.get("update_time")),
        "meta_data": _json(raw.get("meta_data"), {}),
        "resource_template_uuid": _text(raw.get("resource_template_uuid")),
        "class": _text(raw.get("class")),
        "type": _text(raw.get("type")),
        "barcode": _text(raw.get("barcode")),
        "name": _text(raw.get("name")),
        "config": _json(raw.get("config"), {}),
        "data": _json(raw.get("data"), {}),
        "revision": int(raw.get("revision") or 1),
    }
    description = raw.get("description")
    if description is not None:
        result["description"] = str(description)
    parent_uuid = _text(raw.get("parent_uuid"))
    if parent_uuid:
        result["parent_uuid"] = parent_uuid
    return result


def _backend_actions(
    value: Any,
    *,
    device_name: str,
) -> list[dict[str, str]]:
    """投影设备动作，并附上与工作流模板相同的确定性 UUID。"""

    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name"))
        action_type = _text(item.get("type"))
        if not name or not action_type:
            continue
        action = {"name": name, "type": action_type}
        template_uuid = _action_template_uuid(device_name, name)
        if template_uuid:
            action["workflow_node_template_uuid"] = template_uuid
        result.append(action)
    return result


def _action_template_uuid(device_name: str, action_name: str) -> str:
    """由设备业务名和动作名生成稳定动作模板 UUID；身份不合法时返回空串。"""

    if not device_name:
        return ""
    try:
        return action_template_uuid(device_name, action_name)
    except TemplateIdentityError:
        return ""


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _timestamp(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = 0.0
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DEVICE_OVERVIEW_MATERIALS_SQL",
    "project_backend_device_overviews",
    "project_device_catalog",
]
