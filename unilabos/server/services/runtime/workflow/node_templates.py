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

结构模板与 V1 ``registry/template_projection.py`` 同口径挂在宿主节点（``host_node``）的资源模板下，
但按 dev 实际支持的结构节点生成：``condition``（``protocol/runtime/condition.py`` 的封闭表达式）、
``loop``（``protocol/runtime/loop.py`` 的 for / while，取代 V1 的 ``repeat_until``）和只做展示分组的
``group``（并行是分组的 ``meta_data.group_mode=parallel``，不是单独的节点类型），以及人工装入 / 取走 /
补量的 ``material_io``（``protocol/runtime/material_io.py``）。参数合同放在 ``meta_data.unilab.parameter_schema``
只作编辑提示，``schema`` 留空，真正的校验仍由保存整图时的结构校验完成；条件与循环的 ``goal_default`` 是能直接
保存的最小参数，节点面板拖入后即可保存再改参数；``material_io`` 必须指明库位与物料，``goal_default`` 只带模式与
确认设置，保存前要填好。

结构模板是调度权威自己的控制结构、不是设备定义（资源模板身份是 ``template_uuid("host_node")``，与上报
无关）：权威不扫描注册表，Host 还没上报（全新库）时也照样投影；host_node 条目上报后只用来取显示名、
避开同名动作。
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
#: V1 列表默认可见的节点类型（另加 dev 的循环节点 ``loop``）；显式 ``node_type`` 筛选可看到其它类型
#: （如已发布工作流 ``workflow``、展示分组 ``group``）。
DEFAULT_VISIBLE_NODE_TYPES = frozenset(
    {"ILab", "device_action", "py_script", "tool_call", "manual_confirm", "condition", "repeat_until", "loop",
     "material_io"}
)
#: 结构模板的所有者：与 V1 一样挂在宿主节点的资源模板下（host_node 条目未上报时同样投影）。
STRUCTURAL_TEMPLATE_OWNER = "host_node"
#: 结构模板的类身份：作者在 ``@workflow`` 里经 ctx 写出的控制结构（与设备动作的业务身份不冲突）。
_STRUCTURAL_TEMPLATE_CLASS = "unilabos.registry.workflows:WorkflowBuildContext"


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
    """没有统一动作 schema 的动作（ROS 动作类型、未补全的手写 YAML）：goal 属性 → target 连接点，result 属性 → source 连接点。"""

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


def _condition_parameter_schema() -> Dict[str, Any]:
    """条件节点 ``param`` 的展示合同；严格校验见 ``protocol.runtime.condition.condition_references``。"""

    return {
        "type": "object",
        "description": (
            "调度器本地求值的严格布尔条件。分支节点在 execution_policy.condition_guards 里写 "
            "{node_uuid: 本节点, equals: true / false}，只执行命中的分支。"
        ),
        "properties": {
            "version": {"type": "integer", "enum": [1]},
            "expression": {
                "type": "object",
                "description": (
                    "封闭 JSON 表达式，不执行 Python 字符串：{lit: 标量}、{value: JSON}、{input: 入口参数名}、"
                    "{node_uuid: 节点}（该节点本次成功结果）、{field: 表达式, name: 字段名}、{not: 表达式}、"
                    "{op: eq|ne|lt|le|gt|ge|and|or, args: [表达式, ...]}；结果必须是布尔值。"
                ),
            },
        },
        "required": ["version", "expression"],
        "additionalProperties": False,
    }


def _material_io_parameter_schema() -> Dict[str, Any]:
    """人工装入 / 取走 / 补量节点 ``param`` 的展示合同；严格校验见 ``protocol.runtime.material_io``。"""

    material = {
        "description": "物料：{uuid: 物料 UUID}、物料 UUID 或物料业务 ID；也可以是工作流输入或上游步骤的 material 结果",
        "oneOf": [{"type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"}},
                   "required": ["uuid"]}, {"type": "string", "minLength": 1}],
    }
    return {
        "type": "object",
        "description": (
            "调度器在权威侧本地执行：锁定 mount 的库位（unload / refill 另锁 material），停下等人工确认，"
            "确认后在一个事务里装入、取走或补量；不派发设备动作。确认前不改库存，拒绝 / 超时节点失败。"
        ),
        "properties": {
            "mode": {"type": "string", "enum": ["load", "unload", "refill"]},
            "mount": {**material, "description": "挂载物料（库位所属的物料）"},
            "site": {"type": "string", "minLength": 1, "description": "mount 上的库位名或库位 UUID"},
            "resource_template": {"type": "string", "minLength": 1, "description": "load：新物料的资源模板名或 UUID"},
            "material": {**material, "description": "unload / refill：要取走或补量的物料（必须在 site 上）"},
            "material_name": {"type": "string", "minLength": 1, "description": "load：新物料名，缺省按模板名加时间生成"},
            "barcode": {"type": "string", "minLength": 1, "description": "load：新物料条码"},
            "reagent": {
                "type": "object",
                "description": (
                    "load 可选：quantity + quantity_unit，再加 reagent_info_uuid 或 name / cas 作试剂身份；"
                    "refill 必填：只有 quantity + quantity_unit"
                ),
            },
            "disposition": {"type": "string", "enum": ["detach", "soft_delete"], "description": "unload 必填"},
            "scope": {"type": "string", "enum": ["material_only", "material_with_contents"],
                      "description": "unload 必填；detach 只支持 material_only"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600},
            "assignee_user_ids": {"type": "array", "items": {"type": "string"},
                                  "description": "可确认的用户；[] 表示任何人可确认（必须显式给出）"},
        },
        "required": ["mode", "mount", "site", "assignee_user_ids"],
        "additionalProperties": False,
    }


