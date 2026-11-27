"""由领域包设备声明编译出的只读运行时设备模板目录。"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from unilabos.registry.template_identity import (
    TemplateIdentityError,
    device_resource_handle_uuid,
    device_template_uuid,
)
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

_SYNTHETIC_TIME = "1970-01-01T00:00:00Z"


class RuntimeDeviceCatalogError(ValueError):
    """运行时设备模板目录无法建立唯一且稳定的名称身份。"""


class RuntimeDeviceTemplateCatalog:
    """保存单次 OS 启动代际的设备模板，不写入 SQLite。"""

    def __init__(self, snapshot: RegistryTemplateSnapshot) -> None:
        """从冻结注册表快照构建不可变设备模板索引。

        参数：``snapshot`` 是领域包 AST/注册表编译后的完整冻结代际。返回：按
        设备名与确定性 UUID 双向索引的只读目录。异常：名称、连接点或身份冲突
        时关闭式失败，调用方不得发布部分目录。
        """

        if not isinstance(snapshot, RegistryTemplateSnapshot):
            raise TypeError("snapshot 必须是 RegistryTemplateSnapshot")
        by_uuid: dict[str, Mapping[str, Any]] = {}
        uuid_by_name: dict[str, str] = {}
        for definition in snapshot.detached_devices():
            name = definition.get("id")
            if not isinstance(name, str) or not name:
                raise RuntimeDeviceCatalogError("设备模板缺少稳定业务名")
            try:
                template_uuid = device_template_uuid(name)
                detail = _device_template_detail(definition, template_uuid)
            except TemplateIdentityError as error:
                raise RuntimeDeviceCatalogError(str(error)) from error
            if name in uuid_by_name or template_uuid in by_uuid:
                raise RuntimeDeviceCatalogError(f"设备模板身份重复: {name}")
            uuid_by_name[name] = template_uuid
            by_uuid[template_uuid] = _freeze(detail)
        self._fingerprint = snapshot.fingerprint
        self._uuid_by_name = MappingProxyType(uuid_by_name)
        self._by_uuid = MappingProxyType(by_uuid)

    @property
    def fingerprint(self) -> str:
        """返回当前冻结注册表代际指纹。"""

        return self._fingerprint

    def resolve_uuid(self, device_name: str) -> str:
        """按设备稳定业务名解析确定性 UUID；未知名称返回空串。"""

        return self._uuid_by_name.get(device_name, "")

    def contains_uuid(self, template_uuid: str) -> bool:
        """判断 UUID 是否属于当前内存设备模板代际。"""

        return template_uuid in self._by_uuid

    def get(self, template_uuid: str) -> dict[str, Any] | None:
        """按确定性 UUID 返回分离的 Backend 形态模板详情。"""

        detail = self._by_uuid.get(template_uuid)
        return _detach(detail) if detail is not None else None

    def list(self) -> list[dict[str, Any]]:
        """按设备业务名返回本代全部分离模板详情。"""

        return [
            _detach(self._by_uuid[template_uuid])
            for _, template_uuid in sorted(self._uuid_by_name.items())
        ]


def _device_template_detail(
    definition: Mapping[str, Any],
    template_uuid: str,
) -> dict[str, Any]:
    """把注册表设备定义转换为原有 Backend 资源模板响应形状。"""

    name = str(definition["id"])
    class_definition = definition.get("class")
    if not isinstance(class_definition, Mapping):
        class_definition = {}
    schema = definition.get("init_param_schema")
    if not isinstance(schema, Mapping):
        schema = {}
    data_schema = schema.get("data")
    config_schema = schema.get("config")
    handles = _device_resource_handles(
        name,
        template_uuid,
        definition.get("handles") or (),
    )
    meta_data: dict[str, Any] = {}
    metadata = definition.get("metadata") or {}
    for key in ("observations", "twin"):
        if key in metadata:
            meta_data[key] = _detach(metadata[key])
    source_uri = definition.get("source_uri")
    if isinstance(source_uri, str) and source_uri:
        meta_data["unilab"] = {"source_uri": source_uri}
    return {
        "uuid": template_uuid,
        "create_time": _SYNTHETIC_TIME,
        "update_time": _SYNTHETIC_TIME,
        "description": definition.get("description"),
        "meta_data": meta_data,
        "name": name,
        "display_name": str(definition.get("display_name") or name),
        "resource_type": "device",
        "header": None,
        "footer": None,
        "icon": definition.get("icon"),
        "model": copy.deepcopy(definition.get("model") or {}),
        "module": class_definition.get("module") or None,
        "language": class_definition.get("type") or None,
        "tags": copy.deepcopy(definition.get("category") or []),
        "data_schema": copy.deepcopy(
            data_schema.get("properties", {})
            if isinstance(data_schema, Mapping)
            else {}
        ),
        "config_schema": copy.deepcopy(
            config_schema.get("properties", {})
            if isinstance(config_schema, Mapping)
            else {}
        ),
        "pose": {},
        "config_info": copy.deepcopy(definition.get("config_info") or []),
        "available_sites": copy.deepcopy(definition.get("available_sites") or []),
        "cover": definition.get("cover"),
        "scene": copy.deepcopy(definition.get("scene") or []),
        "device_params": copy.deepcopy(definition.get("device_params") or {}),
        "manufacturer_uuid": None,
        "ui_overlay": {},
        "handles": handles,
    }


def _device_resource_handles(
    device_name: str,
    template_uuid: str,
    raw_handles: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """编译设备模板自身的资源 Handle，并生成稳定 UUID。"""

    result: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw in raw_handles:
        if not isinstance(raw, Mapping):
            raise RuntimeDeviceCatalogError("设备资源 Handle 必须是对象")
        name = str(raw.get("handler_key") or "")
        io_type = str(raw.get("io_type") or "")
        business_key = (io_type, name)
        if business_key in seen_keys:
            raise RuntimeDeviceCatalogError(
                f"设备资源 Handle 重复: {device_name}.{io_type}.{name}"
            )
        seen_keys.add(business_key)
        handle_uuid = device_resource_handle_uuid(
            device_name,
            io_type=io_type,
            handle_name=name,
        )
        item = {
            "uuid": handle_uuid,
            "create_time": _SYNTHETIC_TIME,
            "update_time": _SYNTHETIC_TIME,
            "description": raw.get("description"),
            "meta_data": {},
            "resource_template_uuid": template_uuid,
            "name": name,
            "display_name": str(raw.get("label") or name),
            "type": str(raw.get("data_type") or "resource"),
            "io_type": io_type,
        }
        for source_field, target_field in (
            ("data_source", "source"),
            ("data_key", "key"),
            ("side", "side"),
        ):
            if raw.get(source_field) not in (None, ""):
                item[target_field] = raw[source_field]
        result.append(item)
    return sorted(result, key=lambda item: (item["io_type"], item["name"]))


def _freeze(value: Any) -> Any:
    """递归冻结目录值，避免请求方修改当前启动代际。"""

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _detach(value: Any) -> Any:
    """递归复制冻结值，向调用者返回普通 JSON 容器。"""

    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_detach(item) for item in value]
    return copy.deepcopy(value)


__all__ = ["RuntimeDeviceCatalogError", "RuntimeDeviceTemplateCatalog"]
