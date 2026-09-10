"""后端形状工作流候选图到规范 Python 源码的确定性生成层。"""

from __future__ import annotations

import json
import keyword
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.authoring_graph import AuthoringGraphError
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.authoring_material import (
    MaterialAuthoringError,
    RenderedMaterialSource,
    render_material_source_call,
)
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.models import CandidateSourceMapEntry, validate_uuid
from unilabos.workflow.source_coordinates import utf16_length


@dataclass(frozen=True, slots=True)
class RenderedAuthoringSource:
    """一次确定性源码生成的文本和源码映射。"""

    python_source: str
    source_map: list[dict[str, Any]]


def render_authoring_python(
    *,
    graph: Mapping[str, Any],
    catalog: AuthoringCatalogSnapshot,
    function_docstring: str | None = None,
) -> RenderedAuthoringSource:
    """把完整候选图渲染为规范作者 Python。

    参数说明：``graph`` 是后端五集合候选图，``catalog`` 是同一编译事务目录
    快照；``function_docstring`` 是可信 AST 提取并清理的可选工作流函数文档。
    返回可回编译的规范源码和 UTF-16 源码映射。身份或目录投影不一致时
    抛出 ``AuthoringGraphError``；函数文档非字符串时也失败关闭；物料图违反物料流线性
    （MaterialFlowLinearity）或资源模板兼容（ResourceTemplate Compatibility）
    时，也会把内部物料图异常转换为 ``AuthoringGraphError`` 并保留稳定错误码。
    """

    try:
        validate_material_graph_projection(graph)
    except MaterialGraphValidationError as error:
        raise AuthoringGraphError(error.code, error.message) from error
    workflow = graph.get("workflow")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if (
        not isinstance(workflow, Mapping)
        or not isinstance(nodes, list)
        or not isinstance(edges, list)
    ):
        raise AuthoringGraphError("candidate_invalid", "候选图缺少工作流、节点或边")
    workflow_uuid = validate_uuid(workflow.get("uuid"))
    all_nodes = _node_index(nodes)
    all_catalog = _catalog_projection(all_nodes, catalog)
    hidden_composite_nodes = _composite_internal_node_uuids(
        all_nodes,
        all_catalog,
    )
    node_by_uuid = {
        key: value for key, value in all_nodes.items() if key not in hidden_composite_nodes
    }
    catalog_by_node = {
        key: all_catalog[key] for key in node_by_uuid
    }
    visible_edges = [
        edge
        for edge in edges
        if isinstance(edge, Mapping)
        and edge.get("source_node_uuid") in node_by_uuid
        and edge.get("target_node_uuid") in node_by_uuid
    ]
    ordered_nodes = _authoring_ordered_nodes(node_by_uuid, visible_edges)
    device_symbols, device_imports = _device_symbols(ordered_nodes, catalog_by_node)
    published_workflow_imports = {
        tuple(str(action.template["class"]).rsplit(":", 1))
        for node in ordered_nodes
        for action in [catalog_by_node[str(node["uuid"])]]
        if _is_published_workflow(action)
    }
    # ``material_sources`` 冻结每个物料来源节点的 import 与调用表达式。
    material_sources: dict[str, RenderedMaterialSource] = {}
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if not _is_material_source(catalog_by_node[node_uuid]):
            continue
        try:
            material_sources[node_uuid] = render_material_source_call(
                node,
                catalog=catalog,
            )
        except MaterialAuthoringError as error:
            raise AuthoringGraphError(error.code, error.message) from error
    # ``material_imports`` 与设备 import 合并后按限定身份稳定排序。
    material_imports = {
        rendered.resource_import for rendered in material_sources.values()
    }
    input_contract, output_contract, output_bindings = _authoring_metadata(workflow)
    # 隐式物料输出是服务端工作流输入/输出（Workflow I/O）权威事实，不属于作者
    # 结果记录。只渲染显式输出，重新编译时再由服务端确定性合成隐式透传。
    explicit_outputs = [
        item
        for item in output_contract.get("outputs", [])
        if isinstance(item, Mapping) and not bool(item.get("implicit", False))
    ]
    explicit_output_names = [str(item["name"]) for item in explicit_outputs]
    explicit_output_bindings = {
        name: output_bindings[name] for name in explicit_output_names
    }

    annotations = [
        _render_parameter(item, catalog=catalog)
        for item in input_contract.get("parameters", [])
    ]
    output_schemas = {
        item["name"]: item["schema"]
        for item in explicit_outputs
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("schema"), Mapping)
    }
    output_annotations = {
        name: _render_schema(
            dict(output_schemas[name]),
            catalog=catalog,
            include_resource_templates=False,
        )
        for name in explicit_output_bindings
    }
    typing_names: set[str] = (
        {"TypedDict"} if explicit_output_bindings else set()
    )
    needs_field = False
    needs_resource_slot = False
    annotation_resource_imports: set[tuple[str, str]] = set()
    for _name, annotation, _default, imports, resource_imports in annotations:
        typing_names.update(imports & {"Annotated", "Literal"})
        annotation_resource_imports.update(resource_imports)
        needs_field = needs_field or "Field(" in annotation
        needs_resource_slot = needs_resource_slot or "ResourceSlot" in annotation
    for annotation, imports, resource_imports in output_annotations.values():
        typing_names.update(imports & {"Annotated", "Literal"})
        annotation_resource_imports.update(resource_imports)
        needs_field = needs_field or "Field(" in annotation
        needs_resource_slot = needs_resource_slot or "ResourceSlot" in annotation

    lines: list[str] = []
    if typing_names:
        lines.append(f"from typing import {', '.join(sorted(typing_names))}")
        lines.append("")
    if needs_field:
        lines.append("from pydantic import Field")
    for module, symbol in sorted(
        device_imports
        | material_imports
        | published_workflow_imports
        | annotation_resource_imports
    ):
        lines.append(f"from {module} import {symbol}")
    if annotation_resource_imports:
        lines.append(
            "from unilabos.registry.annotations import AllowedResourceTemplates"
        )
    if needs_resource_slot:
        lines.append("from unilabos.registry.placeholder_type import ResourceSlot")
    group_nodes = [
        node
        for node in ordered_nodes
        if _is_group(catalog_by_node[str(node["uuid"])])
    ]
    marker_imports = "device, workflow"
    if group_nodes:
        marker_imports += ", group"
        if any(_parallel_scope(node) is not None for node in group_nodes):
            marker_imports += ", parallel"
    if material_sources:
        marker_imports += ", MaterialFlowRole, material_source, resource_ref"
        if any(
            "custody_policy=" in rendered.call
            for rendered in material_sources.values()
        ):
            marker_imports = marker_imports.replace(
                ", MaterialFlowRole,",
                ", MaterialCustodyPolicy, MaterialFlowRole,",
            )
    elif any(
        isinstance((node.get("meta_data") or {}).get("unilab"), Mapping)
        and bool((node.get("meta_data") or {})["unilab"].get("resource_refs"))
        for node in ordered_nodes
    ):
        # 普通动作也可用 ``resource_ref``；只有真实节点元数据声明时才生成 import。
        marker_imports += ", resource_ref"
    if not explicit_output_bindings:
        marker_imports += ", workflow_output"
    lines.append(f"from unilabos.workflow.authoring import {marker_imports}")
    lines.extend(["", ""])
    result_record_name = _safe_identifier(
        str(
            (workflow.get("meta_data") or {})
            .get("unilab", {})
            .get("authoring_result_record_name")
            or f"{workflow.get('name') or 'Workflow'}Result"
        ),
        fallback="WorkflowResult",
    )
    if explicit_output_bindings:
        lines.append(f"class {result_record_name}(TypedDict):")
        for output_name in explicit_output_bindings:
            annotation, _imports, _resource_imports = output_annotations[output_name]
            lines.append(f"    {output_name}: {annotation}")
        lines.extend(["", ""])
    for selector_key, symbol in device_symbols.items():
        class_identity, device_id = selector_key
        class_name = class_identity.rsplit(":", 1)[1]
        argument = "" if device_id is None else repr(device_id)
        lines.append(f"{symbol}: {class_name} = device({argument})")
    lines.extend(["", ""])
    lines.extend(
        [
            "@workflow(",
            f'    workflow_uuid="{workflow_uuid}",',
            f"    displayname={workflow.get('name')!r},",
        ]
    )
    if workflow.get("description") is not None:
        lines.append(f"    description={workflow.get('description')!r},")
    lines.append(")")
    function_name = _safe_identifier(
        str(
            (workflow.get("meta_data") or {})
            .get("unilab", {})
            .get("authoring_function_name")
            or workflow.get("name")
            or "workflow"
        ),
        fallback="workflow",
    )
    if annotations:
        lines.append(f"def {function_name}(")
        lines.append("    *,")
        for name, annotation, default, _imports, _resource_imports in annotations:
            suffix = "" if default is _NO_DEFAULT else f" = {default!r}"
            lines.append(f"    {name}: {annotation}{suffix},")
        return_annotation = (
            f" -> {result_record_name}" if explicit_output_bindings else ""
        )
        lines.append(f"){return_annotation}:")
    else:
        return_annotation = (
            f" -> {result_record_name}" if explicit_output_bindings else ""
        )
        lines.append(f"def {function_name}(){return_annotation}:")
    if function_docstring is not None:
        _append_function_docstring(lines=lines, docstring=function_docstring)

    incoming = _incoming_bindings(
        visible_edges,
        catalog_by_node=catalog_by_node,
    )
    source_map: list[dict[str, Any]] = []
    # Python 动作结果变量承载节点间数据依赖，必须唯一且不能被节点展示标题改写。
    result_names: set[str] = set()
    if (
        not ordered_nodes
        and not explicit_output_bindings
        and function_docstring is None
    ):
        lines.append('    """空工作流。"""')
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in ordered_nodes:
        parent_uuid = node.get("parent_uuid")
        if isinstance(parent_uuid, str):
            children_by_parent[parent_uuid].append(node)
    rendered_node_uuids: set[str] = set()
    rendered_parallel_scopes: set[str] = set()
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if node_uuid in rendered_node_uuids or isinstance(node.get("parent_uuid"), str):
            continue
        action = catalog_by_node[node_uuid]
        if not _is_group(action):
            _append_action_source(
                node=node,
                indent_level=1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
                device_symbols=device_symbols,
            )
            rendered_node_uuids.add(node_uuid)
            continue
        scope = _parallel_scope(node)
        if scope is not None:
            if scope in rendered_parallel_scopes:
                continue
            rendered_parallel_scopes.add(scope)
            lines.append("    with parallel():")
            scope_groups = sorted(
                (
                    candidate
                    for candidate in group_nodes
                    if _parallel_scope(candidate) == scope
                ),
                key=_parallel_order,
            )
            for group_node in scope_groups:
                _append_group_source(
                    node=group_node,
                    indent_level=2,
                    lines=lines,
                    source_map=source_map,
                    action=catalog_by_node[str(group_node["uuid"])],
                )
                rendered_node_uuids.add(str(group_node["uuid"]))
                for child in children_by_parent[str(group_node["uuid"])]:
                    _append_action_source(
                        node=child,
                        indent_level=3,
                        lines=lines,
                        source_map=source_map,
                        result_names=result_names,
                        material_sources=material_sources,
                        incoming=incoming,
                        node_by_uuid=node_by_uuid,
                        catalog_by_node=catalog_by_node,
                        device_symbols=device_symbols,
                    )
                    rendered_node_uuids.add(str(child["uuid"]))
            continue
        _append_group_source(
            node=node,
            indent_level=1,
            lines=lines,
            source_map=source_map,
            action=action,
        )
        rendered_node_uuids.add(node_uuid)
        for child in children_by_parent[node_uuid]:
            _append_action_source(
                node=child,
                indent_level=2,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
                device_symbols=device_symbols,
            )
            rendered_node_uuids.add(str(child["uuid"]))
    if explicit_output_bindings:
        # 输出绑定字典由编译器按作者声明顺序建立；保留该顺序才能让输出合同
        # 在 Python→图→Python 往返中达到固定点。
        rendered_outputs = [
            f"{name}={_render_output_binding(binding, node_by_uuid, catalog_by_node)}"
            for name, binding in explicit_output_bindings.items()
        ]
        rendered_values = ", ".join(
            f"{name!r}: {expression.split('=', 1)[1]}"
            for name, expression in zip(
                explicit_output_bindings,
                rendered_outputs,
                strict=True,
            )
        )
        lines.append(f"    return {{{rendered_values}}}")
    else:
        lines.append("    return workflow_output()")
    return RenderedAuthoringSource(
        python_source="\n".join(lines).rstrip() + "\n",
        source_map=source_map,
    )


