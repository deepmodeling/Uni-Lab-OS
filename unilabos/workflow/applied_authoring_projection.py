"""已应用创作投影（Applied Authoring Projection）的固定点合并内核。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from unilabos.workflow.authoring_kernel import AuthoringCatalogAction
from unilabos.workflow.models import validate_uuid

_DATABASE_OPERATION_FIELDS = {
    "authority_id",
    "create_time",
    "deleted_at",
    "update_time",
}
_NODE_TEMPLATE_NULLABLE_READ_FIELDS = {
    "class",
    "description",
    "footer",
    "header",
    "icon",
    "schema",
}
_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS = {
    "data_key",
    "data_source",
    "description",
}
_NODE_NULLABLE_READ_FIELDS = {
    "action_name",
    "action_type",
    "description",
    "footer",
    "icon",
    "material_uuid",
    "parent_uuid",
    "script",
}
_EDGE_NULLABLE_READ_FIELDS = {"description"}
_RETAINED_PROJECTION_FIELDS = {"create_time", "update_time", "workflow_uuid"}
# 节点图拥有字段只由同 UUID 已应用工作流图决定，源码编译默认值无权覆盖。
_NODE_GRAPH_OWNED_FIELDS = {"pose", "execution_policy", "disabled", "minimized"}


class AppliedAuthoringProjectionError(ValueError):
    """已应用创作投影不完整、漂移或不能安全复用。"""

    def __init__(self, code: str, message: str):
        """保存稳定错误码和中文原因。

        参数说明：``code`` 是产品诊断码，``message`` 是面向调用者的中文原因。
        返回：无；异常保持失败关闭（Fail-closed），不会产生部分合并结果。
        """

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AppliedAuthoringProjection:
    """完成固定点合并后的四类工作流图实体集合。"""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    node_templates: list[dict[str, Any]]
    handle_templates: list[dict[str, Any]]


def reconcile_applied_authoring_projection(
    *,
    workflow_uuid: str,
    applied_graph: Mapping[str, Any],
    generated_nodes: Sequence[Mapping[str, Any]],
    generated_edges: Sequence[Mapping[str, Any]],
    action_catalog: Mapping[str, AuthoringCatalogAction],
    compatible_catalog_replacements: set[str] | None = None,
) -> AppliedAuthoringProjection:
    """把新生成语义与已应用数据库读投影合并为可信固定点。

    参数说明：``workflow_uuid`` 是当前工作流（Workflow）权威身份；
    ``applied_graph`` 是已应用五集合；``generated_nodes``/``generated_edges`` 是
    本轮从源码生成的写形状；``action_catalog`` 按节点 UUID 提供当前不可变目录；
    ``compatible_catalog_replacements`` 是已由组合调用 pin 认证、可整代升级的
    已发布工作流模板 UUID 集合。
    返回四类已排序投影：保留实体沿用数据库时间和空值形状，新实体沿用当前生成
    形状。异常：身份、父关系或目录代际不一致时抛出
    ``AppliedAuthoringProjectionError``，不返回部分投影。
    形状；重复身份、外部工作流、非法时间或目录语义漂移抛出稳定错误且不返回
    部分结果。
    """

    identity = validate_uuid(workflow_uuid)
    applied_nodes = _index_applied_entities(
        applied_graph.get("nodes"),
        collection_label="工作流节点",
        workflow_uuid=identity,
    )
    applied_edges = _index_applied_entities(
        applied_graph.get("edges"),
        collection_label="工作流边",
        workflow_uuid=identity,
    )
    applied_templates = _index_applied_entities(
        applied_graph.get("node_templates"),
        collection_label="节点模板",
    )
    applied_handles = _index_applied_entities(
        applied_graph.get("handle_templates"),
        collection_label="连接点模板",
    )
    _validate_applied_handle_parents(
        handles=applied_handles,
        templates=applied_templates,
    )

    generated_node_index = _index_generated_entities(
        generated_nodes,
        collection_label="生成工作流节点",
    )
    generated_edge_index = _index_generated_entities(
        generated_edges,
        collection_label="生成工作流边",
    )
    if set(generated_node_index) != set(action_catalog):
        _fail("candidate_invalid", "生成节点与动作目录索引不一致")

    # ``nodes`` 与 ``edges`` 是仅保留数据库读形状、不覆盖新业务语义的候选实体。
    nodes = [
        _retained_node_entity(
            generated,
            applied_nodes.get(node_uuid),
            action=action_catalog[node_uuid],
        )
        for node_uuid, generated in generated_node_index.items()
    ]
    edges = [
        _retained_runtime_entity(
            generated,
            applied_edges.get(edge_uuid),
            nullable_fields=_EDGE_NULLABLE_READ_FIELDS,
            exact_shape_fields=set(),
        )
        for edge_uuid, generated in generated_edge_index.items()
    ]
    node_templates, handle_templates = _catalog_projection(
        action_catalog=action_catalog,
        applied_templates=applied_templates,
        applied_handles=applied_handles,
        compatible_replacements=(compatible_catalog_replacements or set()),
    )
    return AppliedAuthoringProjection(
        nodes=nodes,
        edges=sorted(edges, key=_entity_uuid_sort_key),
        node_templates=node_templates,
        handle_templates=handle_templates,
    )


def _index_applied_entities(
    values: Any,
    *,
    collection_label: str,
    workflow_uuid: str | None = None,
) -> dict[str, dict[str, Any]]:
    """校验并按 UUID 索引一类已应用实体。

    参数说明：``values`` 是不可信集合，``collection_label`` 用于中文诊断；
    ``workflow_uuid`` 非空时同时验证实体可选归属字段。返回保持输入顺序的深拷贝
    索引；非数组、非对象、重复 UUID、外部归属或非法数据库时间关闭式失败。
    """

    if not isinstance(values, list):
        _fail("candidate_invalid", f"已应用{collection_label}必须是数组")
    indexed: dict[str, dict[str, Any]] = {}
    for raw_entity in values:
        if not isinstance(raw_entity, Mapping):
            _fail("candidate_invalid", f"已应用{collection_label}必须是对象")
        entity = deepcopy(dict(raw_entity))
        try:
            entity_uuid = validate_uuid(entity.get("uuid"))
        except (TypeError, ValueError):
            _fail("candidate_invalid", f"已应用{collection_label} UUID 无效")
        if entity_uuid in indexed:
            _fail("candidate_invalid", f"已应用{collection_label} UUID 重复")
        if workflow_uuid is not None and "workflow_uuid" in entity:
            try:
                owner_uuid = validate_uuid(entity["workflow_uuid"])
            except (TypeError, ValueError):
                _fail("candidate_invalid", f"已应用{collection_label}归属 UUID 无效")
            if owner_uuid != workflow_uuid:
                _fail("candidate_invalid", f"已应用{collection_label}属于其他工作流")
        _validate_database_times(entity, collection_label=collection_label)
        indexed[entity_uuid] = entity
    return indexed


def _index_generated_entities(
    values: Sequence[Mapping[str, Any]],
    *,
    collection_label: str,
) -> dict[str, dict[str, Any]]:
    """按 UUID 索引编译器新生成的实体并拒绝身份覆盖。

    参数说明：``values`` 是本进程生成集合，``collection_label`` 用于中文诊断。
    返回不共享容器的有序索引；实体非对象、UUID 非法或重复时关闭式失败。
    """

    indexed: dict[str, dict[str, Any]] = {}
    for raw_entity in values:
        if not isinstance(raw_entity, Mapping):
            _fail("candidate_invalid", f"{collection_label}必须是对象")
        entity = deepcopy(dict(raw_entity))
        try:
            entity_uuid = validate_uuid(entity.get("uuid"))
        except (TypeError, ValueError):
            _fail("candidate_invalid", f"{collection_label} UUID 无效")
        if entity_uuid in indexed:
            _fail("candidate_invalid", f"{collection_label} UUID 重复")
        if any(field in entity for field in _RETAINED_PROJECTION_FIELDS):
            _fail("candidate_invalid", f"{collection_label}伪造了数据库投影字段")
        indexed[entity_uuid] = entity
    return indexed


def _validate_database_times(
    entity: Mapping[str, Any],
    *,
    collection_label: str,
) -> None:
    """验证已应用实体中出现的数据库时间。

    参数说明：``entity`` 是一个已应用读实体，``collection_label`` 用于中文诊断。
    返回：无；时间缺失合法，出现时必须是带时区 ISO-8601 字符串，否则关闭式失败。
    """

    for field_name in ("create_time", "update_time"):
        if field_name not in entity:
            continue
        raw_time = entity[field_name]
        if not isinstance(raw_time, str):
            _fail("candidate_invalid", f"已应用{collection_label}数据库时间无效")
        try:
            parsed_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            _fail("candidate_invalid", f"已应用{collection_label}数据库时间无效")
        if parsed_time.tzinfo is None:
            _fail("candidate_invalid", f"已应用{collection_label}数据库时间缺少时区")


def _validate_applied_handle_parents(
    *,
    handles: Mapping[str, Mapping[str, Any]],
    templates: Mapping[str, Mapping[str, Any]],
) -> None:
    """确认已应用连接点（Handle）都属于已应用节点模板。

    参数说明：``handles`` 与 ``templates`` 是已关闭重复身份的索引。返回：无；
    父模板 UUID 非法或不存在时关闭式失败，避免孤儿连接点被选择性忽略。
    """

    for handle in handles.values():
        try:
            parent_uuid = validate_uuid(handle.get("workflow_node_template_uuid"))
        except (TypeError, ValueError):
            _fail("candidate_invalid", "已应用连接点模板父身份无效")
        if parent_uuid not in templates:
            _fail("candidate_invalid", "已应用连接点模板引用未知节点模板")


def _retained_runtime_entity(
    generated: Mapping[str, Any],
    applied: Mapping[str, Any] | None,
    *,
    nullable_fields: set[str],
    exact_shape_fields: set[str],
) -> dict[str, Any]:
    """合并一个节点或边的业务语义与持久读取形状。

    参数说明：``generated`` 是当前源码语义，``applied`` 是同 UUID 旧读投影；
    ``nullable_fields`` 是 Backend ``omitempty`` 可空白名单；
    ``exact_shape_fields`` 是同 UUID 已应用实体完全拥有的字段白名单。返回独立
    实体：新实体原样返回；保留实体复制原数据库时间/归属，精确保留图拥有字段
    的缺失或显式值，并仅在当前值仍为空时沿用旧可空字段形状。
    """

    result = deepcopy(dict(generated))
    if applied is None:
        return result
    for field_name in _RETAINED_PROJECTION_FIELDS:
        if field_name in applied:
            result[field_name] = deepcopy(applied[field_name])
        else:
            result.pop(field_name, None)
    for field_name in exact_shape_fields:
        if field_name in applied:
            result[field_name] = deepcopy(applied[field_name])
        else:
            result.pop(field_name, None)
    for field_name in nullable_fields:
        if result.get(field_name) is not None:
            continue
        if field_name in applied:
            result[field_name] = deepcopy(applied[field_name])
        else:
            result.pop(field_name, None)
    return result


def _retained_node_entity(
    generated: Mapping[str, Any],
    applied: Mapping[str, Any] | None,
    *,
    action: AuthoringCatalogAction,
) -> dict[str, Any]:
    """保留节点读形状并收敛可由目录无歧义恢复的旧空字段。

    参数：generated 是源码生成节点，applied 是同 UUID 输入读投影，
    action 是本轮冻结的动作目录项。返回：图拥有字段和数据库投影按公共
    合并规则保留；当旧字段为 null/省略、生成值又恰好等于当前目录默认值时，
    精确保留旧 wire 形状。这样不会吞掉作者显式展示覆盖或真实目录演进。
    """

    result = _retained_runtime_entity(
        generated,
        applied,
        nullable_fields=_NODE_NULLABLE_READ_FIELDS,
        exact_shape_fields=_NODE_GRAPH_OWNED_FIELDS,
    )
    if applied is None:
        return result
    template = action.template
    template_defaults = {
        "action_name": template.get("name"),
        "action_type": str(template.get("type") or "UniLabJsonCommand"),
        "description": template.get("description"),
        "footer": template.get("footer"),
        "icon": template.get("icon"),
    }
    for field_name, default_value in template_defaults.items():
        if (
            applied.get(field_name) is None
            and generated.get(field_name) == default_value
        ):
            if field_name in applied:
                result[field_name] = None
            else:
                result.pop(field_name, None)
    return result


def _catalog_projection(
    *,
    action_catalog: Mapping[str, AuthoringCatalogAction],
    applied_templates: Mapping[str, Mapping[str, Any]],
    applied_handles: Mapping[str, Mapping[str, Any]],
    compatible_replacements: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成最小且不可混代的模板与连接点（Handle）目录投影。

    参数说明：``action_catalog`` 是当前节点到目录聚合的索引；两个已应用索引是
    可复用读投影；``compatible_replacements`` 已在调用 pin 边界认证旧聚合。
    返回按 UUID 排序的模板和连接点：严格等价时整体复用旧读形状，经认证的合同
    演进整体换为当前代际；其他漂移或混用即 ``template_catalog_mismatch``。
    异常：目录缺项、未认证漂移或跨代混用时抛出
    ``AppliedAuthoringProjectionError``。
    """

    # ``current_actions`` 按模板身份去重，确保同一模板只能投影一个当前目录代际。
    current_actions: dict[str, AuthoringCatalogAction] = {}
    for action in action_catalog.values():
        try:
            template_uuid = validate_uuid(action.template.get("uuid"))
        except (AttributeError, TypeError, ValueError):
            _fail("template_catalog_mismatch", "当前节点模板身份无效")
        previous_action = current_actions.get(template_uuid)
        if previous_action is not None and previous_action != action:
            _fail("template_catalog_mismatch", "同一节点模板出现多个当前目录代际")
        current_actions[template_uuid] = action

    projected_templates: list[dict[str, Any]] = []
    projected_handles: list[dict[str, Any]] = []
    for template_uuid in sorted(current_actions):
        action = current_actions[template_uuid]
        current_template = action.detached_template()
        current_handles = _index_current_handles(action.detached_handles())
        applied_template = applied_templates.get(template_uuid)
        if applied_template is None:
            projected_templates.append(current_template)
            projected_handles.extend(current_handles.values())
            continue
        applied_generation_handles = {
            handle_uuid: deepcopy(dict(handle))
            for handle_uuid, handle in applied_handles.items()
            if handle.get("workflow_node_template_uuid") == template_uuid
        }
        template_difference_fields = _catalog_entity_difference_fields(
            applied_template,
            current_template,
            nullable_fields=_NODE_TEMPLATE_NULLABLE_READ_FIELDS,
        )
        if set(applied_generation_handles) != set(current_handles):
            template_difference_fields.append("handle_templates")
        if template_difference_fields:
            if template_uuid in compatible_replacements:
                projected_templates.append(current_template)
                projected_handles.extend(current_handles.values())
                continue
            _fail(
                "template_catalog_mismatch",
                "已应用节点模板目录语义已漂移: "
                f"{template_uuid}; 字段={','.join(template_difference_fields)}",
            )
        if any(
            not _catalog_entity_equal(
                applied_generation_handles[handle_uuid],
                current_handles[handle_uuid],
                nullable_fields=_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS,
            )
            for handle_uuid in current_handles
        ):
            if template_uuid in compatible_replacements:
                projected_templates.append(current_template)
                projected_handles.extend(current_handles.values())
                continue
            _fail("template_catalog_mismatch", "已应用连接点模板目录语义已漂移")
        projected_templates.append(deepcopy(dict(applied_template)))
        projected_handles.extend(applied_generation_handles.values())
    projected_handles.sort(key=_entity_uuid_sort_key)
    return projected_templates, projected_handles


