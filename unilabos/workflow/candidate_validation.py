"""可信工作流创作候选结果（Authoring Candidate）的公共校验边界。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Never

from pydantic import ValidationError

from unilabos.workflow.authoring_graph import (
    AuthoringGraphError,
    candidate_changeset,
    semantic_graph_equal,
)
from unilabos.workflow.composite_compatibility import (
    classify_pinned_published_workflow_invocation,
)
from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)

_GRAPH_FIELDS = {
    "workflow",
    "nodes",
    "edges",
    "node_templates",
    "handle_templates",
}


class CandidateBundleError(ValueError):
    """候选图、源码映射或变更集不能共同证明时的稳定错误。"""


def validate_candidate_bundle(
    *,
    graph: Any,
    base_graph: Any,
    workflow_uuid: str,
    revision: int,
    source_map: list[dict[str, Any]],
    changeset: dict[str, Any],
    require_unchanged_graph: bool = False,
) -> dict[str, Any]:
    """验证完整候选 bundle 的身份、图、目录和变更语义。

    参数说明：``graph`` 是编译候选，``base_graph`` 是当前已应用权威图；工作流
    UUID/修订来自服务层；``source_map`` 和 ``changeset`` 必须精确描述候选；
    ``require_unchanged_graph`` 用于只验证源码的场景。返回已校验候选图的普通
    字典，任何伪造、漂移或不完整关系抛出 ``CandidateBundleError``。
    异常：身份、目录、源码映射、变更集或图语义不一致时抛出
    ``CandidateBundleError``。
    """

    try:
        identity = validate_uuid(workflow_uuid)
        if type(revision) is not int or revision < 1:
            _fail("工作流修订无效")
        candidate = _closed_graph(graph)
        base = _closed_graph(base_graph)
        workflow = _workflow(candidate["workflow"], identity=identity, revision=revision)
        base_workflow = _workflow(base["workflow"], identity=identity, revision=revision)
        _workflow_authoring_boundary(workflow, base_workflow)

        nodes, node_entities = _nodes(candidate["nodes"], workflow_uuid=identity)
        edges = _edges(candidate["edges"])
        templates = _node_templates(candidate["node_templates"])
        handles = _handle_templates(candidate["handle_templates"])
        _base_nodes, base_node_entities = _nodes(
            base["nodes"],
            workflow_uuid=identity,
        )
        _edges(base["edges"])
        base_templates = _node_templates(base["node_templates"])
        base_handles = _handle_templates(base["handle_templates"])
        _catalog_projection(
            nodes=nodes,
            templates=templates,
            handles=handles,
            base_templates=base_templates,
            base_handles=base_handles,
            node_entities=node_entities,
            base_node_entities=base_node_entities,
        )
        validate_graph(
            nodes=nodes,
            edges=edges,
            templates=templates,
            handles=handles,
            effective_params={node.uuid: node.param or {} for node in nodes},
            workflow_meta_data=workflow["meta_data"],
            node_meta_data={node.uuid: node.meta_data for node in nodes},
            validate_workflow_io_contract=True,
        )
        normalized_map = [
            CandidateSourceMapEntry.model_validate(item).model_dump()
            for item in source_map
        ]
        if any(item["workflow_node_uuid"] not in node_entities for item in normalized_map):
            _fail("源码映射引用了候选图之外的节点")
        normalized_changeset = CandidateChangeset.model_validate(changeset).model_dump()
        _changeset_semantics(
            graph=candidate,
            base_graph=base,
            changeset=normalized_changeset,
        )
        if require_unchanged_graph and not semantic_graph_equal(candidate, base):
            _fail("只验证源码的候选结果改变了工作流图")
        return candidate
    except CandidateBundleError:
        raise
    except (
        AuthoringGraphError,
        GraphValidationError,
        KeyError,
        TypeError,
        ValidationError,
        ValueError,
    ) as error:
        raise CandidateBundleError(
            "候选结果不满足可信工作流合同："
            f"{type(error).__name__}: {error}"
        ) from error


def _closed_graph(value: Any) -> dict[str, Any]:
    """验证候选图只含且完整包含后端五集合。

    参数说明：``value`` 是待校验对象；返回浅复制字典，顶层字段或集合类型错误
    抛出 ``CandidateBundleError``。
    """

    if not isinstance(value, Mapping) or set(value) != _GRAPH_FIELDS:
        _fail("候选图必须且只能包含完整五集合")
    graph = dict(value)
    if not isinstance(graph["workflow"], Mapping) or any(
        not isinstance(graph[field], list) for field in _GRAPH_FIELDS - {"workflow"}
    ):
        _fail("候选图集合类型无效")
    return graph


def _workflow(
    value: Any,
    *,
    identity: str,
    revision: int,
) -> dict[str, Any]:
    """校验工作流投影的稳定身份和 JSON 类型。

    参数说明：``value`` 是工作流实体，``identity``/``revision`` 是服务权威；
    返回普通字典，不一致时抛出 ``CandidateBundleError``。
    """

    if not isinstance(value, Mapping):
        _fail("候选工作流必须是对象")
    workflow = dict(value)
    required = {"uuid", "name", "tags", "revision", "meta_data"}
    if not required <= set(workflow):
        _fail("候选工作流缺少必填字段")
    if validate_uuid(workflow["uuid"]) != identity or workflow["revision"] != revision:
        _fail("候选工作流身份或修订不匹配")
    if not isinstance(workflow["name"], str) or not workflow["name"].strip():
        _fail("候选工作流名称无效")
    if "description" in workflow and workflow["description"] is not None and not isinstance(
        workflow["description"], str
    ):
        _fail("候选工作流描述无效")
    normalize_json_array(workflow["tags"])
    normalize_json_object(workflow["meta_data"])
    return workflow


def _workflow_authoring_boundary(
    candidate: Mapping[str, Any],
    base: Mapping[str, Any],
) -> None:
    """限制作者源码可以改变的工作流字段。

    参数说明：候选可改变名称、描述和保留 ``meta_data.unilab``；UUID、修订、
    标签、投影时间和非保留元数据必须保持权威值，否则失败关闭。
    """

    for field in ("uuid", "revision", "tags", "create_time", "update_time"):
        if field in candidate or field in base:
            if not strict_json_equal(candidate.get(field), base.get(field)):
                _fail("候选结果改变了非创作工作流字段")
    candidate_meta = dict(candidate["meta_data"])
    base_meta = dict(base["meta_data"])
    candidate_meta.pop("unilab", None)
    base_meta.pop("unilab", None)
    if not strict_json_equal(candidate_meta, base_meta):
        _fail("候选结果改变了非创作工作流元数据")


def _nodes(
    values: list[Any],
    *,
    workflow_uuid: str,
) -> tuple[list[WorkflowNodeWrite], dict[str, Mapping[str, Any]]]:
    """校验节点数组并建立 UUID 索引。

    参数说明：``values`` 是候选节点，``workflow_uuid`` 是所属工作流；返回模型
    列表和原实体索引，重复 UUID 或外部归属失败关闭。
    """

    models: list[WorkflowNodeWrite] = []
    entities: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            _fail("候选节点必须是对象")
        if "workflow_uuid" in value and validate_uuid(value["workflow_uuid"]) != workflow_uuid:
            _fail("候选节点属于另一个工作流")
        model = WorkflowNodeWrite.model_validate(
            {key: value[key] for key in WorkflowNodeWrite.model_fields if key in value}
        )
        if model.uuid in entities:
            _fail("候选节点 UUID 重复")
        models.append(model)
        entities[model.uuid] = value
    return models, entities


def _edges(values: list[Any]) -> list[WorkflowEdgeWrite]:
    """校验边数组及唯一身份。

    参数说明：``values`` 是候选边对象列表；返回 Pydantic 模型列表，重复 UUID
    或字段非法时失败关闭。
    """

    models: list[WorkflowEdgeWrite] = []
    identities: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            _fail("候选边必须是对象")
        model = WorkflowEdgeWrite.model_validate(
            {key: value[key] for key in WorkflowEdgeWrite.model_fields if key in value}
        )
        if model.uuid in identities:
            _fail("候选边 UUID 重复")
        identities.add(model.uuid)
        models.append(model)
    return models


def _node_templates(values: list[Any]) -> dict[str, dict[str, Any]]:
    """校验节点模板目录投影。

    参数说明：``values`` 是节点模板数组；返回 UUID 索引，确保模板身份、资源
    模板身份、业务名称和 JSON 字段合法。
    """

    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            _fail("候选节点模板必须是对象")
        template = dict(value)
        identity = validate_uuid(template.get("uuid"))
        validate_uuid(template.get("resource_template_uuid"))
        for field in ("name", "display_name", "type", "node_type"):
            if not isinstance(template.get(field), str) or not template[field].strip():
                _fail("候选节点模板文本字段无效")
        for field in ("meta_data", "goal", "goal_default", "feedback", "result"):
            normalize_json_object(template.get(field))
        if identity in result:
            _fail("候选节点模板 UUID 重复")
        result[identity] = template
    return result


def _handle_templates(values: list[Any]) -> dict[str, dict[str, Any]]:
    """校验连接点（Handle）模板目录投影。

    参数说明：``values`` 是连接点数组；返回 UUID 索引，确保父模板、方向、业务
    键、必填标志和元数据类型合法。
    """

    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            _fail("候选连接点模板必须是对象")
        handle = dict(value)
        identity = validate_uuid(handle.get("uuid"))
        validate_uuid(handle.get("workflow_node_template_uuid"))
        for field in ("handle_key", "io_type", "display_name", "type"):
            if not isinstance(handle.get(field), str) or not handle[field].strip():
                _fail("候选连接点模板文本字段无效")
        if type(handle.get("required")) is not bool:
            _fail("候选连接点必填标志无效")
        normalize_json_object(handle.get("meta_data"))
        if identity in result:
            _fail("候选连接点模板 UUID 重复")
        result[identity] = handle
    return result


def _catalog_projection(
    *,
    nodes: list[WorkflowNodeWrite],
    templates: dict[str, dict[str, Any]],
    handles: dict[str, dict[str, Any]],
    base_templates: dict[str, dict[str, Any]],
    base_handles: dict[str, dict[str, Any]],
    node_entities: Mapping[str, Mapping[str, Any]],
    base_node_entities: Mapping[str, Mapping[str, Any]],
) -> None:
    """验证最小目录投影并保护已保留目录事实。

    参数说明：候选目录只能含被节点引用的模板；新模板可由当前目录加入；两个
    节点索引用于认证已发布工作流调用 pin。基线模板及连接点通常必须严格相同，
    只有所有保留调用都证明为精确或可加演进时才允许整代替换。
    返回：无；仅验证传入目录投影。异常：目录非最小、跨代混用或 pin 认证失败
    时抛出 ``CandidateBundleError``。
    """

    referenced = {
        node.workflow_node_template_uuid
        for node in nodes
        if node.workflow_node_template_uuid is not None
    }
    if set(templates) != referenced:
        _fail("候选目录投影不是被引用模板的最小集合")
    if any(handle["workflow_node_template_uuid"] not in templates for handle in handles.values()):
        _fail("候选连接点的父模板不在最小目录投影中")
    retained = set(templates) & set(base_templates)
    for template_uuid in retained:
        candidate_generation_handles = {
            identity: value
            for identity, value in handles.items()
            if value["workflow_node_template_uuid"] == template_uuid
        }
        base_generation_handles = {
            identity: value
            for identity, value in base_handles.items()
            if value["workflow_node_template_uuid"] == template_uuid
        }
        if strict_json_equal(
            templates[template_uuid],
            base_templates[template_uuid],
        ) and strict_json_equal(
            candidate_generation_handles,
            base_generation_handles,
        ):
            continue
        if not _published_replacement_is_compatible(
            template_uuid=template_uuid,
            node_entities=node_entities,
            base_node_entities=base_node_entities,
            templates=templates,
            handles=handles,
            base_templates=base_templates,
            base_handles=base_handles,
        ):
            _fail("候选结果改变了未经认证的已保留目录投影")


def _published_replacement_is_compatible(
    *,
    template_uuid: str,
    node_entities: Mapping[str, Mapping[str, Any]],
    base_node_entities: Mapping[str, Mapping[str, Any]],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    base_templates: Mapping[str, Mapping[str, Any]],
    base_handles: Mapping[str, Mapping[str, Any]],
) -> bool:
    """复核一个目录整代替换由全部保留组合调用共同授权。

    参数：模板 UUID、候选/基线节点和目录索引。返回：至少一个同 UUID 保留调用
    存在，且每个保留调用的旧聚合都真实、当前演进均非破坏性时为 ``True``。
    异常：无；任何结构问题由兼容性深模块收敛为 ``False``。
    """

    retained_nodes = [
        (base_node, node_entities[node_uuid])
        for node_uuid, base_node in base_node_entities.items()
        if base_node.get("workflow_node_template_uuid") == template_uuid
        and node_uuid in node_entities
        and node_entities[node_uuid].get("workflow_node_template_uuid")
        == template_uuid
    ]
    if not retained_nodes:
        return False
    previous_template = base_templates.get(template_uuid)
    if not isinstance(previous_template, Mapping):
        return False
    previous_handles = [
        value
        for value in base_handles.values()
        if value.get("workflow_node_template_uuid") == template_uuid
    ]
    return all(
        classify_pinned_published_workflow_invocation(
            previous_node=previous,
            current_node=current,
            previous_templates=[previous_template],
            previous_handles=previous_handles,
            current_templates=[templates[template_uuid]],
            current_handles=[
                value
                for value in handles.values()
                if value.get("workflow_node_template_uuid") == template_uuid
            ],
        )
        != "breaking"
        for previous, current in retained_nodes
    )


def _changeset_semantics(
    *,
    graph: Mapping[str, Any],
    base_graph: Mapping[str, Any],
    changeset: dict[str, Any],
) -> None:
    """证明变更集精确描述候选图相对基线的变化。

    参数说明：两个图决定期望集合，``changeset`` 是编译器声明；集合内容、排序
    无关但生命周期集合不得重复或交叠，种类和保留元数据标志必须精确。
    """

    fields = (
        "created_node_uuids",
        "updated_node_uuids",
        "deleted_node_uuids",
        "created_edge_uuids",
        "updated_edge_uuids",
        "deleted_edge_uuids",
    )
    values = [changeset[field] for field in fields]
    if any(len(value) != len(set(value)) for value in values):
        _fail("变更集包含重复 UUID")
    node_sets = [set(changeset[field]) for field in fields[:3]]
    edge_sets = [set(changeset[field]) for field in fields[3:]]
    for family in (node_sets, edge_sets):
        if any(
            family[left] & family[right]
            for left in range(len(family))
            for right in range(left + 1, len(family))
        ):
            _fail("变更集生命周期集合互相重叠")
    expected = candidate_changeset(graph=graph, applied_graph=base_graph)
    if any(set(changeset[field]) != set(expected[field]) for field in fields):
        _fail("变更集没有精确描述候选图")
    if changeset["kind"] != expected["kind"] or (
        changeset["reserved_metadata_changed"]
        is not expected["reserved_metadata_changed"]
    ):
        _fail("变更集种类或保留元数据标志不准确")


def _fail(message: str) -> Never:
    """抛出候选 bundle 校验错误。

    参数说明：``message`` 是内部中文原因；函数永不返回。
    """

    raise CandidateBundleError(message)


__all__ = ["CandidateBundleError", "validate_candidate_bundle"]
