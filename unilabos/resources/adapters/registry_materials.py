"""把 Registry 资源定义登记到 materials authority。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid5

from unilabos.server.database.tables.materials import ResourceTemplateHandle
from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    ACTOR_REGISTRY,
    ResourceTemplateRead,
    ResourceTemplateWrite,
)
from unilabos.utils.serialization import normalize_json


_REGISTRY_SYNC_NAMESPACE = UUID("9e5f7a4a-cae5-4d89-a039-c10c9c065ad1")


class TemplateGateway(Protocol):
    def list_templates(
        self, *, name: str | None = None, include_definition: bool = False
    ) -> list[ResourceTemplateRead]: ...

    def put_template(self, mutation, value): ...

    def create_template(self, mutation, value): ...


@dataclass(frozen=True)
class RegistryTemplateReport:
    resource_count: int
    template_uuids: dict[str, str]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _available_sites(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = _array(definition.get("available_sites"))
    if explicit:
        return [dict(item) for item in explicit if isinstance(item, Mapping)]
    components = [
        dict(item)
        for item in _array(definition.get("config_info"))
        if isinstance(item, Mapping)
    ]
    if not components:
        return []
    config = _mapping(components[0].get("config"))
    sites = config.get("sites", components[0].get("sites", []))
    return [dict(item) for item in _array(sites) if isinstance(item, Mapping)]


def _resource_type(definition: Mapping[str, Any]) -> str:
    components = [
        item
        for item in _array(definition.get("config_info"))
        if isinstance(item, Mapping)
    ]
    if components:
        value = components[0].get("type") or _mapping(components[0].get("config")).get(
            "category"
        )
        if value:
            return str(value)
    model = _mapping(definition.get("model"))
    nested = model.get("resource") or model.get("resource_dict")
    if isinstance(nested, Mapping) and nested.get("type"):
        return str(nested["type"])
    return "resource"


def registry_resource_class_name(definition: Mapping[str, Any]) -> str | None:
    """注册表资源条目的 PLR 短类型名：``config_info`` 里序列化的 ``config.type``，其次非通用的 ``class.type``。

    它是物料 ``config.type`` 的来源（PLR 反序列化靠它找类）；旧格式图在读图边界按同一规则补齐。
    物料与模板的 ``class`` 是注册表资源名本身（见 :mod:`unilabos.resources.material_class`）。
    条目没有 ``config_info``、``class.type`` 又是通用后端名时返回 None。
    """

    components = [
        item
        for item in _array(definition.get("config_info"))
        if isinstance(item, Mapping)
    ]
    if components:
        serialized_type = _mapping(components[0].get("config")).get("type")
        if serialized_type:
            return str(serialized_type)

    declared_type = _mapping(definition.get("class")).get("type")
    if declared_type and declared_type not in {
        "pylabrobot",
        "python",
        "ros2",
        "hostlink",
    }:
        return str(declared_type)
    return None


def _handles(definition: Mapping[str, Any]) -> list[ResourceTemplateHandle]:
    result: list[ResourceTemplateHandle] = []
    for ordinal, raw in enumerate(_array(definition.get("handles"))):
        if not isinstance(raw, Mapping):
            continue
        io_type = str(raw.get("io_type") or "").strip().lower()
        io_type = {
            "input": "target",
            "output": "source",
            "both": "bidirectional",
        }.get(io_type, io_type)
        if io_type not in {"source", "target", "bidirectional"}:
            continue
        key = str(raw.get("handler_key") or raw.get("key") or f"handle-{ordinal}")
        data_type = str(raw.get("data_type") or "object")
        side = str(raw.get("side") or "").upper() or None
        if side not in {None, "NORTH", "SOUTH", "EAST", "WEST"}:
            side = None
        result.append(
            ResourceTemplateHandle(
                key=key,
                label=str(raw.get("label") or key),
                io_type=io_type,
                data_type=data_type,
                side=side,
                data_key=str(raw.get("data_key") or "") or None,
                data_source=str(raw.get("data_source") or "") or None,
                description=str(raw.get("description") or ""),
            )
        )
    return result


def registry_definition_to_template(
    definition: Mapping[str, Any], *, template_uuid: str | None = None
) -> ResourceTemplateWrite:
    """将上传后端的 Registry 形状投影成微后端规范模板。

    只弹出顶层的提升字段，浅拷贝即可；调用方传入的已是 ``normalize_json`` 产出的
    独立 JSON 结构，逐个深拷贝十几 MB 的 definition 只是白花几百毫秒。
    """

    values = dict(definition)
    name = str(values.get("id") or "").strip()
    if not name:
        raise ValueError("registry resource template id is required")
    class_definition = _mapping(values.get("class"))
    category = [str(item) for item in _array(values.get("category")) if str(item)]
    available_sites = _available_sites(values)
    handles = _handles(values)
    for promoted in (
        "id",
        "display_name",
        "registry_type",
        "category",
        "available_sites",
        "handles",
        "class",
    ):
        values.pop(promoted, None)
    return ResourceTemplateWrite(
        template_uuid=template_uuid,
        name=name,
        display_name=str(definition.get("display_name") or name),
        resource_type=_resource_type(definition),
        # class 是初始化物料用的注册表名，即条目 id；实现模块在 module_name（class.module）。
        # 条目没有声明实现（没有 class.module）时没法按它初始化，模板不约束物料的 class。
        class_name=name if class_definition.get("module") else None,
        module_name=str(class_definition.get("module") or "") or None,
        template_version=str(definition.get("version") or "registry-v1"),
        category=category,
        available_sites=available_sites,
        handles=handles,
        definition=values,
    )


def _template_definition_hash(value: ResourceTemplateWrite) -> str:
    return canonical_hash(
        {
            "name": value.name,
            "display_name": value.display_name or value.name,
            "resource_type": value.resource_type,
            "class_name": value.class_name,
            "module_name": value.module_name,
            "template_version": value.template_version,
            "category": value.category,
            "available_sites": value.available_sites,
            "handles": [item.model_dump(mode="json") for item in value.handles],
            "definition": value.definition,
            "status": value.status,
        }
    )


def register_resource_definitions(
    definitions: Iterable[Mapping[str, Any]], gateway: TemplateGateway
) -> RegistryTemplateReport:
    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw in definitions:
        definition = normalize_json(dict(raw))
        name = str(definition.get("id") or "").strip()
        if not name:
            raise ValueError("registry resource template id is required")
        if name in seen_names:
            raise ValueError(f"duplicate registry resource template id: {name}")
        seen_names.add(name)
        normalized.append(definition)
    normalized.sort(key=lambda item: str(item["id"]))
    # 变更判定只看 definition_hash：目录模式（默认）不拉、不校验十几 MB 的 definition
    existing = {item.name: item for item in gateway.list_templates()}
    identities: dict[str, str] = {}
    for definition in normalized:
        name = str(definition["id"])
        current = existing.get(name)
        value = registry_definition_to_template(
            definition,
            template_uuid=current.template_uuid if current is not None else None,
        )
        if current is not None and current.definition_hash == _template_definition_hash(
            value
        ):
            identities[name] = current.template_uuid
            continue
        envelope = {
            "effect_key": f"sync_template:{name}",
            "operation": "sync_template",
            "actor_type": ACTOR_REGISTRY,
            "actor_uuid": name,
        }
        # 幂等键必须覆盖服务端 request_hash 的全部输入（信封 + 正文）：只按定义
        # 派生时，信封字段一旦变化（如 actor_type 从 edge 改为 registry），同一
        # command_uuid 会命中旧库里的效果行并被判定为"同键不同请求"而拒绝。
        request_hash = canonical_hash(
            {**envelope, "payload": value.model_dump(mode="json", exclude_none=False)}
        )
        mutation = InventoryMutation(
            command_uuid=str(
                uuid5(_REGISTRY_SYNC_NAMESPACE, f"{name}:{request_hash}")
            ),
            **envelope,
        )
        result = (
            gateway.put_template(mutation, value)
            if value.template_uuid is not None
            else gateway.create_template(mutation, value)
        )
        identities[name] = result.data.template_uuid
    return RegistryTemplateReport(
        resource_count=len(normalized), template_uuids=identities
    )


def sync_registry_resources(registry: Any, gateway: TemplateGateway) -> RegistryTemplateReport:
    # 只投影资源：设备条目的深拷贝 + 规范化要几百毫秒，这里用不到
    resources = {
        item["id"]: item for item in registry.obtain_registry_resource_info()
    }
    return register_resource_definitions(resources.values(), gateway)


__all__ = [
    "RegistryTemplateReport",
    "register_resource_definitions",
    "registry_definition_to_template",
    "registry_resource_class_name",
    "sync_registry_resources",
]
