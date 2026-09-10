"""设备注册表（Registry）模板定义的一次性规范化不可变快照。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_backend_action_handles,
)
from unilabos.utils.tools import normalize_json

_CONTROL_ACTION_PARAMETERS = frozenset({"unilabos_device_id"})


class RegistryTemplateSnapshotError(ValueError):
    """设备注册表模板快照无法完整、唯一地规范化。"""


@dataclass(frozen=True, slots=True)
class RegistryTemplateSnapshot:
    """供本地投影与 Backend 模板同步共同消费的不可变定义代际。"""

    fingerprint: str
    device_templates: tuple[Mapping[str, Any], ...]
    resource_templates: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_registry(cls, registry: Any) -> RegistryTemplateSnapshot:
        """只遍历一次设备注册表并构建规范快照。

        参数说明：``registry`` 提供完整设备和器材模板列表；返回值深度冻结，后续
        消费者不能因修改原 Registry 对象而看到不同合同。
        """

        try:
            raw_devices = registry.obtain_registry_device_info()
            raw_resources = registry.obtain_registry_resource_info()
        except AttributeError:
            raise RegistryTemplateSnapshotError(
                "设备注册表必须提供设备和器材模板读取接口"
            ) from None
        devices = _compile_templates(raw_devices, expected_type="device")
        resources = _compile_templates(raw_resources, expected_type="resource")
        payload = {"devices": devices, "resources": resources}
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            fingerprint=fingerprint,
            device_templates=tuple(_freeze(item) for item in devices),
            resource_templates=tuple(_freeze(item) for item in resources),
        )

    def detached_devices(self) -> list[dict[str, Any]]:
        """返回可写但不共享容器的规范设备模板列表。"""

        return [_detach(item) for item in self.device_templates]

    def detached_resources(self) -> list[dict[str, Any]]:
        """返回可写但不共享容器的规范器材模板列表。"""

        return [_detach(item) for item in self.resource_templates]

    def detached_definitions(self) -> list[dict[str, Any]]:
        """按设备后器材的固定顺序返回 Backend 同步定义全集。"""

        return [*self.detached_devices(), *self.detached_resources()]


def _compile_templates(
    definitions: Iterable[Mapping[str, Any]],
    *,
    expected_type: str,
) -> list[dict[str, Any]]:
    """规范化并按业务名排序一类资源模板。

    参数说明：``definitions`` 是 Registry 完整集合，``expected_type`` 是 device 或
    resource；返回规范列表，缺失或重复业务名时整次失败。
    """

    templates: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_definition in definitions:
        definition = _template_definition(
            raw_definition,
            expected_type=expected_type,
        )
        name = definition["id"]
        if name in seen_names:
            raise RegistryTemplateSnapshotError(
                f"重复的 {expected_type} 模板业务名: {name}"
            )
        seen_names.add(name)
        templates.append(definition)
    templates.sort(key=lambda definition: definition["id"])
    return templates


def _template_definition(
    raw_definition: Mapping[str, Any],
    *,
    expected_type: str,
) -> dict[str, Any]:
    """把一个 Registry 资源定义映射为 Backend 模板 DTO。

    参数说明：``raw_definition`` 是原始定义，``expected_type`` 固定资源种类；返回
    去除本地路径和运行态字段的规范 JSON 对象。
    """

    source = normalize_json(dict(raw_definition))
    name = str(source.get("id") or "").strip()
    if not name:
        raise RegistryTemplateSnapshotError(f"{expected_type} 模板业务名不能为空")
    resource_class = source.get("class")
    if not isinstance(resource_class, Mapping):
        resource_class = {}
    action_mappings = resource_class.get("action_value_mappings")
    if not isinstance(action_mappings, Mapping):
        action_mappings = {}
    definition: dict[str, Any] = {
        "id": name,
        "display_name": str(
            source.get("display_name") or source.get("displayname") or name
        ).strip(),
        "registry_type": expected_type,
        "model": _object(source.get("model")),
        "class": {
            "module": str(resource_class.get("module") or "").strip(),
            "type": str(resource_class.get("type") or "").strip(),
            "action_value_mappings": {
                str(action_name): _action_definition(action_definition)
                for action_name, action_definition in sorted(action_mappings.items())
            },
        },
        "handles": [
            _resource_handle(handle)
            for handle in _object_list(source.get("handles"))
        ],
        "category": _array(source.get("category")),
        "config_info": _array(source.get("config_info")),
        "scene": _array(source.get("scene")),
        "device_params": _object(source.get("device_params")),
    }
    source_fqid = source.get("source_fqid")
    if isinstance(source_fqid, str) and source_fqid.strip():
        # ``source_fqid`` 是资源模板源码身份；Backend 用它解析动作字段允许集。
        definition["source_fqid"] = source_fqid.strip()
    source_uri = source.get("source_uri")
    if isinstance(source_uri, str) and source_uri.startswith("package://"):
        definition["source_uri"] = source_uri
    schema = _initial_parameter_schema(source.get("init_param_schema"))
    if schema:
        definition["init_param_schema"] = schema
    for field in ("description", "icon", "cover"):
        value = source.get(field)
        if value is not None:
            definition[field] = value
    return definition


def _action_definition(raw_action: Any) -> dict[str, Any]:
    """把一个 Registry 动作定义映射为共同规范动作合同。

    参数说明：``raw_action`` 是可疑外部值；返回供本地投影和 Backend 同步共同
    消费的动作对象，并移除旧本地设备选择参数。
    """

    action = raw_action if isinstance(raw_action, Mapping) else {}
    contract_kind = action.get("contract_kind")
    if contract_kind == "invalid_typed":
        diagnostic = action.get("contract_diagnostic")
        message = (
            diagnostic.get("message") if isinstance(diagnostic, Mapping) else None
        )
        raise RegistryTemplateSnapshotError(
            "强类型动作合同无效" + (f": {message}" if message else "")
        )
    # ``production_schema`` 是移除旧设备选择参数后的唯一 Backend 上传合同。
    production_schema = _production_action_schema(action.get("schema"))
    handles = action.get("handles")
    if not isinstance(handles, Mapping):
        handles = {}
    if contract_kind == "typed":
        if not isinstance(production_schema, Mapping):
            raise RegistryTemplateSnapshotError("强类型动作缺少第 2 版动作合同")
        try:
            handles = compile_backend_action_handles(production_schema)
        except ActionTemplateProjectionError as error:
            raise RegistryTemplateSnapshotError(str(error)) from error
    definition: dict[str, Any] = {
        "feedback": _object(action.get("feedback")),
        "goal": _without_control_action_parameters(action.get("goal")),
        "goal_default": _without_control_action_parameters(
            action.get("goal_default")
        ),
        "result": _object(action.get("result")),
        "schema": production_schema,
        "type": str(action.get("type") or "").strip(),
        "handles": {
            "input": [
                _workflow_handle(handle)
                for handle in _object_list(handles.get("input"))
            ],
            "output": [
                _workflow_handle(handle)
                for handle in _object_list(handles.get("output"))
            ],
        },
        "display_name": str(
            action.get("display_name") or action.get("displayname") or ""
        ).strip(),
    }
    for field in (
        "contract_kind",
        "contract_diagnostic",
        "description",
        "uuid",
    ):
        value = action.get(field)
        if value is not None:
            definition[field] = copy.deepcopy(value)
    node_type = str(action.get("node_type") or "").strip()
    if node_type:
        definition["node_type"] = node_type
    return definition


def _without_control_action_parameters(value: Any) -> dict[str, Any]:
    """移除生产调度已由物料和 Edge 绑定承担的旧设备选择参数。

    参数说明：``value`` 是 goal 或默认值对象；返回深拷贝后的业务参数。
    """

    return {
        str(key): copy.deepcopy(item)
        for key, item in _object(value).items()
        if str(key) not in _CONTROL_ACTION_PARAMETERS
    }


def _production_action_schema(raw_schema: Any) -> Any:
    """从动作 Schema 移除旧本地设备选择字段。

    参数说明：``raw_schema`` 是动作根 Schema；返回深拷贝，保留物料锁扩展和第 2
    版动作合同扩展。
    """

    schema = copy.deepcopy(raw_schema)
    if not isinstance(schema, dict):
        return schema
    candidates = [schema]
    properties = schema.get("properties")
    if isinstance(properties, dict):
        goal = properties.get("goal")
        if isinstance(goal, dict):
            candidates.append(goal)
    for candidate in candidates:
        candidate_properties = candidate.get("properties")
        if isinstance(candidate_properties, dict):
            for name in _CONTROL_ACTION_PARAMETERS:
                candidate_properties.pop(name, None)
        required = candidate.get("required")
        if isinstance(required, list):
            candidate["required"] = [
                name for name in required if name not in _CONTROL_ACTION_PARAMETERS
            ]
    return schema


def _resource_handle(raw_handle: Mapping[str, Any]) -> dict[str, Any]:
    """规范化一个资源句柄。

    参数说明：``raw_handle`` 是 Registry 资源句柄；返回 Backend DTO 字段。
    """

    return {
        "data_key": str(raw_handle.get("data_key") or ""),
        "data_source": str(raw_handle.get("data_source") or ""),
        "data_type": str(raw_handle.get("data_type") or ""),
        "description": str(raw_handle.get("description") or ""),
        "handler_key": str(raw_handle.get("handler_key") or ""),
        "io_type": str(raw_handle.get("io_type") or ""),
        "label": str(raw_handle.get("label") or ""),
        "side": str(raw_handle.get("side") or ""),
    }


def _workflow_handle(raw_handle: Mapping[str, Any]) -> dict[str, Any]:
    """规范化动作中的一个工作流句柄定义。

    参数说明：``raw_handle`` 来自 input 或 output 集合；返回 Backend 动作 DTO。
    """

    return {
        "label": str(raw_handle.get("label") or ""),
        "data_key": str(raw_handle.get("data_key") or ""),
        "data_type": str(raw_handle.get("data_type") or ""),
        "data_source": str(raw_handle.get("data_source") or ""),
        "handler_key": str(raw_handle.get("handler_key") or ""),
    }


def _initial_parameter_schema(raw_schema: Any) -> dict[str, Any]:
    """规范化资源初始化参数 Schema。

    参数说明：``raw_schema`` 是 Registry 初始化合同；返回只含 data/config 属性的
    Backend DTO。
    """

    if not isinstance(raw_schema, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for namespace in ("data", "config"):
        schema = raw_schema.get(namespace)
        if isinstance(schema, Mapping) and isinstance(
            schema.get("properties"), Mapping
        ):
            normalized[namespace] = {"properties": dict(schema["properties"])}
    return normalized


def _object(value: Any) -> dict[str, Any]:
    """把映射值复制为普通字典，其他值返回空对象。"""

    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: Any) -> list[Any]:
    """把列表或元组复制为普通列表，其他值返回空数组。"""

    return list(value) if isinstance(value, (list, tuple)) else []


def _object_list(value: Any) -> list[Mapping[str, Any]]:
    """从数组值中过滤出对象成员。

    参数说明：``value`` 是 Registry 数组字段；返回只含映射的列表。
    """

    return [entry for entry in _array(value) if isinstance(entry, Mapping)]


def _freeze(value: Any) -> Any:
    """递归冻结 JSON 容器。

    参数说明：``value`` 是规范模板值；返回只读映射和元组组成的深冻结副本。
    """

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _detach(value: Any) -> Any:
    """递归分离不可变模板值。

    参数说明：``value`` 来自快照；返回可供 HTTP 编码或局部转换的普通容器。
    """

    if isinstance(value, Mapping):
        return {key: _detach(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_detach(child) for child in value]
    return value


__all__ = ["RegistryTemplateSnapshot", "RegistryTemplateSnapshotError"]
