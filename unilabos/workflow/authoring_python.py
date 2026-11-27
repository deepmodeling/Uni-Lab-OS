"""后端形状工作流候选图到规范 Python 源码的确定性生成层。"""

from __future__ import annotations

import ast
import json
import keyword
import re
from collections import defaultdict, deque
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
from unilabos.workflow.authoring_material_io import (
    MaterialIOAuthoringError,
    RenderedMaterialIO,
    render_material_io_call,
)
from unilabos.workflow.manual_confirmation import normalize_manual_confirmation_config
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.models import CandidateSourceMapEntry, validate_uuid
from unilabos.workflow.presentation_metadata import PRESENTATION_KEYS
from unilabos.workflow.source_coordinates import utf16_length
from unilabos.workflow.store import StoreConflict


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
    inline_expanded_composites: bool = False,
) -> RenderedAuthoringSource:
    """把完整候选图渲染为规范作者 Python。

    参数说明：``graph`` 是后端五集合候选图，``catalog`` 是同一编译事务目录
    快照；``function_docstring`` 是可信 AST 提取并清理的可选工作流函数文档；
    ``inline_expanded_composites`` 为真时把已展开的组合调用写成内部条件/循环
    源码，供 JSON 导入在没有已发布子流程目录时仍能规范化。
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
    inventory_requirements = graph.get("inventory_requirements", [])
    if (
        not isinstance(workflow, Mapping)
        or not isinstance(nodes, list)
        or not isinstance(edges, list)
        or not isinstance(inventory_requirements, list)
    ):
        raise AuthoringGraphError("candidate_invalid", "候选图缺少工作流、节点或边")
    workflow_uuid = validate_uuid(workflow.get("uuid"))
    all_nodes = _node_index(nodes)
    all_catalog = _catalog_projection(all_nodes, catalog)
    for action in all_catalog.values():
        canonical_name = str(action.template["name"])
        if canonical_name.startswith("auto-"):
            source_name = canonical_name.removeprefix("auto-")
            resolved = catalog.require_action(str(action.template["class"]), source_name)
            if resolved.template["uuid"] != action.template["uuid"]:
                raise AuthoringGraphError("template_catalog_mismatch", "自动前缀动作与同名 Python 方法目录身份冲突")
    hidden_composite_nodes = _composite_internal_node_uuids(
        all_nodes,
        all_catalog,
    )
    node_by_uuid = {
        key: value
        for key, value in all_nodes.items()
        if key not in hidden_composite_nodes
    }
    catalog_by_node = {key: all_catalog[key] for key in node_by_uuid}
    # 组合调用带入的子需求由展开推导，源码只保留本工作流自己声明的数量需求。
    inventory_requirements = [
        requirement
        for requirement in inventory_requirements
        if not _is_composite_requirement(requirement, hidden_composite_nodes)
    ]
    visible_edges = [
        edge
        for edge in edges
        if isinstance(edge, Mapping)
        and edge.get("source_node_uuid") in node_by_uuid
        and edge.get("target_node_uuid") in node_by_uuid
    ]
    workflow_unilab = (workflow.get("meta_data") or {}).get("unilab", {})
    owns_resource_scopes = isinstance(workflow_unilab, Mapping) and bool(
        workflow_unilab.get("resources")
        or any(
            isinstance(scope, Mapping) and not scope.get("composite_invocation_uuid")
            for scope in workflow_unilab.get("resource_scopes", [])
        )
    )
    ordered_nodes = _authoring_ordered_nodes(
        node_by_uuid, visible_edges, preserve_source_order=owns_resource_scopes,
    )
    if inline_expanded_composites:
        inline_nodes = [
            node
            for node_uuid, node in all_nodes.items()
            if not _is_published_workflow(all_catalog[node_uuid])
        ]
        device_symbols, device_imports = _device_symbols(inline_nodes, all_catalog)
        published_workflow_imports = {
            tuple(str(action.template["class"]).rsplit(":", 1))
            for node_uuid, action in all_catalog.items()
            if _is_published_workflow(action)
            and not _expanded_composite_children(node_uuid, all_nodes)
        }
        material_scan_nodes = inline_nodes
    else:
        device_symbols, device_imports = _device_symbols(ordered_nodes, catalog_by_node)
        published_workflow_imports = {
            tuple(str(action.template["class"]).rsplit(":", 1))
            for node in ordered_nodes
            for action in [catalog_by_node[str(node["uuid"])]]
            if _is_published_workflow(action)
        }
        material_scan_nodes = ordered_nodes
    # ``material_sources`` 冻结每个物料来源节点的 import 与调用表达式。
    material_sources: dict[str, RenderedMaterialSource | RenderedMaterialIO] = {}
    for node in material_scan_nodes:
        node_uuid = str(node["uuid"])
        action = (
            all_catalog[node_uuid]
            if inline_expanded_composites
            else catalog_by_node[node_uuid]
        )
        try:
            if _is_material_source(action):
                material_sources[node_uuid] = render_material_source_call(
                    node,
                    catalog=catalog,
                )
            elif _is_material_io(action):
                material_sources[node_uuid] = render_material_io_call(
                    node,
                    catalog=catalog,
                )
            else:
                continue
        except (MaterialAuthoringError, MaterialIOAuthoringError) as error:
            raise AuthoringGraphError(error.code, error.message) from error
    # ``material_imports`` 与设备 import 合并后按限定身份稳定排序。
    material_imports = {
        rendered.resource_import
        for rendered in material_sources.values()
        if rendered.resource_import is not None
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
    output_descriptors = {
        str(item["name"]): item
        for item in explicit_outputs
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    output_annotations = {
        name: _render_schema(
            dict(output_schemas[name]),
            catalog=catalog,
            include_resource_templates=False,
            unit=output_descriptors[name].get("unit"),
        )
        for name in explicit_output_bindings
    }
    typing_names: set[str] = {"TypedDict"} if explicit_output_bindings else set()
    needs_field = False
    needs_resource_slot = False
    needs_json_value = False
    annotation_resource_imports: set[tuple[str, str]] = set()
    for _name, annotation, _default, imports, resource_imports in annotations:
        typing_names.update(imports & {"Annotated", "Literal"})
        needs_json_value = needs_json_value or "JSONValue" in imports
        annotation_resource_imports.update(resource_imports)
        needs_field = needs_field or "Field(" in annotation
        needs_resource_slot = needs_resource_slot or "ResourceSlot" in annotation
    for annotation, imports, resource_imports in output_annotations.values():
        typing_names.update(imports & {"Annotated", "Literal"})
        needs_json_value = needs_json_value or "JSONValue" in imports
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
    if needs_json_value:
        lines.append("from unilabos.registry.annotations import JSONValue")
    if needs_resource_slot:
        lines.append("from unilabos.registry.placeholder_type import ResourceSlot")
    control_scan_nodes = (
        list(all_nodes.values()) if inline_expanded_composites else ordered_nodes
    )
    control_scan_catalog = all_catalog if inline_expanded_composites else catalog_by_node
    group_nodes = [
        node
        for node in control_scan_nodes
        if _is_group(control_scan_catalog[str(node["uuid"])])
    ]
    condition_nodes = [
        node
        for node in control_scan_nodes
        if _is_condition(control_scan_catalog[str(node["uuid"])])
    ]
    repeat_nodes = [
        node
        for node in control_scan_nodes
        if _is_repeat_until(control_scan_catalog[str(node["uuid"])])
    ]
    explicit_control_dependencies = _explicit_control_dependencies(
        ordered_nodes=ordered_nodes, edges=visible_edges, catalog_by_node=catalog_by_node,
    )
    marker_imports = "device, workflow"
    workflow_unilab = (workflow.get("meta_data") or {}).get("unilab", {})
    has_resource_scopes = isinstance(workflow_unilab, Mapping) and bool(
        workflow_unilab.get("resources") or workflow_unilab.get("resource_scopes")
    )
    if has_resource_scopes:
        marker_imports += ", resources"
    if group_nodes:
        marker_imports += ", group"
        if any(_parallel_scope(node) is not None for node in group_nodes):
            marker_imports += ", parallel"
    if explicit_control_dependencies and "parallel" not in {item.strip() for item in marker_imports.split(",")}:
        marker_imports += ", parallel"
    if repeat_nodes:
        marker_imports += ", repeat_until, until"
    if inventory_requirements:
        marker_imports += ", quantity_requirement"
    if material_sources:
        marker_imports += ", resource_ref"
        if any(isinstance(value, RenderedMaterialSource) for value in material_sources.values()):
            marker_imports += ", MaterialCustodyPolicy, MaterialFlowRole, material_source"
        if any(isinstance(value, RenderedMaterialIO) for value in material_sources.values()):
            marker_imports += ", material_io"
    elif any(
        isinstance((node.get("meta_data") or {}).get("unilab"), Mapping)
        and bool((node.get("meta_data") or {})["unilab"].get("resource_refs"))
        for node in (
            all_nodes.values() if inline_expanded_composites else ordered_nodes
        )
    ):
        # 普通动作也可用 ``resource_ref``；只有真实节点元数据声明时才生成 import。
        marker_imports += ", resource_ref"
    if any(
        isinstance((node.get("meta_data") or {}).get("unilab"), Mapping)
        and bool((node.get("meta_data") or {})["unilab"].get("site_group_bindings"))
        for node in (
            all_nodes.values() if inline_expanded_composites else ordered_nodes
        )
    ):
        marker_imports += ", site_group"
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
    from unilabos.workflow.native_commands import COMMAND_CLASSES
    if any(identity in COMMAND_CLASSES for identity, _ in device_symbols):
        lines.insert(0, "from unilabos.workflow.authoring import service")
    for selector_key, symbol in device_symbols.items():
        class_identity, device_id = selector_key
        class_name = class_identity.rsplit(":", 1)[1]
        argument = "" if device_id is None else repr(device_id)
        selector = "service" if class_identity in COMMAND_CLASSES else "device"
        lines.append(f"{symbol}: {class_name} = {selector}({argument})")
    lines.extend(["", ""])
    lines.extend(
        [
            "@workflow(",
            f'    workflow_uuid="{workflow_uuid}",',
            f"    displayname={workflow.get('name')!r},",
        ]
    )
    root_fields = _authoring_root_fields(workflow)
    if "resources" in root_fields:
        raw_resources = (
            workflow_unilab.get("resources")
            if isinstance(workflow_unilab, Mapping)
            else None
        )
        if not isinstance(raw_resources, list) or not raw_resources:
            raise AuthoringGraphError(
                "candidate_invalid", "工作流根 resources 创作元数据无效"
            )
        lines.append(f"    resources={tuple(str(value) for value in raw_resources)!r},")
    if "tags" in root_fields:
        lines.append(f"    tags={_stable_python_json(workflow.get('tags') or [])!r},")
    if "meta_data" in root_fields:
        lines.append(
            "    meta_data="
            f"{_stable_python_json(_public_workflow_meta_data(workflow))!r},"
        )
    if "workflow_type" in root_fields:
        lines.append(f"    workflow_type={workflow.get('workflow_type')!r},")
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

    render_nodes = all_nodes if inline_expanded_composites else node_by_uuid
    render_catalog = all_catalog if inline_expanded_composites else catalog_by_node
    inlined_invocations = (
        _inlined_composite_uuids(all_nodes, all_catalog)
        if inline_expanded_composites
        else set()
    )
    incoming_edges = (
        _rewrite_inlined_composite_edges(
            edges if inline_expanded_composites else visible_edges,
            inlined_invocations=inlined_invocations,
            all_nodes=all_nodes,
            catalog_by_node=render_catalog,
        )
        if inline_expanded_composites
        else list(visible_edges)
    )
    incoming = _incoming_bindings(
        incoming_edges,
        catalog_by_node=render_catalog,
    )
    source_map: list[dict[str, Any]] = []
    # Python 动作结果变量承载节点间数据依赖，必须唯一且不能被节点展示标题改写。
    result_names: set[str] = set()
    result_name_by_uuid: dict[str, str] = {}
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
    ready_pairs = _ready_control_pairs(incoming_edges, render_catalog)
    linked_pairs = _sequential_pairs(incoming_edges, render_catalog)

    def append_actions(nodes: list[dict[str, Any]], indent_level: int) -> None:
        """根动作与分组内动作共享依赖还原，禁止因展示层级改变并行语义。"""
        pending = deque(nodes)
        while pending:
            first = pending.popleft()
            first_uuid = str(first["uuid"])
            action = render_catalog[first_uuid]
            if _is_condition(action) or _is_repeat_until(action):
                renderer = _append_condition_source if _is_condition(action) else _append_repeat_until_source
                renderer(
                    node=first, indent_level=indent_level, lines=lines, source_map=source_map,
                    result_names=result_names, material_sources=material_sources, incoming=incoming,
                    node_by_uuid=render_nodes, catalog_by_node=render_catalog,
                    device_symbols=device_symbols, explicit_control_dependencies=explicit_control_dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )
                rendered_node_uuids.add(first_uuid)
                continue

            batch = [first]
            batch_ids = {first_uuid}
            prerequisites = explicit_control_dependencies.get(first_uuid)
            if prerequisites is not None and not (_is_material_source(action) or _is_material_io(action)):
                # 仅将同前序且相互独立的相邻动作放入同一并行块。
                while pending:
                    next_uuid = str(pending[0]["uuid"])
                    if explicit_control_dependencies.get(next_uuid) != prerequisites:
                        break
                    if _is_material_io(render_catalog[next_uuid]) or any(
                        (member, next_uuid) in linked_pairs or (next_uuid, member) in linked_pairs
                        for member in batch_ids
                    ):
                        break
                    batch.append(pending.popleft())
                    batch_ids.add(next_uuid)

            parallel_batch = len(batch) > 1
            dependencies = explicit_control_dependencies
            if parallel_batch:
                names = [
                    _python_result_name(render_nodes[source], result_name_by_uuid)
                    for source in prerequisites
                ]
                lines.append("    " * indent_level + f"with parallel(depends_on=[{', '.join(names)}]):")
                dependencies = {key: value for key, value in dependencies.items() if key not in batch_ids}
            for action_node in batch:
                _append_action_source(
                    node=action_node, indent_level=indent_level + int(parallel_batch), lines=lines,
                    source_map=source_map, result_names=result_names,
                    material_sources=material_sources, incoming=incoming,
                    node_by_uuid=render_nodes, catalog_by_node=render_catalog,
                    device_symbols=device_symbols,
                    explicit_control_dependencies=dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )
                rendered_node_uuids.add(str(action_node["uuid"]))

    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if node_uuid in rendered_node_uuids or isinstance(node.get("parent_uuid"), str):
            continue
        action = catalog_by_node[node_uuid]
        if (
            inline_expanded_composites
            and _is_published_workflow(action)
            and _expanded_composite_children(node_uuid, all_nodes)
        ):
            _append_expanded_composite_source(
                node=node,
                indent_level=1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                all_nodes=all_nodes,
                all_catalog=all_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            rendered_node_uuids.add(node_uuid)
            continue
        if _is_condition(action):
            _append_condition_source(
                node=node,
                indent_level=1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=render_nodes,
                catalog_by_node=render_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            rendered_node_uuids.add(node_uuid)
            rendered_node_uuids.update(
                str(child["uuid"])
                for child in condition_nodes
                if child.get("parent_uuid") == node_uuid
            )
            rendered_node_uuids.update(
                str(child["uuid"]) for child in children_by_parent[node_uuid]
            )
            continue
        if _is_repeat_until(action):
            _append_repeat_until_source(
                node=node,
                indent_level=1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=render_nodes,
                catalog_by_node=render_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            rendered_node_uuids.add(node_uuid)
            rendered_node_uuids.update(
                str(child["uuid"]) for child in children_by_parent[node_uuid]
            )
            continue
        if _is_material_source_declaration(node, action):
            # 此类库存声明没有执行入口，不能生成 AST 不接受的并行动作分支。
            _append_action_source(
                node=node,
                indent_level=1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=render_nodes,
                catalog_by_node=render_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            rendered_node_uuids.add(node_uuid)
            continue
        if not _is_group(action):
            run = _top_level_action_run(
                node_uuid,
                ordered_nodes,
                catalog_by_node=render_catalog,
                rendered=rendered_node_uuids,
                inline_expanded_composites=inline_expanded_composites,
                all_nodes=all_nodes,
            )
            append_actions(run, 1)
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
                    action=render_catalog[str(group_node["uuid"])],
                )
                rendered_node_uuids.add(str(group_node["uuid"]))
                append_actions(children_by_parent[str(group_node["uuid"])], 3)
            continue
        _append_group_source(
            node=node,
            indent_level=1,
            lines=lines,
            source_map=source_map,
            action=action,
        )
        rendered_node_uuids.add(node_uuid)
        append_actions(children_by_parent[node_uuid], 2)
    # 展示分组不会表达跨分支依赖；保留真实动作之间的原生 ready 注释。
    for source_uuid, target_uuid in sorted(ready_pairs):
        source_parent = render_nodes[source_uuid].get("parent_uuid")
        target_parent = render_nodes[target_uuid].get("parent_uuid")
        if (
            target_uuid not in explicit_control_dependencies
            and source_parent != target_parent
            and (source_parent is None or (
                source_parent in render_catalog and _is_group(render_catalog[source_parent])
            ))
            and (target_parent is None or (
                target_parent in render_catalog and _is_group(render_catalog[target_parent])
            ))
        ):
            lines.append(f"    # unilab:ready_edge source={source_uuid} target={target_uuid}")
    _append_quantity_requirement_sources(
        lines=lines,
        requirements=inventory_requirements,
        node_by_uuid=node_by_uuid,
        material_sources=material_sources,
        result_name_by_uuid=result_name_by_uuid,
    )
    reservations = [
        scope for scope in workflow_unilab.get("resource_scopes", [])
        if scope.get("kind") == "reservation" and not scope.get("composite_invocation_uuid")
    ]
    if reservations:
        occupied_names = result_names | set(device_symbols.values()) | {
            item["name"] for item in input_contract.get("parameters", [])
        } | {symbol for _, symbol in (
            device_imports | material_imports | published_workflow_imports | annotation_resource_imports
        )}
        receiver = "locks"
        while receiver in occupied_names:
            receiver += "_"
        lines.append(f"    with resources() as {receiver}:")
    for scope in reservations:
        members = scope.get("node_uuids", [])
        # Parent reservations contain expanded operation members in the graph.
        # Collapsed Python must reference their visible operation call instead.
        if not inline_expanded_composites:
            visible_members = []
            for identity in members:
                seen = set()
                while identity in hidden_composite_nodes and identity not in seen:
                    seen.add(identity)
                    identity = all_nodes[identity].get("parent_uuid")
                if identity not in visible_members:
                    visible_members.append(identity)
            members = visible_members
        if not members or any(identity not in result_name_by_uuid for identity in members):
            raise AuthoringGraphError("candidate_invalid", "预留范围包含无法引用的节点")
        if not scope.get("resources"):
            raise AuthoringGraphError("candidate_invalid", "资源预留必须显式指定资源别名；请迁移旧的节点集合声明")
        references = ", ".join(result_name_by_uuid[identity] for identity in members)
        resources = "".join(f"{alias!r}, " for alias in scope.get("resources", []))
        lines.append(f"        {receiver}.hold({resources}nodes=[{references}], name={scope.get('name', '')!r})")
    if explicit_output_bindings:
        # 输出绑定字典由编译器按作者声明顺序建立；保留该顺序才能让输出合同
        # 在 Python→图→Python 往返中达到固定点。
        output_nodes = all_nodes if inline_expanded_composites else node_by_uuid
        output_catalog = all_catalog if inline_expanded_composites else catalog_by_node
        rendered_outputs = [
            f"{name}={_render_output_binding(binding, output_nodes, output_catalog, result_name_by_uuid=result_name_by_uuid, inlined_invocations=inlined_invocations)}"
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
    _apply_resource_scope_sources(
        lines=lines,
        source_map=source_map,
        resource_scopes=(
            workflow_unilab.get("resource_scopes", [])
            if isinstance(workflow_unilab, Mapping)
            else []
        ),
    )
    return RenderedAuthoringSource(
        python_source="\n".join(lines).rstrip() + "\n",
        source_map=source_map,
    )


def _is_composite_requirement(requirement: Any, hidden_nodes: set[str] | frozenset[str]) -> bool:
    """需求来自组合调用展开：带展开来源标记，或消费动作是被隐藏的子图内部节点。"""
    if not isinstance(requirement, Mapping):
        return False
    unilab = (requirement.get("meta_data") or {}).get("unilab") if isinstance(requirement.get("meta_data"), Mapping) else None
    if isinstance(unilab, Mapping) and unilab.get("composite_origin"):
        return True
    return str(requirement.get("consume_node_uuid") or "") in hidden_nodes


def _append_quantity_requirement_sources(
    *,
    lines: list[str],
    requirements: list[Any],
    node_by_uuid: Mapping[str, dict[str, Any]],
    material_sources: Mapping[str, RenderedMaterialSource],
    result_name_by_uuid: Mapping[str, str] | None = None,
) -> None:
    """在工作流返回前生成不参与 DAG 执行的数量需求标记。"""

    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            raise AuthoringGraphError("candidate_invalid", "数量库存需求必须是对象")
        if (
            requirement.get("target_type") != "current_substance"
            or requirement.get("allow_split") is not False
        ):
            raise AuthoringGraphError(
                "candidate_invalid",
                "Python 创作当前只支持单容器当前内容物数量需求",
            )
        metadata = requirement.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("quantity_binding") if isinstance(unilab, Mapping) else None
        )
        source_uuid = str(
            unilab.get("material_source_node_uuid")
            if isinstance(unilab, Mapping)
            else ""
        )
        consume_uuid = str(requirement.get("consume_node_uuid") or "")
        if (
            not isinstance(binding, Mapping)
            or source_uuid not in material_sources
            or consume_uuid not in node_by_uuid
        ):
            raise AuthoringGraphError(
                "candidate_invalid",
                "数量需求缺少可恢复的物料来源、消费动作或数量绑定",
            )
        if binding.get("kind") == "workflow_input":
            quantity_expression = str(binding.get("parameter") or "")
            if (
                not quantity_expression.isidentifier()
                or keyword.iskeyword(quantity_expression)
            ):
                raise AuthoringGraphError(
                    "candidate_invalid",
                    "数量需求工作流输入绑定无效",
                )
        elif binding.get("kind") == "literal":
            quantity_expression = repr(binding.get("value"))
        else:
            raise AuthoringGraphError("candidate_invalid", "数量需求绑定类型无效")
        source_name = _python_result_name(
            node_by_uuid[source_uuid],
            result_name_by_uuid,
        )
        consume_name = _python_result_name(
            node_by_uuid[consume_uuid],
            result_name_by_uuid,
        )
        requirement_key = requirement.get("requirement_key")
        quantity_unit = requirement.get("quantity_unit")
        scale = unilab.get("quantity_scale", 1.0)
        description = requirement.get("description")
        if not isinstance(requirement_key, str) or not requirement_key:
            raise AuthoringGraphError("candidate_invalid", "数量需求键无效")
        if not isinstance(quantity_unit, str) or not quantity_unit:
            raise AuthoringGraphError("candidate_invalid", "数量需求单位无效")
        arguments = [
            f"requirement_key={requirement_key!r}",
            f"source={source_name}",
            f"consume={consume_name}",
            f"quantity={quantity_expression}",
            f"quantity_unit={quantity_unit!r}",
        ]
        if float(scale) != 1.0:
            arguments.append(f"scale={float(scale)!r}")
        if description is not None:
            if not isinstance(description, str) or not description:
                raise AuthoringGraphError("candidate_invalid", "数量需求说明无效")
            arguments.append(f"description={description!r}")
        lines.append(f"    quantity_requirement({', '.join(arguments)})")


def _apply_resource_scope_sources(
    *,
    lines: list[str],
    source_map: list[dict[str, Any]],
    resource_scopes: Any,
) -> None:
    """把候选图中的资源范围包回确定性 Python 源码。

    资源范围不拥有执行节点；此处仅按入口/出口节点的源码映射插入词法上下文，
    同时修正后续 UTF-16 行号。资源范围必须覆盖可定位的真实节点，未知范围由
    候选图编译阶段失败关闭。
    """

    if not isinstance(resource_scopes, list) or not resource_scopes:
        return
    positions = {
        str(item.get("workflow_node_uuid")): item
        for item in source_map
        if isinstance(item, Mapping) and item.get("workflow_node_uuid")
    }
    # 组合子作用域由被调用实验操作的源码拥有；父源码只保留调用表达式，不能把
    # 子图节点反向渲染成第二份 ``with resources(...)``。下一轮静态展开会按
    # provenance 字段从同一冻结合同确定性恢复这些派生作用域。
    scopes = [
        item
        for item in resource_scopes
        if isinstance(item, Mapping)
        and item.get("composite_invocation_uuid") is None
        and item.get("kind") != "reservation"
    ]

    parent_by_scope = {
        str(item.get("scope_id")): str(item.get("parent_scope_id"))
        for item in scopes
        if item.get("scope_id")
    }

    def scope_depth(scope: Mapping[str, Any]) -> int:
        """返回词法作用域深度；同一入口时父范围必须先插入。"""

        depth = 0
        current = str(scope.get("scope_id") or "")
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            parent = parent_by_scope.get(current, "")
            if not parent:
                break
            depth += 1
            current = parent
        return depth

    scopes.sort(
        key=lambda item: (
            -int(positions.get(str(item.get("entry_node_uuid")), {}).get("start_line", 0)),
            scope_depth(item),
            int(positions.get(str(item.get("exit_node_uuid")), {}).get("end_line", 0)),
        )
    )
    for scope in scopes:
        aliases = scope.get("resources") or scope.get("resource_aliases")
        entry_uuid = str(scope.get("entry_node_uuid") or "")
        exit_uuid = str(scope.get("exit_node_uuid") or "")
        entry = positions.get(entry_uuid)
        exit_item = positions.get(exit_uuid)
        if (
            not isinstance(aliases, list)
            or not aliases
            or entry is None
            or exit_item is None
        ):
            raise AuthoringGraphError(
                "candidate_invalid", "资源作用域缺少可恢复的入口/出口节点"
            )
        if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            raise AuthoringGraphError("candidate_invalid", "资源作用域别名无效")
        members = set(scope.get("node_uuids") or ())
        member_positions = [positions[node_id] for node_id in members if node_id in positions]
        # 范围也可能拥有展示分组；入口/出口只描述执行动作，不能丢掉分组成员。
        start_line = min(int(item["start_line"]) for item in [entry, *member_positions]) - 1
        end_line = max(int(item["end_line"]) for item in [exit_item, *member_positions]) - 1
        if start_line < 0 or end_line < start_line or end_line >= len(lines):
            raise AuthoringGraphError("candidate_invalid", "资源作用域源码范围无效")
        # parallel 没有节点映射；范围从它的首个动作开始时必须包住整个语法块，
        # 不能把 resources 插进 parallel 形成非法单分支。只提升已完整覆盖的块。
        for statement in reversed(list(ast.walk(ast.parse("\n".join(lines))))):
            if not isinstance(statement, ast.With) or len(statement.items) != 1:
                continue
            expression = statement.items[0].context_expr
            if not (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "parallel"
            ):
                continue
            block_start, block_end = statement.lineno - 1, statement.end_lineno - 1
            contained = {
                node_id for node_id, position in positions.items()
                if block_start <= int(position["start_line"]) - 1 <= block_end
            }
            if (
                contained and contained <= members
                and block_start <= start_line <= block_end
            ):
                start_line = block_start
                end_line = max(end_line, block_end)
        indentation = lines[start_line][: len(lines[start_line]) - len(lines[start_line].lstrip())]
        for index in range(start_line, end_line + 1):
            lines[index] = "    " + lines[index]
        lines.insert(
            start_line,
            indentation + f"with resources({', '.join(repr(str(alias)) for alias in aliases)}):",
        )
        for item in source_map:
            for field in ("start_line", "end_line"):
                value = int(item[field])
                if value - 1 >= start_line:
                    item[field] = value + 1


def _public_workflow_meta_data(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """返回可由领域 Python 源码拥有的工作流公开元数据。"""

    meta_data = workflow.get("meta_data")
    public = dict(meta_data) if isinstance(meta_data, Mapping) else {}
    public.pop("unilab", None)
    return public


def _authoring_root_fields(workflow: Mapping[str, Any]) -> set[str]:
    """读取由领域 Python 明确拥有的可选工作流根字段。

    参数：``workflow`` 是当前完整图中的工作流根投影。返回：允许生成器写回的
    ``tags``、``meta_data``、``workflow_type``、``resources`` 子集；旧图未声明
    所有权时为空。
    异常：无；畸形元数据按未声明处理，不把运行派生字段写入作者源码。
    """

    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    values = (
        unilab.get("authoring_root_fields") if isinstance(unilab, Mapping) else None
    )
    if not isinstance(values, list):
        return set()
    return {
        value
        for value in values
        if value in {"tags", "meta_data", "workflow_type", "resources"}
    }


def _stable_python_json(value: Any) -> Any:
    """把 JSON 对象递归整理为可稳定 ``repr`` 的 Python 字面量。"""

    if isinstance(value, Mapping):
        return {
            str(key): _stable_python_json(value[key]) for key in sorted(value, key=str)
        }
    if isinstance(value, list):
        return [_stable_python_json(item) for item in value]
    return value


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
    # 子工作流分组不是自定义展示节点；描述为空是合法的，不能套用普通节点
    # 的“自定义展示必须包含描述”校验。描述存在时仍保留原有展示注释往返。
    metadata_comment = (
        _node_metadata_comment(node=node, action=action)
        if isinstance(node.get("description"), str) and node["description"].strip()
        else None
    )
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}{_node_anchor(node_uuid, node)}")
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
    explicit_control_dependencies: Mapping[str, tuple[str, ...]],
    inline_expanded_composites: bool = False,
    result_name_by_uuid: dict[str, str] | None = None,
) -> None:
    """按给定展示缩进追加一个物料来源或普通动作节点源码。

    参数说明：``node`` 是待生成节点；``indent_level`` 控制顶层/分组/并行分支
    缩进；``lines``、``source_map``、``result_names`` 是原位收集器；其余映射分别
    提供物料来源调用、候选边、节点/目录索引和设备选择器名；内联已展开组合时
    ``inline_expanded_composites`` 把嵌套 ``workflow`` 写成子树源码。返回：无。
    异常：重复结果名、连接点身份或参数来源不可信时抛出 ``AuthoringGraphError``。
    """

    node_uuid = str(node["uuid"])
    action = catalog_by_node[node_uuid]
    if (
        inline_expanded_composites
        and _is_published_workflow(action)
        and _expanded_composite_children(node_uuid, node_by_uuid)
    ):
        _append_expanded_composite_source(
            node=node,
            indent_level=indent_level,
            lines=lines,
            source_map=source_map,
            result_names=result_names,
            material_sources=material_sources,
            incoming=incoming,
            all_nodes=node_by_uuid,
            all_catalog=catalog_by_node,
            device_symbols=device_symbols,
            explicit_control_dependencies=explicit_control_dependencies,
            inline_expanded_composites=inline_expanded_composites,
            result_name_by_uuid=result_name_by_uuid,
        )
        return
    if _is_group(action) or _is_condition(action) or _is_repeat_until(action):
        raise AuthoringGraphError("candidate_invalid", "控制或展示节点不能作为动作生成")
    indent = "    " * indent_level
    start_line = len(lines) + 1
    allocated_names = result_name_by_uuid if result_name_by_uuid is not None else {}
    result_name = _allocate_result_name(
        node,
        result_names=result_names,
        result_name_by_uuid=allocated_names,
        uniquify=inline_expanded_composites,
    )
    metadata_comment = _node_metadata_comment(node=node, action=action)
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}{_node_anchor(node_uuid, node)}")
    if node_uuid in material_sources:
        rendered_framework = material_sources[node_uuid]
        if isinstance(rendered_framework, RenderedMaterialIO):
            material_expression = None
            material_target = next(
                (
                    handle
                    for handle in action.handles
                    if handle.get("io_type") == "target"
                    and handle.get("handle_key") == "material"
                ),
                None,
            )
            if material_target is not None:
                source = incoming.get(
                    (node_uuid, str(material_target.get("uuid") or ""))
                )
                if source is not None:
                    source_node_uuid, source_handle_uuid = source
                    source_node = node_by_uuid[source_node_uuid]
                    source_action = catalog_by_node[source_node_uuid]
                    source_handle = next(
                        (
                            handle
                            for handle in source_action.handles
                            if str(handle.get("uuid") or "") == source_handle_uuid
                        ),
                        None,
                    )
                    if source_handle is None:
                        raise AuthoringGraphError(
                            "candidate_invalid",
                            "material_io 上游物料连接点不在目录中",
                        )
                    source_name = _python_result_name(
                        source_node, allocated_names
                    )
                    material_expression = (
                        source_name
                        if (
                            _is_material_source(source_action)
                            or _is_material_io(source_action)
                        )
                        and source_handle.get("handle_key") == "material"
                        else f"{source_name}.{source_handle['handle_key']}"
                    )
            try:
                call = rendered_framework.call(material_expression)
            except MaterialIOAuthoringError as error:
                raise AuthoringGraphError(error.code, error.message) from error
        else:
            mount_expression = None
            if rendered_framework.requires_mount_edge:
                mount_target = next(
                    (
                        handle
                        for handle in action.handles
                        if handle.get("io_type") == "target"
                        and handle.get("handle_key") == "mount"
                    ),
                    None,
                )
                source = (
                    incoming.get(
                        (node_uuid, str(mount_target.get("uuid") or ""))
                    )
                    if mount_target is not None
                    else None
                )
                if source is not None:
                    source_node_uuid, source_handle_uuid = source
                    source_node = node_by_uuid[source_node_uuid]
                    source_action = catalog_by_node[source_node_uuid]
                    source_handle = next(
                        (
                            handle
                            for handle in source_action.handles
                            if str(handle.get("uuid") or "")
                            == source_handle_uuid
                        ),
                        None,
                    )
                    if source_handle is None:
                        raise AuthoringGraphError(
                            "candidate_invalid",
                            "material_source mount 上游连接点不在目录中",
                        )
                    source_name = _python_result_name(
                        source_node, allocated_names
                    )
                    mount_expression = (
                        source_name
                        if (
                            _is_material_source(source_action)
                            or _is_material_io(source_action)
                        )
                        and source_handle.get("handle_key") == "material"
                        else f"{source_name}.{source_handle['handle_key']}"
                    )
            try:
                call = rendered_framework.with_mount(mount_expression)
            except MaterialAuthoringError as error:
                raise AuthoringGraphError(error.code, error.message) from error
    else:
        arguments = _render_action_arguments(
            node=node,
            action=action,
            incoming=incoming,
            node_by_uuid=node_by_uuid,
            catalog_by_node=catalog_by_node,
            result_name_by_uuid=allocated_names,
        )
        if node_uuid in explicit_control_dependencies:
            names = [
                _python_result_name(node_by_uuid[source], allocated_names)
                for source in explicit_control_dependencies[node_uuid]
            ]
            arguments.append(f"depends_on=[{', '.join(names)}]")
        if _is_published_workflow(action):
            class_identity = action.template.get("class")
            if not isinstance(class_identity, str) or class_identity.count(":") != 1:
                raise AuthoringGraphError(
                    "composite_catalog_mismatch",
                    "已发布工作流模板缺少绝对导入身份",
                )
            callee = class_identity.rsplit(":", 1)[1]
        else:
            selector = device_symbols[_selector_key(node, action)]
            method_name = str(node.get("action_name") or action.template["name"]).removeprefix("auto-")
            callee = f"{selector}.{method_name}"
        call = f"{callee}({', '.join(arguments)})"
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


def _inlined_composite_uuids(
    nodes: Mapping[str, Mapping[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> set[str]:
    """返回已展开、应内联而不是保留 ``workflow()`` 调用的组合节点 UUID。"""

    return {
        node_uuid
        for node_uuid, action in catalog_by_node.items()
        if _is_published_workflow(action)
        and _expanded_composite_children(node_uuid, nodes)
    }


def _node_composite(node: Mapping[str, Any]) -> Mapping[str, Any]:
    """读取节点上的组合边界映射；没有合法对象时返回空映射。"""

    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
    return composite if isinstance(composite, Mapping) else {}


def _catalog_handle(
    action: AuthoringCatalogAction | None,
    handle_uuid: str,
) -> Mapping[str, Any] | None:
    """按 UUID 取出目录连接点；缺失时返回 ``None``。"""

    if action is None:
        return None
    return next(
        (item for item in action.handles if str(item["uuid"]) == handle_uuid),
        None,
    )


def _is_data_parameter_handle(
    action: AuthoringCatalogAction | None,
    handle_uuid: str,
) -> bool:
    """判断连接点是否会进入动作参数渲染，而不是 ready 等结构依赖。"""

    handle = _catalog_handle(action, handle_uuid)
    if handle is None:
        return False
    return (
        handle.get("handle_key") != "ready"
        and str(handle.get("data_source") or "executor").lower()
        in {"executor", "goal"}
    )


def _remap_inlined_source(
    node: Mapping[str, Any],
    handle_uuid: str,
) -> tuple[str, str] | None:
    """把内联组合调用的输出连接点投影到唯一内部来源节点。"""

    mapping = _node_composite(node).get("source_mappings")
    item = mapping.get(handle_uuid) if isinstance(mapping, Mapping) else None
    if not isinstance(item, Mapping) or item.get("kind") != "node_output":
        return None
    child_uuid = item.get("workflow_node_uuid")
    child_handle = item.get("source_handle_uuid")
    if not isinstance(child_uuid, str) or not isinstance(child_handle, str):
        return None
    return (child_uuid, child_handle)


def _remap_inlined_targets(
    node: Mapping[str, Any],
    handle_uuid: str,
) -> list[tuple[str, str]]:
    """把内联组合调用的输入连接点投影到内部目标节点。"""

    mapping = _node_composite(node).get("target_mappings")
    raw = mapping.get(handle_uuid) if isinstance(mapping, Mapping) else None
    if not isinstance(raw, list):
        return []
    targets: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        child_uuid = item.get("workflow_node_uuid")
        child_handle = item.get("target_handle_uuid")
        if isinstance(child_uuid, str) and isinstance(child_handle, str):
            targets.append((child_uuid, child_handle))
    return targets


def _rewrite_inlined_composite_edges(
    edges: list[Any],
    *,
    inlined_invocations: set[str],
    all_nodes: Mapping[str, Mapping[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> list[dict[str, Any]]:
    """把穿过已展开组合边界的数据边改写到内部节点，结构依赖边则丢弃。

    参数：``edges`` 是导入图中的候选边，``inlined_invocations`` 是将被写成内联
    源码的组合调用，其余映射提供节点和目录。返回：端点已投影到内部动作的边。
    异常：数据边无法唯一投影时抛出 ``AuthoringGraphError``。
    """

    rewritten: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        source_uuid = str(edge.get("source_node_uuid"))
        source_handle = str(edge.get("source_handle_uuid"))
        target_uuid = str(edge.get("target_node_uuid"))
        target_handle = str(edge.get("target_handle_uuid"))
        if source_uuid in inlined_invocations:
            remapped = _remap_inlined_source(all_nodes[source_uuid], source_handle)
            if remapped is None:
                if _is_data_parameter_handle(
                    catalog_by_node.get(source_uuid),
                    source_handle,
                ):
                    raise AuthoringGraphError(
                        "candidate_invalid",
                        "内联组合输出无法唯一投影到内部节点",
                    )
                continue
            source_uuid, source_handle = remapped
        if target_uuid in inlined_invocations:
            targets = _remap_inlined_targets(all_nodes[target_uuid], target_handle)
            if not targets:
                if _is_data_parameter_handle(
                    catalog_by_node.get(target_uuid),
                    target_handle,
                ):
                    raise AuthoringGraphError(
                        "candidate_invalid",
                        "内联组合入参无法投影到内部节点",
                    )
                continue
            for child_uuid, child_handle in targets:
                rewritten.append(
                    {
                        **dict(edge),
                        "source_node_uuid": source_uuid,
                        "source_handle_uuid": source_handle,
                        "target_node_uuid": child_uuid,
                        "target_handle_uuid": child_handle,
                    }
                )
            continue
        rewritten.append(
            {
                **dict(edge),
                "source_node_uuid": source_uuid,
                "source_handle_uuid": source_handle,
            }
        )
    return rewritten


def _allocate_result_name(
    node: Mapping[str, Any],
    *,
    result_names: set[str],
    result_name_by_uuid: dict[str, str],
    uniquify: bool,
) -> str:
    """分配节点 Python 结果变量；内联组合时允许为冲突名追加稳定后缀。"""

    node_uuid = str(node["uuid"])
    existing = result_name_by_uuid.get(node_uuid)
    if existing is not None:
        return existing
    name = _node_result_name(node)
    if name in result_names:
        if not uniquify:
            raise AuthoringGraphError("candidate_invalid", "节点作者结果变量重复")
        suffix = node_uuid.replace("-", "")[:8]
        candidate = _safe_identifier(f"{name}_{suffix}", fallback="result")
        extra = 2
        while candidate in result_names:
            candidate = _safe_identifier(
                f"{name}_{suffix}_{extra}",
                fallback="result",
            )
            extra += 1
        name = candidate
    result_names.add(name)
    result_name_by_uuid[node_uuid] = name
    return name


def _python_result_name(
    node: Mapping[str, Any],
    result_name_by_uuid: Mapping[str, str] | None,
) -> str:
    """读取已分配或原始的节点结果变量名。"""

    node_uuid = str(node["uuid"])
    if result_name_by_uuid and node_uuid in result_name_by_uuid:
        return result_name_by_uuid[node_uuid]
    return _node_result_name(node)


def _binding_var_names(
    params: Mapping[str, Any] | None,
    *,
    node_by_uuid: Mapping[str, dict[str, Any]],
    result_name_by_uuid: Mapping[str, str] | None,
) -> dict[str, str]:
    """把控制区域 bindings 的逻辑名映射到实际 Python 结果变量。"""

    if not isinstance(params, Mapping):
        return {}
    bindings = params.get("bindings")
    if not isinstance(bindings, Mapping):
        return {}
    names: dict[str, str] = {}
    for key, binding in bindings.items():
        if not isinstance(binding, Mapping) or binding.get("kind") != "node_result":
            continue
        source_uuid = str(binding.get("node_uuid") or "")
        source = node_by_uuid.get(source_uuid)
        if source is None:
            continue
        names[str(key)] = _python_result_name(source, result_name_by_uuid)
    return names


def _expanded_composite_children(
    parent_uuid: str,
    nodes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """返回已展开组合调用的直接子节点，并按作者源码顺序排列。

    参数：``parent_uuid`` 是组合调用节点身份，``nodes`` 是完整候选节点索引。
    返回：直接子节点列表；没有展开子图时为空。异常：子节点缺少合法源码顺序时
    抛出 ``AuthoringGraphError``。
    """

    children = [
        dict(node)
        for node in nodes.values()
        if node.get("parent_uuid") == parent_uuid
    ]
    return sorted(children, key=_composite_child_source_order)


def _composite_child_source_order(node: Mapping[str, Any]) -> int:
    """读取组合子树内节点的作者源码顺序。"""

    unilab = (node.get("meta_data") or {}).get("unilab", {})
    value = (
        unilab.get("authoring_source_order") if isinstance(unilab, Mapping) else None
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthoringGraphError(
            "candidate_invalid",
            "已展开组合子节点缺少作者源码顺序",
        )
    return value


def _append_expanded_composite_source(
    *,
    node: Mapping[str, Any],
    indent_level: int,
    lines: list[str],
    source_map: list[dict[str, Any]],
    result_names: set[str],
    material_sources: Mapping[str, RenderedMaterialSource],
    incoming: Mapping[tuple[str, str], tuple[str, str]],
    all_nodes: Mapping[str, dict[str, Any]],
    all_catalog: Mapping[str, AuthoringCatalogAction],
    device_symbols: Mapping[tuple[str, str | None], str],
    explicit_control_dependencies: Mapping[str, tuple[str, ...]],
    inline_expanded_composites: bool = False,
    result_name_by_uuid: dict[str, str] | None = None,
) -> None:
    """把已展开组合调用写成内部条件、循环或动作源码，而不是 ``workflow()``。

    参数：``node`` 是带完整子图的组合调用；其余收集器与目录映射与普通节点生成
    相同。返回：无。异常：子树缺少节点、顺序或控制结构非法时抛出
    ``AuthoringGraphError``。
    """

    children = _expanded_composite_children(str(node["uuid"]), all_nodes)
    if not children:
        raise AuthoringGraphError(
            "candidate_invalid",
            "已展开的组合调用缺少可内联的子节点",
        )
    rendered_parallel_scopes: set[str] = set()
    for child in children:
        child_uuid = str(child["uuid"])
        child_action = all_catalog[child_uuid]
        if _is_published_workflow(child_action) and _expanded_composite_children(
            child_uuid, all_nodes
        ):
            _append_expanded_composite_source(
                node=child,
                indent_level=indent_level,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                all_nodes=all_nodes,
                all_catalog=all_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            continue
        if _is_condition(child_action):
            _append_condition_source(
                node=child,
                indent_level=indent_level,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=all_nodes,
                catalog_by_node=all_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            continue
        if _is_repeat_until(child_action):
            _append_repeat_until_source(
                node=child,
                indent_level=indent_level,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=all_nodes,
                catalog_by_node=all_catalog,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
            continue
        if _is_group(child_action):
            scope = _parallel_scope(child)
            if scope is not None:
                if scope in rendered_parallel_scopes:
                    continue
                rendered_parallel_scopes.add(scope)
                indent = "    " * indent_level
                lines.append(f"{indent}with parallel():")
                scope_groups = sorted(
                    (
                        candidate
                        for candidate in all_nodes.values()
                        if candidate.get("parent_uuid") == node.get("uuid")
                        and _is_group(all_catalog[str(candidate["uuid"])])
                        and _parallel_scope(candidate) == scope
                    ),
                    key=_parallel_order,
                )
                for group_node in scope_groups:
                    _append_group_source(
                        node=group_node,
                        indent_level=indent_level + 1,
                        lines=lines,
                        source_map=source_map,
                        action=all_catalog[str(group_node["uuid"])],
                    )
                    for grouped_child in all_nodes.values():
                        if grouped_child.get("parent_uuid") != str(group_node["uuid"]):
                            continue
                        _append_action_source(
                            node=grouped_child,
                            indent_level=indent_level + 2,
                            lines=lines,
                            source_map=source_map,
                            result_names=result_names,
                            material_sources=material_sources,
                            incoming=incoming,
                            node_by_uuid=all_nodes,
                            catalog_by_node=all_catalog,
                            device_symbols=device_symbols,
                            explicit_control_dependencies=explicit_control_dependencies,
                            inline_expanded_composites=inline_expanded_composites,
                            result_name_by_uuid=result_name_by_uuid,
                        )
                continue
            _append_group_source(
                node=child,
                indent_level=indent_level,
                lines=lines,
                source_map=source_map,
                action=child_action,
            )
            for grouped_child in all_nodes.values():
                if grouped_child.get("parent_uuid") != child_uuid:
                    continue
                _append_action_source(
                    node=grouped_child,
                    indent_level=indent_level + 1,
                    lines=lines,
                    source_map=source_map,
                    result_names=result_names,
                    material_sources=material_sources,
                    incoming=incoming,
                    node_by_uuid=all_nodes,
                    catalog_by_node=all_catalog,
                    device_symbols=device_symbols,
                    explicit_control_dependencies=explicit_control_dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )
            continue
        _append_action_source(
            node=child,
            indent_level=indent_level,
            lines=lines,
            source_map=source_map,
            result_names=result_names,
            material_sources=material_sources,
            incoming=incoming,
            node_by_uuid=all_nodes,
            catalog_by_node=all_catalog,
            device_symbols=device_symbols,
            explicit_control_dependencies=explicit_control_dependencies,
            inline_expanded_composites=inline_expanded_composites,
            result_name_by_uuid=result_name_by_uuid,
        )


def _append_condition_source(
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
    explicit_control_dependencies: Mapping[str, tuple[str, ...]],
    inline_expanded_composites: bool = False,
    result_name_by_uuid: dict[str, str] | None = None,
) -> None:
    """把条件区域及其直接动作分支确定性写回原生 Python。"""

    node_uuid = str(node["uuid"])
    action = catalog_by_node[node_uuid]
    indent = "    " * indent_level
    start_line = len(lines) + 1
    metadata_comment = _node_metadata_comment(node=node, action=action)
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}{_node_anchor(node_uuid, node)}")
    params = node.get("param")
    branches = params.get("branches") if isinstance(params, Mapping) else None
    if not isinstance(branches, list) or not branches:
        raise AuthoringGraphError("candidate_invalid", "条件区域缺少有序分支")
    source_map.append(
        CandidateSourceMapEntry(
            workflow_node_uuid=node_uuid,
            start_line=start_line,
            start_column=len(indent) + 1,
            end_line=len(lines) + 1,
            end_column=len(indent) + 1,
        ).model_dump()
    )
    for index, branch in enumerate(branches):
        if not isinstance(branch, Mapping):
            raise AuthoringGraphError("candidate_invalid", "条件分支必须是对象")
        label = branch.get("label")
        condition = branch.get("condition")
        var_names = _binding_var_names(
            params if isinstance(params, Mapping) else None,
            node_by_uuid=node_by_uuid,
            result_name_by_uuid=result_name_by_uuid,
        )
        if index == 0 and label == "if" and isinstance(condition, Mapping):
            header = (
                "if "
                f"{_render_condition_expression(condition, node_by_uuid=node_by_uuid, var_names=var_names)}:"
            )
        elif label == f"elif{index - 1}" and isinstance(condition, Mapping):
            header = (
                "elif "
                f"{_render_condition_expression(condition, node_by_uuid=node_by_uuid, var_names=var_names)}:"
            )
        elif index == len(branches) - 1 and label == "else" and condition is None:
            header = "else:"
        else:
            raise AuthoringGraphError("candidate_invalid", "条件分支顺序或表达式无效")
        lines.append(f"{indent}{header}")
        node_uuids = branch.get("node_uuids")
        if not isinstance(node_uuids, list) or not node_uuids:
            raise AuthoringGraphError("candidate_invalid", "条件分支不能为空")
        for child_uuid_value in node_uuids:
            child_uuid = str(child_uuid_value)
            child = node_by_uuid.get(child_uuid)
            child_action = catalog_by_node.get(child_uuid)
            if child is not None and child.get("parent_uuid") != node_uuid:
                # ``node_uuids`` 包含整个分支子树；这里只渲染直接子节点，嵌套
                # 条件由递归调用负责写出其后代，避免重复生成。
                continue
            if child is None or child_action is None or _is_group(child_action):
                raise AuthoringGraphError(
                    "candidate_invalid", "条件分支只能包含动作或嵌套条件节点"
                )
            if _is_condition(child_action):
                _append_condition_source(
                    node=child,
                    indent_level=indent_level + 1,
                    lines=lines,
                    source_map=source_map,
                    result_names=result_names,
                    material_sources=material_sources,
                    incoming=incoming,
                    node_by_uuid=node_by_uuid,
                    catalog_by_node=catalog_by_node,
                    device_symbols=device_symbols,
                    explicit_control_dependencies=explicit_control_dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )
            elif _is_repeat_until(child_action):
                _append_repeat_until_source(
                    node=child,
                    indent_level=indent_level + 1,
                    lines=lines,
                    source_map=source_map,
                    result_names=result_names,
                    material_sources=material_sources,
                    incoming=incoming,
                    node_by_uuid=node_by_uuid,
                    catalog_by_node=catalog_by_node,
                    device_symbols=device_symbols,
                    explicit_control_dependencies=explicit_control_dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )
            else:
                _append_action_source(
                    node=child,
                    indent_level=indent_level + 1,
                    lines=lines,
                    source_map=source_map,
                    result_names=result_names,
                    material_sources=material_sources,
                    incoming=incoming,
                    node_by_uuid=node_by_uuid,
                    catalog_by_node=catalog_by_node,
                    device_symbols=device_symbols,
                    explicit_control_dependencies=explicit_control_dependencies,
                    inline_expanded_composites=inline_expanded_composites,
                    result_name_by_uuid=result_name_by_uuid,
                )


def _append_repeat_until_source(
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
    explicit_control_dependencies: Mapping[str, tuple[str, ...]],
    inline_expanded_composites: bool = False,
    result_name_by_uuid: dict[str, str] | None = None,
) -> None:
    """把冻结 RepeatUntil 区域确定性写回 carry/next Python 语法。"""

    node_uuid = str(node["uuid"])
    action = catalog_by_node[node_uuid]
    params = node.get("param")
    if not isinstance(params, Mapping):
        raise AuthoringGraphError("candidate_invalid", "RepeatUntil 参数必须是对象")
    loop_variable = params.get("loop_variable")
    maximum = params.get("max_iterations")
    initial_carry = params.get("initial_carry")
    next_carry = params.get("next_carry")
    until_expression = params.get("until")
    member_values = params.get("node_uuids")
    if (
        not isinstance(loop_variable, str)
        or _safe_identifier(loop_variable, fallback="invalid") != loop_variable
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < 1
        or not isinstance(initial_carry, Mapping)
        or not isinstance(next_carry, Mapping)
        or set(initial_carry) != set(next_carry)
        or not isinstance(until_expression, Mapping)
        or not isinstance(member_values, list)
        or not member_values
    ):
        raise AuthoringGraphError("candidate_invalid", "RepeatUntil 冻结合同无效")
    indent = "    " * indent_level
    child_indent = "    " * (indent_level + 1)
    start_line = len(lines) + 1
    metadata_comment = _node_metadata_comment(node=node, action=action)
    if metadata_comment is not None:
        lines.append(f"{indent}{metadata_comment}")
    lines.append(f"{indent}{_node_anchor(node_uuid, node)}")
    lines.append(f"{indent}with repeat_until(")
    lines.append(f"{indent}    max_iterations={maximum},")
    carry_parts = [
        f"{json.dumps(str(name), ensure_ascii=False)}: "
        f"{_render_repeat_binding(binding, node_by_uuid=node_by_uuid, result_name_by_uuid=result_name_by_uuid)}"
        for name, binding in initial_carry.items()
    ]
    lines.append(f"{indent}    carry={{{', '.join(carry_parts)}}},")
    lines.append(f"{indent}) as {loop_variable}:")
    source_map.append(
        CandidateSourceMapEntry(
            workflow_node_uuid=node_uuid,
            start_line=start_line,
            start_column=len(indent) + 1,
            end_line=len(lines),
            end_column=utf16_length(lines[-1]) + 1,
        ).model_dump()
    )
    rendered_parallel_scopes: set[str] = set()
    for child_uuid_value in member_values:
        child_uuid = str(child_uuid_value)
        child = node_by_uuid.get(child_uuid)
        child_action = catalog_by_node.get(child_uuid)
        if child is not None and child.get("parent_uuid") != node_uuid:
            continue
        if child is None or child_action is None:
            raise AuthoringGraphError(
                "candidate_invalid", "RepeatUntil 只能包含动作或嵌套控制区域"
            )
        if _is_group(child_action):
            scope = _parallel_scope(child)
            if scope is None or scope in rendered_parallel_scopes:
                if scope is not None:
                    continue
                group_nodes = [child]
                group_indent = indent_level + 1
                action_indent = indent_level + 2
            else:
                rendered_parallel_scopes.add(scope)
                lines.append(f"{child_indent}with parallel():")
                group_nodes = sorted(
                    (
                        candidate
                        for candidate in node_by_uuid.values()
                        if candidate.get("parent_uuid") == node_uuid
                        and _parallel_scope(candidate) == scope
                    ),
                    key=_parallel_order,
                )
                group_indent = indent_level + 2
                action_indent = indent_level + 3
            for group_node in group_nodes:
                group_uuid = str(group_node["uuid"])
                _append_group_source(
                    node=group_node,
                    indent_level=group_indent,
                    lines=lines,
                    source_map=source_map,
                    action=catalog_by_node[group_uuid],
                )
                for grouped_child in node_by_uuid.values():
                    if grouped_child.get("parent_uuid") != group_uuid:
                        continue
                    grouped_action = catalog_by_node.get(str(grouped_child["uuid"]))
                    if grouped_action is None or (
                        _is_group(grouped_action)
                        or _is_condition(grouped_action)
                        or _is_repeat_until(grouped_action)
                    ):
                        raise AuthoringGraphError(
                            "candidate_invalid",
                            "并行分组内只能包含动作节点",
                        )
                    _append_action_source(
                        node=grouped_child,
                        indent_level=action_indent,
                        lines=lines,
                        source_map=source_map,
                        result_names=result_names,
                        material_sources=material_sources,
                        incoming=incoming,
                        node_by_uuid=node_by_uuid,
                        catalog_by_node=catalog_by_node,
                        device_symbols=device_symbols,
                        explicit_control_dependencies=explicit_control_dependencies,
                        inline_expanded_composites=inline_expanded_composites,
                        result_name_by_uuid=result_name_by_uuid,
                    )
        elif _is_condition(child_action):
            _append_condition_source(
                node=child,
                indent_level=indent_level + 1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
        elif _is_repeat_until(child_action):
            _append_repeat_until_source(
                node=child,
                indent_level=indent_level + 1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
        else:
            _append_action_source(
                node=child,
                indent_level=indent_level + 1,
                lines=lines,
                source_map=source_map,
                result_names=result_names,
                material_sources=material_sources,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
                device_symbols=device_symbols,
                explicit_control_dependencies=explicit_control_dependencies,
                inline_expanded_composites=inline_expanded_composites,
                result_name_by_uuid=result_name_by_uuid,
            )
    next_parts = [
        f"{name}={_render_repeat_binding(binding, node_by_uuid=node_by_uuid, result_name_by_uuid=result_name_by_uuid)}"
        for name, binding in next_carry.items()
    ]
    lines.append(f"{child_indent}{loop_variable}.next({', '.join(next_parts)})")
    lines.append(
        f"{child_indent}until("
        f"{_render_condition_expression(until_expression, node_by_uuid=node_by_uuid, var_names=_binding_var_names(params, node_by_uuid=node_by_uuid, result_name_by_uuid=result_name_by_uuid))})"
    )


def _render_repeat_binding(
    value: Any,
    *,
    node_by_uuid: Mapping[str, dict[str, Any]],
    result_name_by_uuid: Mapping[str, str] | None = None,
) -> str:
    """渲染冻结的初始或下一轮 carry 来源。"""

    if not isinstance(value, Mapping):
        raise AuthoringGraphError("candidate_invalid", "循环 carry 来源必须是对象")
    kind = value.get("kind")
    if kind == "literal" and set(value) == {"kind", "value"}:
        return repr(_stable_python_json(value["value"]))
    if kind == "workflow_input" and isinstance(value.get("parameter"), str):
        return _safe_identifier(str(value["parameter"]), fallback="invalid")
    if kind == "node_result" and isinstance(value.get("node_uuid"), str):
        source = node_by_uuid.get(str(value["node_uuid"]))
        path = value.get("result_path")
        if source is None or not isinstance(path, list) or not path:
            raise AuthoringGraphError("candidate_invalid", "循环节点结果来源无效")
        expression = _python_result_name(source, result_name_by_uuid)
        for part in path:
            expression += f".{_safe_identifier(str(part), fallback='invalid')}"
        return expression
    if (
        kind == "loop_carry"
        and isinstance(value.get("control_region_uuid"), str)
        and isinstance(value.get("key"), str)
    ):
        region = node_by_uuid.get(str(value["control_region_uuid"]))
        region_params = region.get("param") if isinstance(region, Mapping) else None
        variable = (
            region_params.get("loop_variable")
            if isinstance(region_params, Mapping)
            else None
        )
        if not isinstance(variable, str):
            raise AuthoringGraphError("candidate_invalid", "循环 carry 区域身份无效")
        return f"{variable}.carry[{json.dumps(str(value['key']), ensure_ascii=False)}]"
    raise AuthoringGraphError("candidate_invalid", "循环 carry 来源不受支持")


def _render_condition_expression(
    value: Mapping[str, Any],
    *,
    node_by_uuid: Mapping[str, dict[str, Any]] | None = None,
    var_names: Mapping[str, str] | None = None,
) -> str:
    """把封闭结构化表达式无损渲染为 Python 条件表达式。"""

    if set(value) == {"lit"}:
        return repr(value["lit"])
    if set(value) == {"var"}:
        raw = str(value["var"])
        if var_names and raw in var_names:
            return var_names[raw]
        return _safe_identifier(raw, fallback="invalid")
    if set(value) == {"carry", "control_region_uuid"} and node_by_uuid is not None:
        region = node_by_uuid.get(str(value["control_region_uuid"]))
        params = region.get("param") if isinstance(region, Mapping) else None
        variable = params.get("loop_variable") if isinstance(params, Mapping) else None
        if not isinstance(variable, str):
            raise AuthoringGraphError("candidate_invalid", "循环条件 carry 来源无效")
        return (
            f"{variable}.carry[{json.dumps(str(value['carry']), ensure_ascii=False)}]"
        )
    if set(value) == {"field", "name"} and isinstance(value["field"], Mapping):
        name = _safe_identifier(str(value["name"]), fallback="invalid")
        return (
            f"{_render_condition_expression(value['field'], node_by_uuid=node_by_uuid, var_names=var_names)}"
            f".{name}"
        )
    if (
        set(value) == {"index", "key"}
        and isinstance(value["index"], Mapping)
        and isinstance(value["key"], Mapping)
    ):
        return (
            f"{_render_condition_expression(value['index'], node_by_uuid=node_by_uuid, var_names=var_names)}"
            f"[{_render_condition_expression(value['key'], node_by_uuid=node_by_uuid, var_names=var_names)}]"
        )
    if (
        set(value) == {"binop", "left", "right"}
        and isinstance(value["left"], Mapping)
        and isinstance(value["right"], Mapping)
    ):
        operator_name = str(value["binop"])
        if operator_name not in {
            "+",
            "-",
            "*",
            "/",
            "//",
            "%",
            "==",
            "!=",
            ">",
            ">=",
            "<",
            "<=",
            "and",
            "or",
        }:
            raise AuthoringGraphError("candidate_invalid", "条件二元运算符无效")
        return (
            f"({_render_condition_expression(value['left'], node_by_uuid=node_by_uuid, var_names=var_names)} "
            f"{operator_name} "
            f"{_render_condition_expression(value['right'], node_by_uuid=node_by_uuid, var_names=var_names)})"
        )
    if set(value) == {"unop", "operand"} and isinstance(value["operand"], Mapping):
        operator_name = str(value["unop"])
        if operator_name == "not":
            return (
                "not "
                f"{_render_condition_expression(value['operand'], node_by_uuid=node_by_uuid, var_names=var_names)}"
            )
        if operator_name == "neg":
            return f"-{_render_condition_expression(value['operand'], node_by_uuid=node_by_uuid, var_names=var_names)}"
        raise AuthoringGraphError("candidate_invalid", "条件一元运算符无效")
    if set(value) == {"call", "args"} and isinstance(value["args"], list):
        name = str(value["call"])
        if name not in {"len", "min", "max", "abs", "round", "contains", "get"}:
            raise AuthoringGraphError("candidate_invalid", "条件函数不在白名单中")
        if any(not isinstance(argument, Mapping) for argument in value["args"]):
            raise AuthoringGraphError("candidate_invalid", "条件函数参数无效")
        arguments = ", ".join(
            _render_condition_expression(
                argument,
                node_by_uuid=node_by_uuid,
                var_names=var_names,
            )
            for argument in value["args"]
        )
        return f"{name}({arguments})"
    raise AuthoringGraphError("candidate_invalid", "条件表达式结构无效")


class _NoDefault:
    """区分无默认值与显式 ``None`` 的内部哨兵类型。"""


_NO_DEFAULT = _NoDefault()


def _node_anchor(node_uuid: str, node: Mapping[str, Any]) -> str:
    """在稳定 UUID 锚点保留禁用和人工确认配置，避免冷编译退化为普通动作。"""

    suffix = " disabled=true" if node.get("disabled") is True else ""
    if str(node.get("type", "")).lower() == "manual_confirm":
        try:
            config = normalize_manual_confirmation_config(node.get("manual_confirmation"))
        except StoreConflict as error:
            raise AuthoringGraphError("invalid_manual_confirmation", str(error)) from error
        suffix += f" manual_confirmation_timeout_seconds={config['timeout_seconds']}"
    return f"# unilab:node_uuid={node_uuid}{suffix}"


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
    source_positions: Mapping[str, int] | None = None,
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
    def order_key(identity: str) -> tuple[int, str]:
        """无依赖节点优先保留旧源码顺序，UUID 作为确定性回退。"""

        return (source_positions or {}).get(identity, 0), identity

    ready = sorted(
        (identity for identity, count in indegree.items() if count == 0),
        key=order_key,
    )
    ordered: list[dict[str, Any]] = []
    while ready:
        identity = ready.pop(0)
        ordered.append(nodes[identity])
        for target in sorted(outgoing[identity]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=order_key)
    if len(ordered) != len(nodes):
        raise AuthoringGraphError("candidate_invalid", "候选图包含依赖环")
    return ordered


def _authoring_ordered_nodes(
    nodes: Mapping[str, dict[str, Any]],
    edges: list[Any],
    *,
    preserve_source_order: bool = False,
) -> list[dict[str, Any]]:
    """按执行依赖排列平面图，结构化图仍保留作者源码边界。

    参数说明：``nodes`` 是候选节点索引，``edges`` 是完整执行边数组。返回：当
    每个节点都有唯一非负 ``authoring_source_order`` 时以其作为平面拓扑排序
    的稳定同级顺序，否则对不含展示分组的旧图返回拓扑顺序。异常：结构化图
    缺少顺序、顺序重复，或结构化源码顺序逆转依赖时抛出错误。
    """

    topological = _topological_nodes(nodes, edges)
    # ``source_positions`` 是节点 UUID 到作者源码零基顺序的可信映射。
    source_positions: dict[str, int] = {}
    missing_source_order = False
    for node_uuid, node in nodes.items():
        unilab = (node.get("meta_data") or {}).get("unilab", {})
        value = (
            unilab.get("authoring_source_order")
            if isinstance(unilab, Mapping)
            else None
        )
        if value is None:
            missing_source_order = True
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthoringGraphError(
                "candidate_invalid", "作者源码节点顺序必须是非负整数"
            )
        if value in source_positions.values():
            raise AuthoringGraphError("candidate_invalid", "作者源码节点顺序不能重复")
        source_positions[node_uuid] = value
    has_structured_node = any(
        str(node.get("type")) in {"group", "condition", "repeat_until"}
        or str(node.get("node_type")) in {"group", "condition", "repeat_until"}
        for node in nodes.values()
    )
    if missing_source_order:
        if has_structured_node:
            raise AuthoringGraphError(
                "candidate_invalid", "结构化候选图缺少作者源码顺序"
            )
        return topological

    if not has_structured_node and not preserve_source_order:
        # 画布编辑的边拥有执行顺序；旧源码位置只是同级排序提示。结构化图
        # 不走此路径，避免跨分组或控制区域移动节点而改变原有词法语义。
        return _topological_nodes(nodes, edges, source_positions)

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
            raise AuthoringGraphError(
                "candidate_invalid", "作者源码节点顺序逆转了执行依赖"
            )
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
        raise AuthoringGraphError(
            "candidate_invalid", "并行结构作用域身份无效"
        ) from error


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


def _is_condition(action: AuthoringCatalogAction) -> bool:
    """判断目录动作是否为框架拥有的条件控制区域模板。"""

    template = action.template
    return (
        template.get("type") == "condition"
        and template.get("node_type") == "condition"
        and template.get("class") == "unilabos.workflow.authoring:condition"
        and template.get("name") == "condition"
    )


def _is_repeat_until(action: AuthoringCatalogAction) -> bool:
    """判断目录动作是否为框架拥有的 RepeatUntil 控制区域模板。"""

    template = action.template
    return (
        template.get("type") == "repeat_until"
        and template.get("node_type") == "repeat_until"
        and template.get("class") == "unilabos.workflow.authoring:repeat_until"
        and template.get("name") == "repeat_until"
    )


def _is_published_workflow(action: AuthoringCatalogAction) -> bool:
    """判断目录动作是否为框架发布的工作流调用模板。

    参数：``action`` 是目录聚合。返回：类型和来源元数据同时满足发布合同时为
    ``True``。异常：无；不完整模板返回 ``False``。
    """

    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    if (
        template.get("type") != "workflow"
        or template.get("node_type") != "workflow"
        or not isinstance(unilab, Mapping)
    ):
        return False
    return isinstance(unilab.get("workflow_source"), Mapping) or isinstance(
        unilab.get("workflow_contract"), Mapping
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
            or _is_material_io(action)
            or _is_group(action)
            or _is_condition(action)
            or _is_repeat_until(action)
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

    参数说明：``descriptor`` 是版本 1 参数描述；单位（如有）会保留在生成的
    ``Field`` 元数据中。返回名称、注解、默认值和所需 typing 名称集合及资源
    模板 import，非法描述抛出 ``AuthoringGraphError``。
    """

    name = descriptor.get("name")
    schema = descriptor.get("schema")
    if not isinstance(name, str) or not isinstance(schema, Mapping):
        raise AuthoringGraphError("candidate_invalid", "工作流输入合同无效")
    annotation, imports, resource_imports = _render_schema(
        dict(schema),
        catalog=catalog,
        unit=descriptor.get("unit"),
    )
    # 展示元数据只属于最外层参数，不得插入数组元素的 Field。
    for field_name in ("title", "description"):
        value = descriptor.get(field_name)
        if isinstance(value, str):
            annotation = _append_outer_field_arguments(annotation, {field_name: repr(value)})
            imports.update({"Annotated", "Field"})
    default = descriptor.get("default", _NO_DEFAULT)
    return name, annotation, default, imports, resource_imports