def _append_function_docstring(*, lines: list[str], docstring: str) -> None:
    """向规范工作流函数体追加确定性的中文函数文档字面量。

    参数：``lines`` 是统一源码行账本；``docstring`` 是可信 AST 已按 Python 文档
    规则清理的语义文本。返回：无，原位追加可再次静态解析的三引号文档行。
    异常：``docstring`` 不是字符串时抛出 ``AuthoringGraphError``，不生成部分文档。
    """

    if not isinstance(docstring, str):
        raise AuthoringGraphError("candidate_invalid", "工作流函数文档必须是字符串")
    # ``docstring_lines`` 按语义换行保留中文函数合同的段落结构。
    docstring_lines = docstring.split("\n")
    # ``escaped_lines`` 使用 JSON 字符串的兼容转义规则保护引号、反斜线与控制字符；
    # 截去外层双引号后仍是合法 Python 三引号字面量内容。
    escaped_lines = [
        json.dumps(line, ensure_ascii=False)[1:-1] for line in docstring_lines
    ]
    if len(escaped_lines) == 1:
        lines.append(f'    """{escaped_lines[0]}"""')
        return
    lines.append(f'    """{escaped_lines[0]}')
    for escaped_line in escaped_lines[1:]:
        # ``escaped_line`` 是一行语义文档内容；空内容必须输出真正的空行，
        # 避免生成仅含函数缩进的尾随空白并触发 Ruff W293。
        lines.append(f"    {escaped_line}" if escaped_line else "")
    # 多行文档的闭合分隔符独占一行，并以空行隔开后续节点展示注释，使作者
    # 源码与规范源码保持同一个人类/AI 可读的稳定版式。
    lines.append('    """')
    lines.append("")


