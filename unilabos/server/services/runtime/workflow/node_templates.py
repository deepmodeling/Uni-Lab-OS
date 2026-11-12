"""设备动作节点模板：dev 注册表 → V1 ``/workflow-node-templates`` 投影。

V1 的节点模板来自设备注册表的模板投影（每个设备类 × 动作一个模板，外加 ready / 参数
连接点）。dev 原来的画布节点不引用模板（``type=device_action`` + ``action_name``）。统一
分支把生效注册表条目投影进 ``runtime.db`` 已有的 ``workflow_node_template`` /
``workflow_handle_template`` 表（``authority_id = registry-template-projection``）：

- 模板 / 连接点 UUID 用 V1 ``template_identity`` 的固定命名空间（设备名 + 动作名派生），
  与 V1 / Backend 一致；资源模板 UUID 沿用 dev 注册表的 ``template_uuid(name)``；
- 第 2 版强类型动作合同用 ``compile_action_template_handles``；旧式动作按 goal / result
  的 JSON Schema 属性生成数据连接点，外加一对 ready 控制连接点；
- 注册表内容指纹不变时不重算；不再生效的模板软删除，引用它的旧节点仍可读取历史图。

模板节点的 ``node_type`` 默认 ``ILab``（dev 执行计划按别名归一为 ``device_action``），
因此 V1 控制台以模板创建的节点可以直接被 dev 调度执行。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Dict, List, Optional, Tuple

from unilabos.protocol.runtime.workflow import validate_uuid
from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_handle_uuid,
    action_template_uuid,
)

logger = logging.getLogger(__name__)

REGISTRY_TEMPLATE_AUTHORITY = "registry-template-projection"
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100
#: V1 列表默认可见的节点类型；显式 ``node_type`` 筛选可看到其它类型（如已发布工作流 ``workflow``）。
DEFAULT_VISIBLE_NODE_TYPES = frozenset(
    {"ILab", "device_action", "py_script", "tool_call", "manual_confirm", "condition", "repeat_until", "material_io"}
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ready_handle(io_type: str) -> Dict[str, Any]:
    return {
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "ready",
        "description": None,
        "type": "default",
        "required": False,
        "data_source": None,
        "data_key": None,
        "meta_data": {},
    }


def _schema_handles(schema: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """旧式动作（无第 2 版合同）：goal 属性 → target 连接点，result 属性 → source 连接点。"""

    from unilabos.server.services.runtime.workflow.handle_projection import workflow_handle_type

    properties = _mapping(schema.get("properties"))
    handles: List[Dict[str, Any]] = [_ready_handle("target"), _ready_handle("source")]
    order = 0
    for section, io_type, data_source in (("goal", "target", "goal"), ("result", "source", "executor")):
        section_schema = _mapping(properties.get(section))
        required = section_schema.get("required") if isinstance(section_schema.get("required"), list) else []
        for key, value_schema in _mapping(section_schema.get("properties")).items():
            if not isinstance(key, str) or not key or key == "ready":
                continue
            value_schema = _mapping(value_schema)
            handles.append(
                {
                    "handle_key": key,
                    "io_type": io_type,
                    "display_name": str(value_schema.get("title") or key),
                    "description": value_schema.get("description"),
                    "type": workflow_handle_type(value_schema),
                    "required": io_type == "target" and key in required,
                    "data_source": data_source,
                    "data_key": key,
                    "meta_data": {"unilab": {"value_schema": dict(value_schema), "contract_order": order}},
                }
            )
            order += 1
    return handles


def project_registry_templates(
    payloads: Iterable[Mapping[str, Any]],
    *,
    resource_template_uuid_of: Callable[[str], str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """生效的设备注册表条目 → (节点模板, 连接点模板)。无法投影的动作记日志后跳过。"""

    from unilabos.registry.action_template_projection import (
        ActionTemplateProjectionError,
        compile_action_template_handles,
        goal_parameter_schema,
    )

    templates: List[Dict[str, Any]] = []
    handles: List[Dict[str, Any]] = []
    for payload in payloads:
        if str(payload.get("registry_type") or "") != "device":
            continue
        device_name = str(payload.get("id") or "")
        klass = _mapping(payload.get("class"))
        actions = _mapping(klass.get("action_value_mappings"))
        if not device_name or not actions:
            continue
        resource_uuid = resource_template_uuid_of(device_name)
        resource_summary = {
            "uuid": resource_uuid,
            "name": device_name,
            "display_name": str(payload.get("display_name") or payload.get("name") or device_name),
        }
        class_identity = str(klass.get("module") or device_name)
        seen_names: set[str] = set()
        for action_name, raw_action in sorted(actions.items()):
            action = _mapping(raw_action)
            if not isinstance(action_name, str) or not action_name or action_name.lower() in seen_names:
                continue
            try:
                template_uuid = action_template_uuid(device_name, action_name)
            except TemplateIdentityError:
                logger.warning("[NodeTemplates] 设备 %s 动作 %r 名称不能生成稳定身份，跳过", device_name, action_name)
                continue
            schema = _mapping(action.get("schema"))
            contract = _mapping(schema.get("x-unilabos-action-contract"))
            try:
                if contract.get("version") == 2:
                    action_handles = compile_action_template_handles(
                        schema, node_business_key=(resource_uuid, action_name), resource_template_identity_resolver=None,
                    )
                    parameter_schema = goal_parameter_schema(schema)
                else:
                    action_handles = _schema_handles(schema)
                    parameter_schema = dict(_mapping(_mapping(schema.get("properties")).get("goal")))
            except (ActionTemplateProjectionError, TypeError, ValueError) as exc:
                logger.warning("[NodeTemplates] 设备 %s 动作 %s 投影失败，跳过：%s", device_name, action_name, exc)
                continue
            seen_names.add(action_name.lower())
            unilab: Dict[str, Any] = {
                "contract_kind": "typed" if contract.get("version") == 2 else "legacy",
                "parameter_schema": parameter_schema,
                "resource_template": resource_summary,
            }
            if action.get("always_free"):
                unilab["always_free"] = True
            templates.append(
                {
                    "uuid": template_uuid,
                    "resource_template_uuid": resource_uuid,
                    "name": action_name,
                    "display_name": str(action.get("display_name") or action.get("displayname") or action_name),
                    "description": action.get("description") or schema.get("description"),
                    "class": class_identity,
                    "goal": dict(_mapping(action.get("goal"))),
                    "goal_default": dict(_mapping(action.get("goal_default"))),
                    "feedback": dict(_mapping(action.get("feedback"))),
                    "result": dict(_mapping(action.get("result"))),
                    "schema": parameter_schema,
                    "type": str(action.get("type") or "UniLabJsonCommand"),
                    "node_type": str(action.get("node_type") or "ILab"),
                    "meta_data": {"unilab": unilab},
                }
            )
            for handle in action_handles:
                handle = {key: value for key, value in handle.items() if key != "node_business_key"}
                handle["uuid"] = action_handle_uuid(
                    template_uuid, io_type=str(handle["io_type"]), handle_key=str(handle["handle_key"]),
                )
                handle["workflow_node_template_uuid"] = template_uuid
                handles.append(handle)
    return templates, handles


def _omit_none(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_omit_none(item) for item in value]
    return value


def template_summary(template: Mapping[str, Any]) -> Dict[str, Any]:
    """V1 列表摘要：资源模板摘要来自 ``meta_data.unilab.resource_template``（或 ``meta_data.resource_template``）。"""

    meta_data = _mapping(template.get("meta_data"))
    resource = _mapping(_mapping(meta_data.get("unilab")).get("resource_template")) or _mapping(
        meta_data.get("resource_template")
    )
    return _omit_none(
        {
            "uuid": template["uuid"],
            "name": template["name"],
            "display_name": template.get("display_name") or template["name"],
            "description": template.get("description"),
            "type": template["type"],
            "node_type": template["node_type"],
            "icon": template.get("icon"),
            "resource_template": {
                "uuid": resource.get("uuid") or template.get("resource_template_uuid"),
                "name": resource.get("name"),
                "display_name": resource.get("display_name"),
            },
        }
    )


class NodeTemplateCatalog:
    """混入 ``WorkflowService``：注册表模板同步与 V1 模板查询。"""

    _registry_template_fingerprint: Optional[str] = None

    def sync_registry_templates(self) -> None:
        """把生效注册表的设备动作投影写入模板表；注册表未就绪时跳过（只读已有投影）。"""

        from unilabos.server.services.runtime.registry import get_registry_service, template_uuid
        from unilabos.server.services.runtime.workflow.store import utc_now

        registry = get_registry_service()
        if registry is None:
            return
        fingerprint = registry.active_fingerprint()
        if fingerprint == self._registry_template_fingerprint:
            return
        templates, handles = project_registry_templates(
            registry.active_payloads("device"), resource_template_uuid_of=template_uuid,
        )
        now = utc_now()
        with self.transaction() as conn:  # type: ignore[attr-defined]
            active_ids = [item["uuid"] for item in templates]
            marks = ",".join("?" for _ in active_ids) or "''"
            # 不再生效的注册表模板先软删除，避免 (资源模板, 动作名) 唯一索引与改名冲突
            conn.execute(
                f"UPDATE workflow_node_template SET deleted_at = ?, update_time = ? "
                f"WHERE authority_id = ? AND deleted_at IS NULL AND uuid NOT IN ({marks})",
                (now, now, REGISTRY_TEMPLATE_AUTHORITY, *active_ids),
            )
            conn.execute(
                f"UPDATE workflow_handle_template SET deleted_at = ?, update_time = ? "
                f"WHERE authority_id = ? AND deleted_at IS NULL AND workflow_node_template_uuid NOT IN ({marks})",
                (now, now, REGISTRY_TEMPLATE_AUTHORITY, *active_ids),
            )
            if templates:
                self._upsert_published_templates(  # type: ignore[attr-defined]
                    conn, templates, handles, authority_id=REGISTRY_TEMPLATE_AUTHORITY, now=now,
                )
        self._registry_template_fingerprint = fingerprint

    def _sync_registry_templates_best_effort(self) -> None:
        try:
            self.sync_registry_templates()
        except Exception:  # noqa: BLE001 - 投影失败不能阻断读取既有模板
            logger.exception("[NodeTemplates] 注册表模板投影失败，继续使用已有模板")

    def _active_template_rows(self) -> List[Dict[str, Any]]:
        with self.write_lock:  # type: ignore[attr-defined]
            rows = self.connection.execute(  # type: ignore[attr-defined]
                "SELECT * FROM workflow_node_template WHERE deleted_at IS NULL"
            ).fetchall()
        return [self._node_template_row(row) for row in rows]  # type: ignore[attr-defined]

    def list_node_templates(
        self,
        *,
        page: int = 0,
        page_size: int = 0,
        keyword: str = "",
        resource_template_uuid: Optional[str] = None,
        action_type: str = "",
        node_type: str = "",
    ) -> Dict[str, Any]:
        """V1 节点模板摘要页：创建时间降序、UUID 降序；未指定 ``node_type`` 时只列默认可见类型。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        self._sync_registry_templates_best_effort()
        normalized_page = _DEFAULT_PAGE if page < 1 else page
        normalized_size = _DEFAULT_PAGE_SIZE if page_size < 1 else min(page_size, _MAX_PAGE_SIZE)
        if resource_template_uuid is not None:
            try:
                resource_template_uuid = validate_uuid(resource_template_uuid)
            except (TypeError, ValueError):
                raise WorkflowError("invalid_input", detail="resource_template_uuid 必须是有效 UUID") from None
        needle = keyword.strip().lower()
        wanted_type = action_type.strip()
        wanted_node_type = node_type.strip()
        matches = []
        for template in sorted(
            self._active_template_rows(), key=lambda item: (str(item.get("create_time") or ""), str(item["uuid"])),
            reverse=True,
        ):
            if resource_template_uuid is not None and template.get("resource_template_uuid") != resource_template_uuid:
                continue
            if needle and needle not in str(template.get("name", "")).lower() and needle not in str(
                template.get("display_name", "")
            ).lower():
                continue
            if wanted_type and template.get("type") != wanted_type:
                continue
            candidate_node_type = str(template.get("node_type") or "")
            if wanted_node_type:
                if candidate_node_type != wanted_node_type:
                    continue
            elif candidate_node_type not in DEFAULT_VISIBLE_NODE_TYPES:
                continue
            matches.append(template)
        offset = (normalized_page - 1) * normalized_size
        return {
            "items": [template_summary(item) for item in matches[offset: offset + normalized_size]],
            "has_more": len(matches) > offset + normalized_size,
            "page": normalized_page,
            "page_size": normalized_size,
        }

    def get_node_template_record(self, template_uuid: str) -> Dict[str, Any]:
        """读取一个活动节点模板（含注册表投影与已发布工作流模板）；不存在为 ``not_found``。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        try:
            identity = validate_uuid(template_uuid)
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input", detail="template_uuid 必须是有效 UUID") from None
        for attempt in range(2):
            with self.write_lock:  # type: ignore[attr-defined]
                row = self.connection.execute(  # type: ignore[attr-defined]
                    "SELECT * FROM workflow_node_template WHERE uuid = ? AND deleted_at IS NULL", (identity,),
                ).fetchone()
            if row is not None:
                return self._node_template_row(row)  # type: ignore[attr-defined]
            if attempt == 0:
                self._sync_registry_templates_best_effort()
        raise WorkflowError("not_found", detail=f"工作流节点模板不存在或已被删除（模板 UUID：{identity}）")

    def get_node_template(self, template_uuid: str) -> Dict[str, Any]:
        """V1 详情：``{template, handles}``。"""

        template = self.get_node_template_record(template_uuid)
        with self.write_lock:  # type: ignore[attr-defined]
            rows = self.connection.execute(  # type: ignore[attr-defined]
                "SELECT * FROM workflow_handle_template WHERE workflow_node_template_uuid = ? AND deleted_at IS NULL",
                (template["uuid"],),
            ).fetchall()
        handles = [self._handle_template_row(row) for row in rows]  # type: ignore[attr-defined]
        handles.sort(
            key=lambda item: (
                0 if item.get("handle_key") == "ready" else 1,
                int(_mapping(_mapping(item.get("meta_data")).get("unilab")).get("contract_order") or 0),
                str(item.get("io_type")),
                str(item.get("handle_key")),
            )
        )
        return _omit_none({"template": template, "handles": handles})


__all__ = [
    "DEFAULT_VISIBLE_NODE_TYPES",
    "NodeTemplateCatalog",
    "REGISTRY_TEMPLATE_AUTHORITY",
    "project_registry_templates",
    "template_summary",
]