def _render_schema(
    schema: dict[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
    include_resource_templates: bool = True,
    unit: str | None = None,
) -> tuple[str, set[str], set[tuple[str, str]]]:
    """把规范值 Schema 渲染为静态 Python 注解。

    参数说明：``schema`` 是工作流值 Schema v1，``catalog`` 反解本代资源
    模板源码身份；``include_resource_templates=False`` 用于显式结果记录，因为
    其生产者连接点会在回编译时重新给出更精确保证。返回注解文本、所需 typing
    名称和资源模板 import，unit（如有）作为 Field 元数据保留。当前合同之外的
    Schema 失败关闭。
    """

    template_uuids = (
        _resource_template_allowlist(schema) if include_resource_templates else None
    )
    annotation, imports = _render_schema_base(schema)
    if unit is not None:
        if not isinstance(unit, str) or not unit.strip():
            raise AuthoringGraphError("candidate_invalid", "工作流单位无效")
        unit = unit.strip()
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
        annotation = _append_annotation_metadata(
            annotation,
            f"AllowedResourceTemplates({', '.join(symbols)})",
        )
        imports.add("Annotated")
    if unit is not None:
        annotation = _append_unit_metadata(annotation, unit)
        imports.update({"Annotated", "Field"})
    return annotation, imports, resource_imports


def _append_annotation_metadata(annotation: str, metadata: str) -> str:
    """向最外层 Annotated 追加一个元数据项。

    参数说明：annotation 是内部生成的静态注解文本，metadata 是已验证的元数据
    调用文本。返回：保持单层 Annotated 的注解文本；没有外层注解时新建一层。
    异常：无，调用方负责保证文本来自受信任合同。
    """

    if annotation.startswith("Annotated[") and annotation.endswith("]"):
        return f"{annotation[:-1]}, {metadata}]"
    return f"Annotated[{annotation}, {metadata}]"


def _append_outer_field_arguments(annotation: str, arguments: dict[str, str]) -> str:
    """只编辑最外层 Field，绝不把参数描述符写入嵌套元素。"""
    expression = ast.parse(annotation, mode="eval").body
    values = None
    if isinstance(expression, ast.Subscript) and isinstance(expression.value, ast.Name) and expression.value.id == "Annotated":
        values = list(expression.slice.elts) if isinstance(expression.slice, ast.Tuple) else [expression.slice]
    fields = [value for value in (values or [])[1:] if isinstance(value, ast.Call)
              and isinstance(value.func, ast.Name) and value.func.id == "Field"]
    if len(fields) > 1:
        raise AuthoringGraphError("candidate_invalid", "参数只能有一个外层 Field")
    field = fields[0] if fields else ast.Call(func=ast.Name(id="Field", ctx=ast.Load()), args=[], keywords=[])
    for key, value in arguments.items():
        if any(item.arg == key for item in field.keywords):
            raise AuthoringGraphError("candidate_invalid", f"参数 Field 重复字段：{key}")
        field.keywords.append(ast.keyword(arg=key, value=ast.parse(value, mode="eval").body))
    if not fields:
        if values is None:
            values = [expression]
        values.append(field)
        expression = ast.Subscript(value=ast.Name(id="Annotated", ctx=ast.Load()),
                                   slice=ast.Tuple(elts=values, ctx=ast.Load()), ctx=ast.Load())
    return ast.unparse(ast.fix_missing_locations(expression))


def _append_unit_metadata(annotation: str, unit: str) -> str:
    """把单位写入最外层参数 Field。"""
    return _append_outer_field_arguments(annotation, {"unit": repr(unit)})


def _render_schema_base(schema: dict[str, Any]) -> tuple[str, set[str]]:
    """保留受支持的展示及文件标记；不能无损生成的结构明确拒绝。"""
    if schema.get("type") == "object" and ("properties" in schema or "required" in schema):
        raise AuthoringGraphError("candidate_invalid", "结构化对象输入暂不支持画布回写，请保留原 Python 类型声明")
    annotation, imports = _render_schema_base_value(schema)
    extra = {key: schema[key] for key in sorted(PRESENTATION_KEYS & schema.keys())}
    arguments = {"json_schema_extra": repr(extra)} if extra else {}
    if schema.get("type") == "array" or "enum" in schema:
        for key, field in (("minimum", "ge"), ("maximum", "le"),
                           ("minLength", "min_length"), ("maxLength", "max_length"),
                           ("minItems", "min_length"), ("maxItems", "max_length")):
            if key in schema:
                arguments[field] = repr(schema[key])
    if arguments:
        annotation = _append_outer_field_arguments(annotation, arguments)
        imports.update({"Annotated", "Field"})
    return annotation, imports


def _render_schema_base_value(schema: dict[str, Any]) -> tuple[str, set[str]]:
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
        # Keep generated dictionaries compatible with the AST parser's
        # recursive JSON value contract so generated source compiles back to
        # the same workflow input schema.
        "object": "dict[str, JSONValue]",
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
        imports = {"Annotated"}
        if value_type == "object":
            imports.add("JSONValue")
        return f"Annotated[{annotation}, Field({', '.join(field_arguments)})]", imports
    return annotation, {"JSONValue"} if value_type == "object" else set()


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
            pending.extend(member for member in members if isinstance(member, Mapping))
        child = item.get("items")
        if isinstance(child, Mapping):
            pending.append(child)
    if len(found) > 1:
        raise AuthoringGraphError(
            "candidate_invalid",
            "值 Schema 包含多个资源模板允许集合",
        )
    return found[0] if found else None


def _ready_handle_identities(
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> set[tuple[str, str]]:
    """返回节点上 ready 连接点的（节点, 连接点）身份。"""

    return {
        (node_uuid, str(handle["uuid"]))
        for node_uuid, action in catalog_by_node.items()
        for handle in action.handles
        if str(handle.get("handle_key") or "") == "ready"
    }


def _ready_control_pairs(
    edges: list[Any],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> set[tuple[str, str]]:
    """返回 ready 控制边连接的节点对。"""

    ready_handles = _ready_handle_identities(catalog_by_node)
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source = (str(edge.get("source_node_uuid")), str(edge.get("source_handle_uuid")))
        target = (str(edge.get("target_node_uuid")), str(edge.get("target_handle_uuid")))
        if source in ready_handles and target in ready_handles:
            pairs.add((source[0], target[0]))
    return pairs


def _sequential_pairs(
    edges: list[Any],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> set[tuple[str, str]]:
    """返回应保持语句顺序的节点对，包括 ready 边和数据边。"""

    pairs = _ready_control_pairs(edges, catalog_by_node)
    ready_handles = _ready_handle_identities(catalog_by_node)
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source = (str(edge.get("source_node_uuid")), str(edge.get("source_handle_uuid")))
        target = (str(edge.get("target_node_uuid")), str(edge.get("target_handle_uuid")))
        if source in ready_handles and target in ready_handles:
            continue
        pairs.add((source[0], target[0]))
    return pairs


def _is_plain_top_level_action(
    node: Mapping[str, Any],
    *,
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
    inline_expanded_composites: bool,
    all_nodes: Mapping[str, dict[str, Any]],
) -> bool:
    """顶层普通动作才参与 ready 边的顺序/并行选择。"""

    node_uuid = str(node["uuid"])
    if isinstance(node.get("parent_uuid"), str):
        return False
    action = catalog_by_node[node_uuid]
    if _is_condition(action) or _is_repeat_until(action) or _is_group(action):
        return False
    if _is_material_source_declaration(node, action):
        return False
    if (
        inline_expanded_composites
        and _is_published_workflow(action)
        and _expanded_composite_children(node_uuid, all_nodes)
    ):
        return False
    return True


def _top_level_action_run(
    start: str,
    ordered_nodes: list[dict[str, Any]],
    *,
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
    rendered: set[str],
    inline_expanded_composites: bool,
    all_nodes: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """从当前节点起收集随后连续的顶层普通动作。"""

    run: list[dict[str, Any]] = []
    started = False
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if not started:
            if node_uuid != start:
                continue
            started = True
        elif node_uuid in rendered:
            break
        if not _is_plain_top_level_action(
            node,
            catalog_by_node=catalog_by_node,
            inline_expanded_composites=inline_expanded_composites,
            all_nodes=all_nodes,
        ):
            if node_uuid == start:
                return []
            break
        run.append(node)
    return run


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
        and str(handle.get("data_source") or "executor").lower() in {"executor", "goal"}
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


def _explicit_control_dependencies(
    *,
    ordered_nodes: list[dict[str, Any]],
    edges: list[Any],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> dict[str, tuple[str, ...]]:
    """找出必须在规范 Python 中显式生成的 Ready 前序。

    图中的实际 Ready 前序若等于当前词法布局会隐式产生的前序，就保持普通顺序
    写法；否则为该节点生成 ``depends_on``。数据/物料边会替代同端点的默认 Ready
    边，因此比较前先从默认集合中删除这些硬依赖端点。
    """

    source_ready_handles = {
        (node_uuid, str(handle["uuid"]))
        for node_uuid, action in catalog_by_node.items()
        for handle in action.handles
        if handle.get("io_type") == "source"
        and handle.get("handle_key") == "ready"
    }
    target_ready_handles = {
        (node_uuid, str(handle["uuid"]))
        for node_uuid, action in catalog_by_node.items()
        for handle in action.handles
        if handle.get("io_type") == "target"
        and handle.get("handle_key") == "ready"
    }
    actual_ready_pairs: set[tuple[str, str]] = set()
    data_pairs: set[tuple[str, str]] = set()
    # V2 循环边保存在区域参数中，不是普通 Ready 连线；比较时仍须保留。
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if _is_repeat_until(catalog_by_node[node_uuid]):
            parameters = node.get("param") or {}
            actual_ready_pairs.update(
                (str(source), node_uuid)
                for source in parameters.get("predecessor_node_uuids", [])
            )
            actual_ready_pairs.update(
                (node_uuid, str(target))
                for target in parameters.get("successor_node_uuids", [])
            )
    for value in edges:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        source = (
            str(value.get("source_node_uuid")),
            str(value.get("source_handle_uuid")),
        )
        target = (
            str(value.get("target_node_uuid")),
            str(value.get("target_handle_uuid")),
        )
        pair = (source[0], target[0])
        if source in source_ready_handles and target in target_ready_handles:
            actual_ready_pairs.add(pair)
        else:
            data_pairs.add(pair)

    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_nodes: list[dict[str, Any]] = []
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if _is_group(catalog_by_node[node_uuid]):
            group_nodes.append(node)
        parent_uuid = node.get("parent_uuid")
        if isinstance(parent_uuid, str):
            children_by_parent[parent_uuid].append(node)

    implicit_pairs: set[tuple[str, str]] = set()

    def sequence_flow(
        flows: list[tuple[tuple[str, ...], tuple[str, ...]]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """组合一层默认词法顺序并把相邻执行片段边写入集合。"""

        entries: tuple[str, ...] = ()
        exits: tuple[str, ...] = ()
        for current_entries, current_exits in flows:
            if not current_entries:
                continue
            if not entries:
                entries = current_entries
            if exits:
                implicit_pairs.update(
                    (source_uuid, target_uuid)
                    for source_uuid in exits
                    for target_uuid in current_entries
                )
            exits = current_exits
        return entries, exits

    nodes_by_uuid = {str(node['uuid']): node for node in ordered_nodes}

    def declaration_only_source(node: Mapping[str, Any], action: Any) -> bool:
        # 展示分组不改变物料声明语义；条件/循环区域仍保留运行时入口。
        if not _is_material_source(action) or node.get('param', {}).get('supports_replenishment'):
            return False
        parent_uuid = node.get('parent_uuid')
        while parent_uuid:
            parent = nodes_by_uuid.get(str(parent_uuid), {})
            if parent.get('type') != 'group':
                return False
            parent_uuid = parent.get('parent_uuid')
        return True

    def action_flow(node: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """复现结构区域的真实前沿，不能把条件分支结果泄露到区域外。"""
        node_uuid = str(node["uuid"])
        action = catalog_by_node[node_uuid]
        if declaration_only_source(node, action):
            return (), ()
        if _is_group(action):
            return group_flow(node)
        if _is_condition(action):
            exits: list[str] = []
            for branch in (node.get("param") or {}).get("branches", []):
                branch_ids = set(branch.get("node_uuids", []))
                sequence_flow([action_flow(child) for child in children_by_parent[node_uuid] if str(child["uuid"]) in branch_ids])
                exits.extend(branch.get("exit_node_uuids") or [node_uuid])
            return (node_uuid,), tuple(exits)
        return (node_uuid,), (node_uuid,)

    def group_flow(node: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """按展示分组内的源码顺序计算默认控制流。"""

        return sequence_flow(
            [action_flow(child) for child in children_by_parent[str(node["uuid"])]]
        )

    top_level_flows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    # 循环体具有独立词法序列；嵌套循环在外层仍以区域节点作为出入口。
    for node in ordered_nodes:
        if _is_repeat_until(catalog_by_node[str(node["uuid"])]):
            group_flow(node)
    rendered_parallel_scopes: set[str] = set()
    for node in ordered_nodes:
        if isinstance(node.get("parent_uuid"), str):
            continue
        node_uuid = str(node["uuid"])
        action = catalog_by_node[node_uuid]
        if not _is_group(action):
            top_level_flows.append(action_flow(node))
            continue
        scope = _parallel_scope(node)
        if scope is None:
            top_level_flows.append(group_flow(node))
            continue
        if scope in rendered_parallel_scopes:
            continue
        rendered_parallel_scopes.add(scope)
        branches = sorted(
            (
                candidate
                for candidate in group_nodes
                if _parallel_scope(candidate) == scope
            ),
            key=_parallel_order,
        )
        branch_flows = [group_flow(branch) for branch in branches]
        top_level_flows.append(
            (
                tuple(item for entries, _exits in branch_flows for item in entries),
                tuple(item for _entries, exits in branch_flows for item in exits),
            )
        )
    sequence_flow(top_level_flows)

    actual_by_target: dict[str, set[str]] = defaultdict(set)
    expected_by_target: dict[str, set[str]] = defaultdict(set)
    for source, target in actual_ready_pairs:
        actual_by_target[target].add(source)
    for source, target in implicit_pairs - data_pairs:
        expected_by_target[target].add(source)
    source_order = {str(node["uuid"]): index for index, node in enumerate(ordered_nodes)}
    result: dict[str, tuple[str, ...]] = {}
    for node_uuid in source_order:
        action = catalog_by_node[node_uuid]
        if _is_group(action) or _is_material_source(action) or _is_condition(action) or _is_repeat_until(action):
            continue
        actual = actual_by_target[node_uuid]
        if actual != expected_by_target[node_uuid]:
            result[node_uuid] = tuple(sorted(actual, key=source_order.__getitem__))
    return result


def _render_action_arguments(
    *,
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
    incoming: Mapping[tuple[str, str], tuple[str, str]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
    result_name_by_uuid: Mapping[str, str] | None = None,
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
    carry_bindings = (
        unilab.get("carry_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    # ``resource_refs`` 以目标连接点 UUID 保存原部署业务 ID，使实际 UUID 参数
    # 在规范源码中仍能恢复作者声明，而不是退化为匿名字典字面量。
    resource_refs = (
        unilab.get("resource_refs", {}) if isinstance(unilab, Mapping) else {}
    )
    site_group_bindings = (
        unilab.get("site_group_bindings", {}) if isinstance(unilab, Mapping) else {}
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
            expression = f"resource_ref({json.dumps(resource_id, ensure_ascii=False)})"
        elif handle_uuid in site_group_bindings:
            binding = site_group_bindings[handle_uuid]
            group_key = (
                binding.get("group_key") if isinstance(binding, Mapping) else None
            )
            if (
                not isinstance(group_key, str)
                or not group_key.strip()
                or group_key != group_key.strip()
            ):
                raise AuthoringGraphError(
                    "candidate_invalid", "动作命名库位组元数据无效"
                )
            exact_parameter = (
                binding.get("exact_parameter") if isinstance(binding, Mapping) else None
            )
            exact_argument = (
                f", exact={exact_parameter}"
                if isinstance(exact_parameter, str) and exact_parameter
                else ""
            )
            expression = (
                f"site_group({json.dumps(group_key, ensure_ascii=False)}"
                f"{exact_argument})"
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
        elif handle_uuid in carry_bindings:
            binding = carry_bindings[handle_uuid]
            if (
                not isinstance(binding, Mapping)
                or not isinstance(binding.get("control_region_uuid"), str)
                or not isinstance(binding.get("key"), str)
            ):
                raise AuthoringGraphError("candidate_invalid", "节点 carry 绑定无效")
            region = node_by_uuid.get(str(binding["control_region_uuid"]))
            region_params = region.get("param") if isinstance(region, Mapping) else None
            loop_variable = (
                region_params.get("loop_variable")
                if isinstance(region_params, Mapping)
                else None
            )
            if not isinstance(loop_variable, str):
                raise AuthoringGraphError("candidate_invalid", "节点 carry 区域无效")
            expression = (
                f"{loop_variable}.carry["
                f"{json.dumps(str(binding['key']), ensure_ascii=False)}]"
            )
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
            source_name = _python_result_name(source_node, result_name_by_uuid)
            expression = (
                source_name
                if (
                    _is_material_source(source_action)
                    or _is_material_io(source_action)
                )
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
    *,
    result_name_by_uuid: Mapping[str, str] | None = None,
    inlined_invocations: set[str] | None = None,
) -> str:
    """渲染一个工作流输出表达式。

    参数说明：``binding`` 是保留元数据中的输出绑定，另两个索引解析节点结果；
    ``result_name_by_uuid`` 是内联去重后的实际变量名；``inlined_invocations``
    把组合调用输出投影到内部完成来源。返回 Python 表达式，非法身份失败关闭。
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
    if inlined_invocations and node_uuid in inlined_invocations:
        remapped = _remap_inlined_source(node_by_uuid[node_uuid], handle_uuid)
        if remapped is None:
            raise AuthoringGraphError(
                "candidate_invalid",
                "内联组合输出无法唯一投影到内部节点",
            )
        node_uuid, handle_uuid = remapped
    node = node_by_uuid[node_uuid]
    action = catalog_by_node[node_uuid]
    handle = next(
        (item for item in action.handles if str(item["uuid"]) == handle_uuid),
        None,
    )
    if handle is None or handle.get("io_type") != "source":
        raise AuthoringGraphError("candidate_invalid", "工作流输出连接点无效")
    result_name = _python_result_name(node, result_name_by_uuid)
    if (
        _is_material_source(action) or _is_material_io(action)
    ) and handle.get("handle_key") == "material":
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


def _is_material_source_declaration(
    node: Mapping[str, Any], action: AuthoringCatalogAction
) -> bool:
    """无需补料的顶层物料来源只作声明，不引入执行流程。"""
    return _is_material_source(action) and not node.get("param", {}).get(
        "supports_replenishment", False
    )


def _is_material_io(action: AuthoringCatalogAction) -> bool:
    """判断目录 aggregate 是否为 material_io 框架节点。"""

    return (
        action.template.get("type") == "material_io"
        and action.template.get("node_type") == "material_io"
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
    descriptions_match = description == template_description or (
        title == template_title and description in (None, "")
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