def _append_group_source(
    *,
    node: Mapping[str, Any],
    indent_level: int,
    lines: list[str],
    source_map: list[dict[str, Any]],
    action: AuthoringCatalogAction,
) -> None:
    """追加展示分组节点的注释、锚点和 ``with group`` 头。

    参数说明：``node`` 与 ``action`` 是分组节点及其目录模板；``indent_level``
    是四空格缩进层级；``lines``/``source_map`` 是本次生成结果收集器。返回：无，
    原位追加确定性源码。异常：分组参数或展示元数据无法无损生成时失败关闭。
    """

    node_uuid = str(node["uuid"])
    indent = "    " * indent_level
    start_line = len(lines) + 1
    metadata_comment = _node_metadata_comment(node=node, action=action)
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}# unilab:node_uuid={node_uuid}")
    params = node.get("param")
    name = params.get("name") if isinstance(params, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        raise AuthoringGraphError("candidate_invalid", "展示分组缺少静态 name")
    lines.append(f"{indent}with group(name={name!r}):")
    source_map.append(
        CandidateSourceMapEntry(
            workflow_node_uuid=node_uuid,
            start_line=start_line,
            start_column=len(indent) + 1,
            end_line=len(lines),
            end_column=utf16_length(lines[-1]) + 1,
        ).model_dump()
    )


def _append_action_source(
    *,
    node: Mapping[str, Any],
    indent_level: int,
    lines: list[str],
    source_map: list[dict[str, Any]],
    result_names: set[str],
    material_sources: Mapping[str, RenderedMaterialSource],
    incoming: Mapping[tuple[str, str], tuple[str, str]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
    device_symbols: Mapping[tuple[str, str | None], str],
) -> None:
    """按给定展示缩进追加一个物料来源或普通动作节点源码。

    参数说明：``node`` 是待生成节点；``indent_level`` 控制顶层/分组/并行分支
    缩进；``lines``、``source_map``、``result_names`` 是原位收集器；其余映射分别
    提供物料来源调用、候选边、节点/目录索引和设备选择器名。返回：无。异常：
    重复结果名、连接点身份或参数来源不可信时抛出 ``AuthoringGraphError``。
    """

    node_uuid = str(node["uuid"])
    action = catalog_by_node[node_uuid]
    if _is_group(action):
        raise AuthoringGraphError("candidate_invalid", "展示分组不能作为动作生成")
    indent = "    " * indent_level
    start_line = len(lines) + 1
    result_name = _node_result_name(node)
    if result_name in result_names:
        raise AuthoringGraphError("candidate_invalid", "节点作者结果变量重复")
    result_names.add(result_name)
    metadata_comment = _node_metadata_comment(node=node, action=action)
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}# unilab:node_uuid={node_uuid}")
    if node_uuid in material_sources:
        call = material_sources[node_uuid].call
    elif _is_published_workflow(action):
        arguments = _render_action_arguments(
            node=node,
            action=action,
            incoming=incoming,
            node_by_uuid=node_by_uuid,
            catalog_by_node=catalog_by_node,
        )
        class_identity = action.template.get("class")
        if not isinstance(class_identity, str) or class_identity.count(":") != 1:
            raise AuthoringGraphError(
                "composite_catalog_mismatch",
                "已发布工作流模板缺少绝对导入身份",
            )
        call = f"{class_identity.rsplit(':', 1)[1]}({', '.join(arguments)})"
    else:
        arguments = _render_action_arguments(
            node=node,
            action=action,
            incoming=incoming,
            node_by_uuid=node_by_uuid,
            catalog_by_node=catalog_by_node,
        )
        selector_key = _selector_key(node, action)
        call = (
            f"{device_symbols[selector_key]}."
            f"{node.get('action_name') or action.template['name']}"
            f"({', '.join(arguments)})"
        )
    lines.append(f"{indent}{result_name} = {call}")
    source_map.append(
        CandidateSourceMapEntry(
            workflow_node_uuid=node_uuid,
            start_line=start_line,
            start_column=len(indent) + 1,
            end_line=len(lines),
            end_column=utf16_length(lines[-1]) + 1,
        ).model_dump()
    )


class _NoDefault:
    """区分无默认值与显式 ``None`` 的内部哨兵类型。"""


_NO_DEFAULT = _NoDefault()


def _node_index(nodes: list[Any]) -> dict[str, dict[str, Any]]:
    """建立无重复节点 UUID 索引。

    参数说明：``nodes`` 是候选节点数组；返回 UUID 到普通节点字典的映射，非法
    节点或重复身份抛出 ``AuthoringGraphError``。
    """

    result: dict[str, dict[str, Any]] = {}
    for value in nodes:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选节点必须是对象")
        node = dict(value)
        identity = validate_uuid(node.get("uuid"))
        if identity in result:
            raise AuthoringGraphError("candidate_invalid", "候选节点 UUID 重复")
        result[identity] = node
    return result


def _catalog_projection(
    nodes: Mapping[str, dict[str, Any]],
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, AuthoringCatalogAction]:
    """为每个候选节点解析权威目录动作。

    参数说明：节点必须引用模板 UUID，``catalog`` 是当前快照；返回节点 UUID 到
    目录动作的映射，未知模板失败关闭。
    """

    result: dict[str, AuthoringCatalogAction] = {}
    for node_uuid, node in nodes.items():
        try:
            result[node_uuid] = catalog.require_template(
                str(node["workflow_node_template_uuid"])
            )
        except (KeyError, AuthoringCatalogError) as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "候选节点引用了当前目录之外的模板",
            ) from error
    return result


