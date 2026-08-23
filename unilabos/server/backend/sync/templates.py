"""把 Edge Registry 作为一个完整模板图同步到正式后端。"""

from __future__ import annotations

import gzip
import json
import os
import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

import requests

from unilabos.utils.tools import normalize_json
from unilabos.utils.tracing import inject_trace_context, span


DEVELOPER_TOKEN_ENV = "UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN"
_CONTROL_ACTION_PARAMETERS = frozenset({"unilabos_device_id"})


class TemplateSyncError(RuntimeError):
    """模板收集或后端事务同步失败。"""


@dataclass(frozen=True)
class TemplateSyncReport:
    """一次完整同步的稳定结果，供初始化 Job 和测试流程消费。"""

    device_count: int
    resource_count: int
    template_uuids: Dict[str, str]


class TemplateSynchronizer:
    """隐藏 Registry 遍历、协议映射、压缩和 HTTP 提交细节。"""

    def __init__(
        self,
        backend_address: str,
        developer_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 60.0,
    ) -> None:
        token = str(developer_token or "").strip()
        if not token:
            raise TemplateSyncError("developer token is required")
        self.backend_api = _api_base(backend_address)
        self.developer_token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync(self, registry: Any) -> TemplateSyncReport:
        """收集设备和器材模板，并通过一次 HTTP 请求事务性同步。"""

        from unilabos.app.register import collect_devices_and_resources

        device_definitions, resource_definitions = collect_devices_and_resources(
            registry
        )
        devices = _collect_templates(
            device_definitions.values(), expected_type="device"
        )
        resources = _collect_templates(
            resource_definitions.values(), expected_type="resource"
        )
        definitions = [*devices, *resources]
        if not definitions:
            raise TemplateSyncError("Edge Registry does not contain any templates")

        payload = {"resources": definitions}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.developer_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        url = f"{self.backend_api}/resource-templates"
        target = urlsplit(url)
        with span(
            "edge.http.template.sync",
            kind="client",
            attributes={
                "http.request.method": "POST",
                "http.route": "/api/v1/resource-templates",
                "server.address": target.hostname or "",
                "template.device.count": len(devices),
                "template.resource.count": len(resources),
            },
        ) as request_span:
            inject_trace_context(headers)
            response = self.session.post(
                url,
                data=gzip.compress(encoded),
                headers=headers,
                timeout=self.timeout,
            )
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass

        result = _decode_sync_response(response)
        template_uuids = {
            str(identity["name"]): str(identity["uuid"])
            for identity in result.get("templates", [])
            if isinstance(identity, Mapping)
            and identity.get("name")
            and identity.get("uuid")
        }
        expected_names = {definition["id"] for definition in definitions}
        if set(template_uuids) != expected_names:
            missing = sorted(expected_names - set(template_uuids))
            raise TemplateSyncError(
                f"backend response is missing template identities: {missing}"
            )
        return TemplateSyncReport(
            device_count=len(devices),
            resource_count=len(resources),
            template_uuids=template_uuids,
        )