def _entity_uuid_sort_key(item: Mapping[str, Any]) -> str:
    """读取已应用创作实体的稳定 UUID 排序键。

    参数：``item`` 是已经通过当前投影校验的实体映射。返回：字符串 UUID。
    异常：无；缺失值只会规范为字符串并由上游身份校验拒绝。
    """

    return str(item["uuid"])


def _index_current_handles(
    handles: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按 UUID 索引当前目录聚合的连接点（Handle）。

    参数说明：``handles`` 是一个当前节点模板的完整连接点集合。返回深拷贝有序
    索引；连接点非对象、UUID 非法或重复时以目录不匹配关闭式失败。
    """

    indexed: dict[str, dict[str, Any]] = {}
    for raw_handle in handles:
        if not isinstance(raw_handle, Mapping):
            _fail("template_catalog_mismatch", "当前连接点模板必须是对象")
        handle = deepcopy(dict(raw_handle))
        try:
            handle_uuid = validate_uuid(handle.get("uuid"))
        except (TypeError, ValueError):
            _fail("template_catalog_mismatch", "当前连接点模板身份无效")
        if handle_uuid in indexed:
            _fail("template_catalog_mismatch", "当前连接点模板身份重复")
        indexed[handle_uuid] = handle
    return indexed


def _catalog_entity_equal(
    applied: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    nullable_fields: set[str],
) -> bool:
    """按受控 wire 等价比较一项已应用和当前目录实体。

    参数说明：两项实体分别来自数据库读投影和当前目录；``nullable_fields`` 是
    Backend ``omitempty`` 白名单。返回是否受控等价：只忽略数据库操作字段，
    只把白名单字段的缺失与 ``null`` 视作相同，并接受浏览器对等值 JSON number
    的词法重编码；其他差异均保留。
    """

    return not _catalog_entity_difference_fields(
        applied,
        current,
        nullable_fields=nullable_fields,
    )


def _catalog_entity_difference_fields(
    applied: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    nullable_fields: set[str],
) -> list[str]:
    """列出已应用实体与当前目录之间的严格 wire 漂移字段。

    参数说明：两项实体分别来自数据库读投影和当前目录；``nullable_fields`` 是
    Backend ``omitempty`` 白名单。返回稳定排序的不同字段名；相等整数和浮点数
    按同一 JSON number 比较，且不把具体合同值写入用户诊断。
    """

    applied_semantic = _catalog_semantic_entity(
        applied,
        nullable_fields=nullable_fields,
    )
    current_semantic = _catalog_semantic_entity(
        current,
        nullable_fields=nullable_fields,
    )
    return sorted(
        field_name
        for field_name in set(applied_semantic) | set(current_semantic)
        if field_name not in applied_semantic
        or field_name not in current_semantic
        or not _catalog_json_equal(
            applied_semantic[field_name],
            current_semantic[field_name],
        )
    )


def _catalog_json_equal(left: Any, right: Any) -> bool:
    """比较目录 JSON，并把相等整数与浮点数视为同一 JSON number。

    参数说明：``left`` 与 ``right`` 是已验证的有限 JSON 值。返回：浏览器重编码
    ``300.0`` 为 ``300`` 时仍为真；布尔值、容器类型、字段集合、数组顺序和
    其他标量继续严格比较。该规则只用于目录读投影固定点，不改变公共 JSON
    编解码器或工作流业务值的类型语义。
    """

    pending = [(left, right)]
    while pending:
        left_item, right_item = pending.pop()
        left_type = type(left_item)
        right_type = type(right_item)
        if left_type in {int, float} and right_type in {int, float}:
            if left_item != right_item:
                return False
            continue
        if left_type is not right_type:
            return False
        if isinstance(left_item, dict):
            if left_item.keys() != right_item.keys():
                return False
            pending.extend(
                (value, right_item[key]) for key, value in left_item.items()
            )
        elif isinstance(left_item, list):
            if len(left_item) != len(right_item):
                return False
            pending.extend(zip(left_item, right_item, strict=True))
        elif left_item != right_item:
            return False
    return True


def _catalog_semantic_entity(
    value: Mapping[str, Any],
    *,
    nullable_fields: set[str],
) -> dict[str, Any]:
    """移除目录比较唯一允许忽略的数据库读形状差异。

    参数说明：``value`` 是目录实体，``nullable_fields`` 是显式可空字段。返回
    深拷贝语义实体；未知空字段不会被删除，避免未来字段静默混代。
    """

    semantic = deepcopy(dict(value))
    for field_name in _DATABASE_OPERATION_FIELDS:
        semantic.pop(field_name, None)
    for field_name in nullable_fields:
        if semantic.get(field_name) is None:
            semantic.pop(field_name, None)
    return semantic


def _fail(code: str, message: str) -> None:
    """以稳定诊断中止已应用投影合并。

    参数说明：``code`` 是产品错误码，``message`` 是中文内部原因。返回：永不
    正常返回，始终抛出 ``AppliedAuthoringProjectionError``。
    """

    raise AppliedAuthoringProjectionError(code, message)


__all__ = [
    "AppliedAuthoringProjection",
    "AppliedAuthoringProjectionError",
    "reconcile_applied_authoring_projection",
]