def _topological_nodes(
    nodes: Mapping[str, dict[str, Any]],
    edges: list[Any],
) -> list[dict[str, Any]]:
    """按依赖和 UUID 确定性排序候选节点。

    参数说明：``nodes`` 是节点索引，``edges`` 是完整边数组；返回拓扑序节点，
    引用未知节点或形成环时抛出 ``AuthoringGraphError``。
    """

    indegree = {identity: 0 for identity in nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for value in edges:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        source = str(value.get("source_node_uuid"))
        target = str(value.get("target_node_uuid"))
        if source not in nodes or target not in nodes:
            raise AuthoringGraphError("candidate_invalid", "候选边引用未知节点")
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = sorted(identity for identity, count in indegree.items() if count == 0)
    ordered: list[dict[str, Any]] = []
    while ready:
        identity = ready.pop(0)
        ordered.append(nodes[identity])
        for target in sorted(outgoing[identity]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(ordered) != len(nodes):
        raise AuthoringGraphError("candidate_invalid", "候选图包含依赖环")
    return ordered


def _authoring_ordered_nodes(
    nodes: Mapping[str, dict[str, Any]],
    edges: list[Any],
) -> list[dict[str, Any]]:
    """优先按可信作者源码顺序排列节点并验证执行依赖方向。

    参数说明：``nodes`` 是候选节点索引，``edges`` 是完整执行边数组。返回：当
    每个节点都有唯一非负 ``authoring_source_order`` 时返回源码顺序，否则对不含
    展示分组的旧候选图返回拓扑顺序。异常：展示分组缺少顺序、顺序重复，或源码
    顺序逆转执行依赖时抛出 ``AuthoringGraphError``。
    """

    topological = _topological_nodes(nodes, edges)
    # ``source_positions`` 是节点 UUID 到作者源码零基顺序的可信映射。
    source_positions: dict[str, int] = {}
    missing_source_order = False
    for node_uuid, node in nodes.items():
        unilab = (node.get("meta_data") or {}).get("unilab", {})
        value = unilab.get("authoring_source_order") if isinstance(unilab, Mapping) else None
        if value is None:
            missing_source_order = True
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthoringGraphError("candidate_invalid", "作者源码节点顺序必须是非负整数")
        if value in source_positions.values():
            raise AuthoringGraphError("candidate_invalid", "作者源码节点顺序不能重复")
        source_positions[node_uuid] = value
    has_group = any(
        str(node.get("type")) == "group" or str(node.get("node_type")) == "group"
        for node in nodes.values()
    )
    if missing_source_order:
        if has_group:
            raise AuthoringGraphError("candidate_invalid", "展示分组候选图缺少作者源码顺序")
        return topological

    def source_position_sort_key(node: Mapping[str, Any]) -> int:
        """读取候选节点的作者源码位置。

        参数：``node`` 是已验证候选节点。返回：非负且唯一的源码位置。异常：身份
        缺失时由映射访问抛出，调用方不会以猜测顺序继续。
        """

        return source_positions[str(node["uuid"])]

    ordered = sorted(nodes.values(), key=source_position_sort_key)
    # ``ordered_index`` 用于证明所有真实执行边仍保持正向拓扑关系。
    ordered_index = {str(node["uuid"]): index for index, node in enumerate(ordered)}
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        source_uuid = str(edge.get("source_node_uuid"))
        target_uuid = str(edge.get("target_node_uuid"))
        if ordered_index[source_uuid] >= ordered_index[target_uuid]:
            raise AuthoringGraphError("candidate_invalid", "作者源码节点顺序逆转了执行依赖")
    return ordered


def _parallel_scope(node: Mapping[str, Any]) -> str | None:
    """读取展示分组所属并行结构的稳定作用域身份。

    参数说明：``node`` 是展示分组候选节点。返回：未处于并行结构时返回
    ``None``，否则返回已规范化 UUID。异常：元数据不是对象或作用域不是 UUID 时
    抛出 ``AuthoringGraphError``，防止错误地串行生成并行分支。
    """

    unilab = (node.get("meta_data") or {}).get("unilab", {})
    if not isinstance(unilab, Mapping):
        raise AuthoringGraphError("candidate_invalid", "展示分组创作元数据必须是对象")
    value = unilab.get("parallel_scope")
    if value is None:
        return None
    try:
        return validate_uuid(value)
    except (TypeError, ValueError) as error:
        raise AuthoringGraphError("candidate_invalid", "并行结构作用域身份无效") from error


def _parallel_order(node: Mapping[str, Any]) -> int:
    """读取展示分组在同一并行结构内的稳定分支顺序。

    参数说明：``node`` 是已确认属于并行结构的展示分组。返回：零基非负分支顺序。
    异常：顺序缺失、布尔值或负数时抛出 ``AuthoringGraphError``。
    """

    unilab = (node.get("meta_data") or {}).get("unilab", {})
    value = unilab.get("parallel_order") if isinstance(unilab, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthoringGraphError("candidate_invalid", "并行分支顺序必须是非负整数")
    return value


def _is_group(action: AuthoringCatalogAction) -> bool:
    """判断目录动作是否为框架拥有的展示分组模板。

    参数说明：``action`` 是候选节点引用的目录动作。返回：模板类型、节点类型和
    类身份都符合展示分组合同时返回 ``True``，否则返回 ``False``。异常：无；
    不完整模板留给后续目录校验失败关闭。
    """

    template = action.template
    return (
        template.get("type") == "group"
        and template.get("node_type") == "group"
        and template.get("class") == "unilabos.workflow.authoring:group"
        and template.get("name") == "group"
    )


def _is_published_workflow(action: AuthoringCatalogAction) -> bool:
    """判断目录动作是否为框架发布的工作流调用模板。

    参数：``action`` 是目录聚合。返回：类型和来源元数据同时满足发布合同时为
    ``True``。异常：无；不完整模板返回 ``False``。
    """

    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return (
        template.get("type") == "workflow"
        and template.get("node_type") == "workflow"
        and isinstance(unilab, Mapping)
        and isinstance(unilab.get("workflow_source"), Mapping)
    )


def _composite_internal_node_uuids(
    nodes: Mapping[str, Mapping[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> set[str]:
    """返回所有已发布工作流调用节点的私有后代 UUID。

    参数：完整节点索引和同代目录映射。返回：规范源码不得直接呈现的内部节点
    UUID 集合。异常：无；父引用错误由后续公共图校验关闭失败。
    """

    invocation_uuids = {
        node_uuid
        for node_uuid, action in catalog_by_node.items()
        if _is_published_workflow(action)
    }
    hidden: set[str] = set()
    for node_uuid, node in nodes.items():
        parent = node.get("parent_uuid")
        seen: set[str] = set()
        while isinstance(parent, str) and parent not in seen:
            if parent in invocation_uuids:
                hidden.add(node_uuid)
                break
            seen.add(parent)
            parent_node = nodes.get(parent)
            parent = parent_node.get("parent_uuid") if parent_node is not None else None
    return hidden


def _device_symbols(
    nodes: list[dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> tuple[dict[tuple[str, str | None], str], set[tuple[str, str]]]:
    """为候选节点分配确定性设备选择器局部名。

    参数说明：节点顺序和目录映射共同确定设备类及固定设备身份；返回选择器键到
    局部名映射，以及需要导入的 ``(module, class)`` 集合。
    异常：节点缺目录项或设备类身份不能拆分时抛出 ``KeyError`` 或
    ``ValueError``。
    """

    keys: set[tuple[str, str | None]] = set()
    imports: set[tuple[str, str]] = set()
    for node in nodes:
        action = catalog_by_node[str(node["uuid"])]
        if (
            _is_material_source(action)
            or _is_group(action)
            or _is_published_workflow(action)
        ):
            continue
        class_identity, device_id = _selector_key(node, action)
        module, class_name = class_identity.rsplit(":", 1)
        imports.add((module, class_name))
        keys.add((class_identity, device_id))
    result: dict[tuple[str, str | None], str] = {}
    used: set[str] = set()
    for index, key in enumerate(
        sorted(keys, key=lambda item: (item[0], item[1] or "")), start=1
    ):
        base = _safe_identifier(key[0].rsplit(":", 1)[1], fallback="device").lower()
        symbol = base
        suffix = 2
        while symbol in used:
            symbol = f"{base}_{suffix}"
            suffix += 1
        used.add(symbol)
        result[key] = symbol
    return result, imports


def _selector_key(
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
) -> tuple[str, str | None]:
    """读取节点的设备类与可选固定设备身份。

    参数说明：``node`` 携带执行器绑定，``action`` 携带设备类；返回选择器键，
    非法绑定抛出 ``AuthoringGraphError``。
    """

    class_identity = action.template.get("class")
    if not isinstance(class_identity, str) or ":" not in class_identity:
        raise AuthoringGraphError("template_catalog_mismatch", "目录模板缺少设备类")
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    binding = unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
    if binding is None:
        return class_identity, None
    if not isinstance(binding, Mapping) or binding.get("mode") != "fixed":
        raise AuthoringGraphError("candidate_invalid", "节点执行器绑定无效")
    device_id = binding.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise AuthoringGraphError("candidate_invalid", "固定设备身份无效")
    return class_identity, device_id


def _authoring_metadata(
    workflow: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """读取候选工作流的输入合同和输出绑定。

    参数说明：``workflow`` 是工作流投影；返回两个普通字典，缺少保留元数据时
    使用空合同，非法类型失败关闭。
    """

    meta_data = workflow.get("meta_data") or {}
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    if not isinstance(unilab, Mapping):
        return (
            {"version": 1, "parameters": []},
            {"version": 1, "outputs": []},
            {},
        )
    input_contract = unilab.get("input_contract", {"version": 1, "parameters": []})
    output_contract = unilab.get("output_contract", {"version": 1, "outputs": []})
    output_bindings = unilab.get("output_bindings", {})
    if (
        not isinstance(input_contract, Mapping)
        or not isinstance(output_contract, Mapping)
        or not isinstance(output_bindings, Mapping)
    ):
        raise AuthoringGraphError("candidate_invalid", "工作流创作元数据无效")
    return dict(input_contract), dict(output_contract), dict(output_bindings)


def _render_parameter(
    descriptor: Mapping[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> tuple[str, str, Any, set[str], set[tuple[str, str]]]:
    """把输入合同参数渲染为函数参数片段。

    参数说明：``descriptor`` 是版本 1 参数描述；返回名称、注解、默认值和所需
    typing 名称集合及资源模板 import，非法描述抛出 ``AuthoringGraphError``。
    """

    name = descriptor.get("name")
    schema = descriptor.get("schema")
    if not isinstance(name, str) or not isinstance(schema, Mapping):
        raise AuthoringGraphError("candidate_invalid", "工作流输入合同无效")
    annotation, imports, resource_imports = _render_schema(
        dict(schema),
        catalog=catalog,
    )
    default = descriptor.get("default", _NO_DEFAULT)
    return name, annotation, default, imports, resource_imports


def _render_schema(
    schema: dict[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
    include_resource_templates: bool = True,
) -> tuple[str, set[str], set[tuple[str, str]]]:
    """把规范值 Schema 渲染为静态 Python 注解。

    参数说明：``schema`` 是工作流版本 1 值 Schema，``catalog`` 反解本代资源
    模板源码身份；``include_resource_templates=False`` 用于显式结果记录，因为
    其生产者连接点会在回编译时重新给出更精确保证。返回注解文本、所需 typing
    名称和资源模板 import，当前合同之外的 Schema 失败关闭。
    """

    template_uuids = (
        _resource_template_allowlist(schema)
        if include_resource_templates
        else None
    )
    annotation, imports = _render_schema_base(schema)
    resource_imports: set[tuple[str, str]] = set()
    if template_uuids is not None:
        symbols: list[str] = []
        for template_uuid in template_uuids:
            try:
                identity = catalog.require_resource_template_symbol(template_uuid)
            except AuthoringCatalogError as error:
                raise AuthoringGraphError(
                    "template_catalog_mismatch",
                    "工作流合同引用了目录外资源模板",
                ) from error
            module, symbol = identity.rsplit(":", 1)
            resource_imports.add((module, symbol))
            symbols.append(symbol)
        annotation = (
            f"Annotated[{annotation}, "
            f"AllowedResourceTemplates({', '.join(symbols)})]"
        )
        imports.add("Annotated")
    return annotation, imports, resource_imports


def _render_schema_base(schema: dict[str, Any]) -> tuple[str, set[str]]:
    """渲染不含资源模板 metadata 的工作流值 Schema 主体。"""

    if schema.get("$slot") == "ResourceSlot":
        return "ResourceSlot", set()
    if "anyOf" in schema:
        members = schema["anyOf"]
        if (
            not isinstance(members, list)
            or len(members) != 2
            or members[1] != {"type": "null"}
        ):
            raise AuthoringGraphError("candidate_invalid", "nullable Schema 无效")
        base, imports = _render_schema_base(dict(members[0]))
        return f"{base} | None", imports
    value_type = schema.get("type")
    names = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict[str, object]",
    }
    if "enum" in schema:
        values = schema.get("enum")
        if not isinstance(values, list) or not values:
            raise AuthoringGraphError("candidate_invalid", "枚举 Schema 无效")
        return f"Literal[{', '.join(repr(item) for item in values)}]", {"Literal"}
    if value_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise AuthoringGraphError("candidate_invalid", "数组 Schema 无效")
        item_annotation, imports = _render_schema_base(dict(items))
        return f"list[{item_annotation}]", imports
    if value_type not in names:
        raise AuthoringGraphError("candidate_invalid", "暂不支持的输入 Schema")
    annotation = names[value_type]
    field_arguments: list[str] = []
    for schema_key, field_key in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
    ):
        if schema_key in schema:
            field_arguments.append(f"{field_key}={schema[schema_key]!r}")
    if field_arguments:
        return f"Annotated[{annotation}, Field({', '.join(field_arguments)})]", {
            "Annotated"
        }
    return annotation, set()


def _resource_template_allowlist(schema: Mapping[str, Any]) -> list[str] | None:
    """读取值 Schema 中唯一的资源模板允许集合。"""

    found: list[list[str]] = []
    pending = [schema]
    while pending:
        item = pending.pop()
        raw_allowlist = item.get("allowed_resource_template_uuids")
        if raw_allowlist is not None:
            if (
                not isinstance(raw_allowlist, list)
                or not raw_allowlist
                or any(not isinstance(value, str) for value in raw_allowlist)
            ):
                raise AuthoringGraphError(
                    "candidate_invalid",
                    "资源模板允许集合无效",
                )
            found.append(list(raw_allowlist))
        members = item.get("anyOf")
        if isinstance(members, list):
            pending.extend(
                member for member in members if isinstance(member, Mapping)
            )
        child = item.get("items")
        if isinstance(child, Mapping):
            pending.append(child)
    if len(found) > 1:
        raise AuthoringGraphError(
            "candidate_invalid",
            "值 Schema 包含多个资源模板允许集合",
        )
    return found[0] if found else None


def _incoming_bindings(
    edges: list[Any],
    *,
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> dict[tuple[str, str], tuple[str, str]]:
    """按目标节点和目标连接点索引数据边来源。

    参数说明：``edges`` 是完整候选边，``catalog_by_node`` 证明哪些目标连接点
    属于动作参数。返回数据目标二元组到源二元组映射；多个 ``ready`` 控制依赖
    可汇合到同一结构连接点，不进入参数渲染，数据目标重复仍失败关闭。
    """

    data_targets = {
        (node_uuid, str(handle["uuid"]))
        for node_uuid, action in catalog_by_node.items()
        for handle in action.handles
        if handle.get("io_type") == "target"
        and handle.get("handle_key") != "ready"
        and str(handle.get("data_source") or "executor").lower()
        in {"executor", "goal"}
    }
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for value in edges:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        target = (
            str(value.get("target_node_uuid")),
            str(value.get("target_handle_uuid")),
        )
        if target not in data_targets:
            continue
        source = (
            str(value.get("source_node_uuid")),
            str(value.get("source_handle_uuid")),
        )
        if target in result:
            raise AuthoringGraphError(
                "candidate_invalid",
                f"目标连接点存在多条入边：{target[0]}/{target[1]}",
            )
        result[target] = source
    return result


def _render_action_arguments(
    *,
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
    incoming: Mapping[tuple[str, str], tuple[str, str]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> list[str]:
    """渲染一个动作（Action）调用的确定性命名参数。

    参数说明：``node`` 与 ``action`` 提供节点事实和不可变动作合同（Action
    Contract），``incoming`` 提供按目标连接点（Handle）索引的稳定入边，
    ``node_by_uuid`` 与 ``catalog_by_node`` 分别解析源工作流节点（WorkflowNode）
    及其目录中的动作（Action）和输出连接点。返回：按业务键排序的
    ``name=value`` 片段；只渲染遗留 ``executor`` 或第 2 版动作合同 ``goal``
    输入，结构依赖不成为动作参数。异常：连接点身份、输入绑定或必填参数无法
    证明时抛出 ``AuthoringGraphError``，不得按节点顺序或名称猜测。
    """

    # ``node_uuid`` 是待渲染工作流节点（WorkflowNode）的稳定身份，用于精确
    # 查询以目标连接点（Handle）为端点的入边。
    node_uuid = str(node["uuid"])
    # ``params`` 是没有工作流输入或上游边提供者时可使用的节点静态参数事实。
    params = node.get("param") or {}
    # ``unilab`` 与 ``input_bindings`` 保存工作流输入到动作输入连接点（Handle）
    # 的稳定绑定，不允许从参数名称反向猜测绑定。
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    input_bindings = (
        unilab.get("input_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    # ``resource_refs`` 以目标连接点 UUID 保存原部署业务 ID，使实际 UUID 参数
    # 在规范源码中仍能恢复作者声明，而不是退化为匿名字典字面量。
    resource_refs = (
        unilab.get("resource_refs", {}) if isinstance(unilab, Mapping) else {}
    )
    # ``rendered`` 按动作合同（Action Contract）业务键顺序收集最终命名参数。
    rendered: list[str] = []
    # ``target_handles`` 只包含动作（Action）数据输入；ready 等结构连接点
    # （Handle）不得被渲染成设备动作（Action）参数。
    target_handles = sorted(
        (
            handle
            for handle in action.handles
            if handle.get("io_type") == "target"
            and handle.get("handle_key") != "ready"
            and str(handle.get("data_source") or "executor").lower()
            in {"executor", "goal"}
        ),
        key=lambda item: str(item.get("handle_key")),
    )
    for handle in target_handles:
        # ``handle_uuid`` 是动作输入连接点（Handle）的稳定身份；``key`` 是动作
        # 合同（Action Contract）冻结的业务参数名。
        handle_uuid = str(handle["uuid"])
        key = str(handle["handle_key"])
        # ``expression`` 只接受工作流输入绑定、精确入边或静态参数三种可证明
        # 来源；空值表示当前没有合法提供者。
        expression: str | None = None
        if handle_uuid in resource_refs:
            # ``resource_binding`` 必须是含唯一非空业务 ID 的保留元数据；对应静态
            # 参数仍须存在实际物料 UUID，避免伪造元数据生成未验证引用。
            resource_binding = resource_refs[handle_uuid]
            resource_id = (
                resource_binding.get("resource_id")
                if isinstance(resource_binding, Mapping)
                else None
            )
            material_reference = params.get(key)
            if (
                not isinstance(resource_id, str)
                or not resource_id.strip()
                or resource_id != resource_id.strip()
                or not isinstance(material_reference, Mapping)
                or not isinstance(material_reference.get("uuid"), str)
            ):
                raise AuthoringGraphError(
                    "candidate_invalid", "动作资源引用元数据或实际物料身份无效"
                )
            expression = (
                f"resource_ref({json.dumps(resource_id, ensure_ascii=False)})"
            )
        elif handle_uuid in input_bindings:
            # ``binding`` 是当前目标连接点（Handle）对应的工作流输入绑定事实；
            # 必须按连接点 UUID 查询，避免根据动作参数名称猜测绑定。
            binding = input_bindings[handle_uuid]
            if not isinstance(binding, Mapping) or not isinstance(
                binding.get("parameter"), str
            ):
                raise AuthoringGraphError("candidate_invalid", "节点输入绑定无效")
            expression = str(binding["parameter"])
        elif (node_uuid, handle_uuid) in incoming:
            # ``source_node_uuid`` 与 ``source_handle_uuid`` 是候选边冻结的源端点
            # 身份，不能替换成节点顺序或展示名称。
            source_node_uuid, source_handle_uuid = incoming[(node_uuid, handle_uuid)]
            # ``source_node`` 与 ``source_action`` 共同解析源结果变量和真实输出
            # 连接点（Handle），保持物料来源（MaterialSource）与普通动作共用路径。
            source_node = node_by_uuid[source_node_uuid]
            source_action = catalog_by_node[source_node_uuid]
            # ``source_handle`` 必须由边上的稳定 UUID 在源目录聚合中唯一命中。
            source_handle = next(
                (
                    item
                    for item in source_action.handles
                    if str(item["uuid"]) == source_handle_uuid
                ),
                None,
            )
            if source_handle is None:
                raise AuthoringGraphError(
                    "candidate_invalid", "数据边源连接点不在目录中"
                )
            # ``source_name`` 是源节点冻结的作者结果变量；只有物料来源
            # （MaterialSource）唯一输出直接引用变量本身，普通动作引用具名结果。
            source_name = _node_result_name(source_node)
            expression = (
                source_name
                if _is_material_source(source_action)
                and source_handle.get("handle_key") == "material"
                else f"{source_name}.{source_handle['handle_key']}"
            )
        elif key in params:
            expression = repr(params[key])
        if expression is not None:
            rendered.append(f"{key}={expression}")
        elif bool(handle.get("required")):
            raise AuthoringGraphError("candidate_invalid", f"动作缺少必填参数 {key}")
    return rendered


def _render_output_binding(
    binding: Any,
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> str:
    """渲染一个工作流输出表达式。

    参数说明：``binding`` 是保留元数据中的输出绑定，另两个索引解析节点结果；
    返回 Python 表达式，非法身份失败关闭。
    """

    if not isinstance(binding, Mapping):
        raise AuthoringGraphError("candidate_invalid", "工作流输出绑定无效")
    if binding.get("kind") == "workflow_input":
        parameter = binding.get("parameter")
        if not isinstance(parameter, str):
            raise AuthoringGraphError("candidate_invalid", "工作流输入输出绑定无效")
        return parameter
    if binding.get("kind") != "node_output":
        raise AuthoringGraphError("candidate_invalid", "未知工作流输出绑定类型")
    node_uuid = validate_uuid(binding.get("workflow_node_uuid"))
    handle_uuid = validate_uuid(binding.get("source_handle_uuid"))
    node = node_by_uuid[node_uuid]
    action = catalog_by_node[node_uuid]
    handle = next(
        (item for item in action.handles if str(item["uuid"]) == handle_uuid),
        None,
    )
    if handle is None or handle.get("io_type") != "source":
        raise AuthoringGraphError("candidate_invalid", "工作流输出连接点无效")
    result_name = _node_result_name(node)
    if _is_material_source(action) and handle.get("handle_key") == "material":
        return result_name
    return f"{result_name}.{handle['handle_key']}"


def _is_material_source(action: AuthoringCatalogAction) -> bool:
    """判断目录 aggregate 是否为框架物料来源（MaterialSource）。

    参数说明：``action`` 是节点模板与连接点（Handle）的不可变聚合。返回：
    仅当模板类型和节点类型同时为 ``material_source`` 时为真。
    """

    return (
        action.template.get("type") == "material_source"
        and action.template.get("node_type") == "material_source"
    )


def _node_result_name(node: Mapping[str, Any]) -> str:
    """读取与节点展示标题分离的 Python 动作结果变量。

    参数说明：``node`` 是候选工作流节点（WorkflowNode）；返回可用于生成数据
    依赖表达式的 Python 标识符。新图优先读取 ``authoring_result_name``，旧图才
    从节点名称兼容推导；伪造或不可规范化的显式身份失败关闭。
    """

    metadata = node.get("meta_data") or {}
    unilab = metadata.get("unilab", {}) if isinstance(metadata, Mapping) else {}
    explicit = (
        unilab.get("authoring_result_name") if isinstance(unilab, Mapping) else None
    )
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise AuthoringGraphError("candidate_invalid", "节点作者结果变量无效")
        normalized = _safe_identifier(explicit, fallback="result")
        if normalized != explicit:
            raise AuthoringGraphError(
                "candidate_invalid", "节点作者结果变量不是稳定标识符"
            )
        return explicit
    return _safe_identifier(str(node.get("name") or "result"), fallback="result")


def _node_metadata_comment(
    *,
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
) -> str | None:
    """把节点标题和描述渲染为规范单行展示注释。

    参数说明：``node`` 提供当前展示字段，``action`` 提供动作模板（Action
    Template）的默认显示名和描述；当节点展示字段等于模板默认值时返回
    ``None``，否则返回 ``# [标题]: 描述``。无法无损表示的换行、右方括号或
    空描述会抛 ``AuthoringGraphError`` 拒绝生成，避免源码往返静默改变候选图。
    """

    title = node.get("name")
    description = node.get("description")
    # 动作模板显示名是人类可读默认值，动作业务名只用于兼容旧目录。
    template_title = action.template.get("display_name") or action.template.get("name")
    template_description = action.template.get("description")
    descriptions_match = (
        description == template_description
        or (
            title == template_title
            and description in (None, "")
        )
    )
    if title == template_title and descriptions_match:
        return None
    if not isinstance(title, str) or not title.strip():
        raise AuthoringGraphError("candidate_invalid", "节点展示标题不能为空")
    if not isinstance(description, str) or not description.strip():
        raise AuthoringGraphError("candidate_invalid", "自定义节点展示必须包含描述")
    normalized_title = title.strip()
    normalized_description = description.strip()
    if "]" in normalized_title or "\n" in normalized_title or "\r" in normalized_title:
        raise AuthoringGraphError("candidate_invalid", "节点展示标题不能写入单行注释")
    if "\n" in normalized_description or "\r" in normalized_description:
        raise AuthoringGraphError("candidate_invalid", "节点展示描述不能写入单行注释")
    return f"# [{normalized_title}]: {normalized_description}"


def _safe_identifier(value: str, *, fallback: str) -> str:
    """把展示文本规范为安全 Python 局部名称。

    参数说明：``value`` 是候选名称，``fallback`` 是清洗为空时的替代；返回非
    关键字标识符，不执行任何源码。
    """

    normalized = re.sub(r"\W+", "_", value, flags=re.UNICODE).strip("_")
    if not normalized or normalized[0].isdigit():
        normalized = fallback
    if keyword.iskeyword(normalized):
        normalized = f"{normalized}_value"
    return normalized


__all__ = ["RenderedAuthoringSource", "render_authoring_python"]