def sync_registry_from_environment(
    registry: Any,
    backend_address: str,
    *,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """使用初始化 Job 注入的开发者身份同步一个 Registry。"""

    developer_token = os.environ.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def run_template_sync_command(
    arguments: Mapping[str, Any],
    *,
    backend_address: str,
    environment: Optional[Mapping[str, str]] = None,
    registry_builder: Optional[Callable[..., Any]] = None,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """执行独立初始化命令，不进入设备图和驱动启动流程。"""

    if registry_builder is None:
        from unilabos.registry.registry import build_registry

        registry_builder = build_registry
    registry = registry_builder(
        registry_paths=arguments.get("registry_path"),
        devices_dirs=arguments.get("devices"),
        # 这里必须保持纯静态收集；旧开关会实例化 PyLabRobot 器材来补配置，
        # 会把初始化 Job 意外耦合到驱动和硬件环境。
        upload_registry=False,
        complete_registry=bool(arguments.get("complete_registry", False)),
        external_only=bool(arguments.get("external_devices_only", False)),
    )
    token_source = environment if environment is not None else os.environ
    developer_token = token_source.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def _collect_templates(
    definitions: Iterable[Mapping[str, Any]], *, expected_type: str
) -> list[Dict[str, Any]]:
    templates: list[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_definition in definitions:
        definition = _template_definition(raw_definition, expected_type=expected_type)
        name = definition["id"]
        if name in seen_names:
            raise TemplateSyncError(f"duplicate {expected_type} template id: {name}")
        seen_names.add(name)
        templates.append(definition)
    templates.sort(key=lambda definition: definition["id"])
    return templates


def _template_definition(
    raw_definition: Mapping[str, Any], *, expected_type: str
) -> Dict[str, Any]:
    source = normalize_json(dict(raw_definition))
    name = str(source.get("id") or "").strip()
    if not name:
        raise TemplateSyncError(f"{expected_type} template id is required")

    resource_class = source.get("class")
    if not isinstance(resource_class, Mapping):
        resource_class = {}
    action_mappings = resource_class.get("action_value_mappings")
    if not isinstance(action_mappings, Mapping):
        action_mappings = {}

    definition: Dict[str, Any] = {
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
    schema = _initial_parameter_schema(source.get("init_param_schema"))
    if schema:
        definition["init_param_schema"] = schema
    for field in ("description", "icon", "cover"):
        value = source.get(field)
        if value is not None:
            definition[field] = value
    return definition


def _action_definition(raw_action: Any) -> Dict[str, Any]:
    action = raw_action if isinstance(raw_action, Mapping) else {}
    handles = action.get("handles")
    if not isinstance(handles, Mapping):
        handles = {}
    definition: Dict[str, Any] = {
        "feedback": _object(action.get("feedback")),
        "goal": _without_control_action_parameters(action.get("goal")),
        "goal_default": _without_control_action_parameters(
            action.get("goal_default")
        ),
        "result": _object(action.get("result")),
        "schema": _production_action_schema(action.get("schema")),
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
        "materials_need_lock": [
            str(name)
            for name in _array(action.get("materials_need_lock"))
            if str(name).strip()
        ],
    }
    node_type = str(action.get("node_type") or "").strip()
    if node_type:
        definition["node_type"] = node_type
    return definition


def _without_control_action_parameters(value: Any) -> Dict[str, Any]:
    """生产调度已用 material/Edge 绑定选定设备，不把旧选择字段当作驱动参数。"""

    return {
        str(key): copy.deepcopy(item)
        for key, item in _object(value).items()
        if str(key) not in _CONTROL_ACTION_PARAMETERS
    }


def _production_action_schema(raw_schema: Any) -> Any:
    """从 Registry action schema 中移除仅供旧本地工作流选择设备的字段。"""

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


def _resource_handle(raw_handle: Mapping[str, Any]) -> Dict[str, Any]:
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


def _workflow_handle(raw_handle: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "label": str(raw_handle.get("label") or ""),
        "data_key": str(raw_handle.get("data_key") or ""),
        "data_type": str(raw_handle.get("data_type") or ""),
        "data_source": str(raw_handle.get("data_source") or ""),
        "handler_key": str(raw_handle.get("handler_key") or ""),
    }


def _initial_parameter_schema(raw_schema: Any) -> Dict[str, Any]:
    if not isinstance(raw_schema, Mapping):
        return {}
    normalized: Dict[str, Any] = {}
    for namespace in ("data", "config"):
        schema = raw_schema.get(namespace)
        if isinstance(schema, Mapping) and isinstance(
            schema.get("properties"), Mapping
        ):
            normalized[namespace] = {"properties": dict(schema["properties"])}
    return normalized


def _object(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _object_list(value: Any) -> list[Mapping[str, Any]]:
    return [entry for entry in _array(value) if isinstance(entry, Mapping)]


def _decode_sync_response(response: Any) -> Dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TemplateSyncError(
            f"template sync returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise TemplateSyncError(
            f"template sync returned HTTP {response.status_code}: {payload}"
        )
    if not isinstance(payload, Mapping):
        raise TemplateSyncError("template sync returned a non-object response")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise TemplateSyncError(
            f"template sync returned business error {code}: {payload.get('error')}"
        )
    result = payload.get("data", payload)
    if not isinstance(result, Mapping) or not isinstance(
        result.get("templates"), list
    ):
        raise TemplateSyncError("template sync returned invalid template identities")
    return dict(result)


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise TemplateSyncError("backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"