def structural_templates(
    resource_summary: Mapping[str, Any],
    *,
    reserved_names: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """宿主节点名下的结构模板（条件 / 循环 / 人工物料 / 分组）及其连接点。

    条件、循环与人工物料带一对 ready 连接点，与设备动作模板一样可以连顺序依赖（组合调用的入口 / 出口
    也需要它）；分组不参与执行依赖，没有连接点。``reserved_names`` 是宿主节点已有的动作名，
    同名时跳过结构模板，避免同一资源模板下模板名冲突。
    """

    from unilabos.protocol.runtime.loop import LoopSpec

    resource_name = str(resource_summary["name"])
    resource_uuid = str(resource_summary["uuid"])
    specs: List[Dict[str, Any]] = [
        {
            "name": "condition",
            "display_name": "条件",
            "description": (
                "调度器本地求值并选择唯一分支的结构化条件；默认表达式恒为真，放入后改写 param.expression，"
                "分支节点用 execution_policy.condition_guards 引用本节点。"
            ),
            "goal_default": {"version": 1, "expression": {"lit": True}},
            "executor_kind": "condition",
            "parameter_schema": _condition_parameter_schema(),
        },
        {
            "name": "loop",
            "display_name": "循环",
            "description": (
                "循环容器：循环体是 parent_uuid 指向本节点的子节点；mode=for 固定 count 轮，mode=while "
                "每轮开始前求值 condition（device_state / node_output），max_iterations 为安全上限。"
            ),
            "goal_default": {"mode": "for", "count": 1},
            "executor_kind": "loop",
            "parameter_schema": LoopSpec.model_json_schema(),
        },
        {
            "name": "material_io",
            "display_name": "人工物料",
            "description": (
                "人工装入 / 取走 / 补量：调度器锁定库位后停下等人工确认，确认后改库存，不派发设备动作。"
                "goal_default 只带模式与确认设置，保存前填好 mount / site 以及该模式的必填字段。"
            ),
            "goal_default": {"mode": "load", "timeout_seconds": 3600, "assignee_user_ids": []},
            "executor_kind": "material_io",
            "parameter_schema": _material_io_parameter_schema(),
        },
        {
            "name": "group",
            "display_name": "分组",
            "description": (
                "组织节点的展示分组，不参与执行依赖；成员以 parent_uuid 指向本节点，"
                "节点 meta_data.group_mode=parallel 表示并行分组（ctx.parallel）。"
            ),
            "goal_default": {},
            # 分组不执行：没有执行器、参数合同和连接点
            "executor_kind": None,
            "parameter_schema": None,
        },
    ]
    reserved = {str(name).lower() for name in reserved_names}
    templates: List[Dict[str, Any]] = []
    handles: List[Dict[str, Any]] = []
    for spec in specs:
        name = spec["name"]
        if name in reserved:
            logger.warning("[NodeTemplates] %s 已有同名动作 %s，跳过同名结构模板", resource_name, name)
            continue
        template_uuid = action_template_uuid(resource_name, name)
        unilab: Dict[str, Any] = {"framework_owner_only": True, "resource_template": dict(resource_summary)}
        if spec["executor_kind"] is not None:
            unilab["executor_kind"] = spec["executor_kind"]
            unilab["parameter_schema"] = spec["parameter_schema"]
        templates.append(
            {
                "uuid": template_uuid,
                "resource_template_uuid": resource_uuid,
                "name": name,
                "display_name": spec["display_name"],
                "description": spec["description"],
                "class": _STRUCTURAL_TEMPLATE_CLASS,
                "goal": {},
                "goal_default": spec["goal_default"],
                "feedback": {},
                "result": {},
                "schema": None,
                "type": name,
                "node_type": name,
                "meta_data": {"unilab": unilab},
            }
        )
        if spec["executor_kind"] is not None:
            for io_type in ("target", "source"):
                handle = _ready_handle(io_type)
                handle["uuid"] = action_handle_uuid(template_uuid, io_type=io_type, handle_key="ready")
                handle["workflow_node_template_uuid"] = template_uuid
                handles.append(handle)
    return templates, handles


def project_registry_templates(
    payloads: Iterable[Mapping[str, Any]],
    *,
    resource_template_uuid_of: Callable[[str], str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """生效的设备注册表条目 → (节点模板, 连接点模板)。无法投影的动作记日志后跳过。

    结构模板（见 ``structural_templates``）总是挂在宿主节点名下：host_node 条目已上报时用它的显示名
    并避开它的动作名，没有上报时用固定的宿主节点摘要。
    """

    from unilabos.registry.action_template_projection import (
        ActionTemplateProjectionError,
        compile_action_template_handles,
        goal_parameter_schema,
    )

    templates: List[Dict[str, Any]] = []
    handles: List[Dict[str, Any]] = []
    owner_projected = False
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
        if device_name == STRUCTURAL_TEMPLATE_OWNER:
            structural, structural_handles = structural_templates(resource_summary, reserved_names=seen_names)
            templates.extend(structural)
            handles.extend(structural_handles)
            owner_projected = True
    if not owner_projected:
        # 权威自带的控制结构不依赖 host_node 条目是否已上报
        structural, structural_handles = structural_templates(
            {
                "uuid": resource_template_uuid_of(STRUCTURAL_TEMPLATE_OWNER),
                "name": STRUCTURAL_TEMPLATE_OWNER,
                "display_name": STRUCTURAL_TEMPLATE_OWNER,
            }
        )
        templates.extend(structural)
        handles.extend(structural_handles)
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
    "STRUCTURAL_TEMPLATE_OWNER",
    "project_registry_templates",
    "structural_templates",
    "template_summary",
]
