"""工作流创作中间表示到后端形状候选图的纯转换。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.workflow.applied_authoring_projection import (
    AppliedAuthoringProjectionError,
    reconcile_applied_authoring_projection,
)
from unilabos.workflow.authoring_ast import (
    ActionDeclaration,
    CompositeDeclaration,
    DeviceDeclaration,
    GroupDeclaration,
    WorkflowProgram,
)
from unilabos.workflow.authoring_graph_semantics import (
    AuthoringGraphError,
    candidate_changeset,
    semantic_graph_equal,
)
from unilabos.workflow.authoring_graph_semantics import (
    graph_containers as _graph_containers,
)
from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.authoring_material import (
    MaterialAuthoringError,
    MaterialSourceDeclaration,
    build_material_source_node,
)
from unilabos.workflow.composite import CompositeAuthoring, CompositeExpansion
from unilabos.workflow.composite_compatibility import (
    classify_pinned_published_workflow_invocation,
)
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.resource_reference import (
    ResourceReferenceResolutionError,
    ResourceReferenceResolver,
    resolve_resource_reference,
)
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    handle_value_schema,
    resource_slot_passthrough_is_compatible,
    schema_contains_resource_slot,
    schema_is_assignable,
)


def build_candidate_graph(
    *,
    program: WorkflowProgram,
    catalog: AuthoringCatalogSnapshot,
    applied_graph: Mapping[str, Any],
    resource_reference_resolver: ResourceReferenceResolver | None = None,
    composite_authoring: CompositeAuthoring | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把静态作者程序构造为完整候选图和变更集（Changeset）。

    参数说明：``program`` 是纯 AST 解析结果，``catalog`` 是不可变目录快照，
    ``applied_graph`` 是当前权威图；``resource_reference_resolver`` 是只读库存
    权威（Inventory Authority）资源身份端口；``composite_authoring`` 是可选的
    已发布工作流只读展开端口。返回最小目录投影候选图和精确变更集；目录缺失、
    连接点不匹配、组合展开或输出不成立时抛出 ``AuthoringGraphError``。物料图违反
    物料流线性（MaterialFlowLinearity）或资源模板兼容
    （ResourceTemplate Compatibility）时，也会把内部物料图异常转换为
    ``AuthoringGraphError`` 并保留稳定错误码。
    """

    applied = _graph_containers(applied_graph)
    devices = {device.symbol: device for device in program.devices}
    action_catalog: dict[str, AuthoringCatalogAction] = {}
    result_nodes: dict[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ] = {}
    result_output_schemas: dict[tuple[str, str], dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    parent_by_node = dict(program.parent_by_node)
    source_order = {
        node_uuid: index for index, node_uuid in enumerate(program.source_order)
    }
    effective_input_contract = _resolved_input_contract(program, catalog=catalog)
    resolved_output_schemas = _resolved_declared_output_schemas(
        program,
        catalog=catalog,
    )
    compatible_catalog_replacements: set[str] = set()
    for declaration in program.groups:
        try:
            group_catalog = catalog.require_action(
                "unilabos.workflow.authoring:group",
                "group",
            )
        except AuthoringCatalogError as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "工作流创作目录缺少唯一展示分组模板",
            ) from error
        action_catalog[declaration.node_uuid] = group_catalog
        nodes.append(
            _group_node(
                declaration=declaration,
                catalog_action=group_catalog,
                source_order=source_order[declaration.node_uuid],
            )
        )
    for declaration in program.actions:
        if isinstance(declaration, CompositeDeclaration):
            if composite_authoring is None:
                raise AuthoringGraphError(
                    "composite_catalog_mismatch",
                    "工作流创作编译器未配置已发布工作流展开端口",
                )
            (
                keyword_arguments,
                resolved_resource_references,
            ) = _composite_keyword_arguments(
                declaration,
                result_nodes=result_nodes,
                resource_reference_resolver=resource_reference_resolver,
            )
            expansion = composite_authoring.compile_invocation(
                parent_workflow_uuid=program.workflow_uuid,
                invocation_uuid=declaration.node_uuid,
                module=declaration.module,
                symbol=declaration.symbol,
                keyword_arguments=keyword_arguments,
                parent_input_contract=effective_input_contract,
            )
            _require_composite_expansion(expansion)
            assert expansion.invocation_node is not None
            compatible_template_uuid = _assert_composite_pin_compatible(
                applied,
                declaration.node_uuid,
                expansion,
            )
            if compatible_template_uuid is not None:
                compatible_catalog_replacements.add(compatible_template_uuid)
            invocation = _apply_authoring_structure(
                _composite_invocation_node(
                    declaration,
                    expansion=expansion,
                    catalog=catalog,
                    source_order=source_order[declaration.node_uuid],
                    resolved_resource_references=resolved_resource_references,
                ),
                parent_uuid=parent_by_node.get(declaration.node_uuid),
                source_order=source_order[declaration.node_uuid],
            )
            nodes.append(invocation)
            internal_nodes = [
                _generated_composite_node(node, catalog=catalog)
                for node in expansion.nodes
            ]
            nodes.extend(internal_nodes)
            edges.extend(deepcopy(list(expansion.edges)))
            for expanded_node in [invocation, *internal_nodes]:
                try:
                    expanded_action = catalog.require_template(
                        str(expanded_node["workflow_node_template_uuid"])
                    )
                except (AuthoringCatalogError, KeyError) as error:
                    raise AuthoringGraphError(
                        "composite_catalog_mismatch",
                        "组合工作流展开节点引用了目录外模板",
                    ) from error
                action_catalog[str(expanded_node["uuid"])] = expanded_action
            invocation_action = action_catalog[declaration.node_uuid]
            _record_composite_output_schemas(
                declaration,
                invocation_node=invocation,
                action=invocation_action,
                catalog=catalog,
                expansion=expansion,
                result_nodes=result_nodes,
                result_output_schemas=result_output_schemas,
                input_contract=expansion.effective_parent_input_contract,
                resolved_resource_references=resolved_resource_references,
            )
            result_nodes[declaration.result_name] = (
                declaration,
                invocation_action,
            )
            effective_input_contract = deepcopy(
                expansion.effective_parent_input_contract
            )
            continue
        if isinstance(declaration, MaterialSourceDeclaration):
            try:
                # ``node`` 与 ``catalog_action`` 分别是候选事实和框架合同。
                node, catalog_action = build_material_source_node(
                    declaration,
                    catalog=catalog,
                    resource_reference_resolver=resource_reference_resolver,
                )
            except MaterialAuthoringError as error:
                raise AuthoringGraphError(error.code, error.message) from error
            action_catalog[declaration.node_uuid] = catalog_action
            result_nodes[declaration.result_name] = (declaration, catalog_action)
            nodes.append(
                _apply_authoring_structure(
                    node,
                    parent_uuid=parent_by_node.get(declaration.node_uuid),
                    source_order=source_order[declaration.node_uuid],
                )
            )
            continue
        device = devices[declaration.device_symbol]
        try:
            catalog_action = catalog.require_action(
                device.class_identity,
                declaration.action_name,
            )
        except AuthoringCatalogError as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "工作流创作目录缺少唯一动作模板",
            ) from error
        action_catalog[declaration.node_uuid] = catalog_action
        node = _apply_authoring_structure(
            _candidate_node(
                declaration=declaration,
                device=device,
                catalog_action=catalog_action,
                resource_reference_resolver=resource_reference_resolver,
            ),
            parent_uuid=parent_by_node.get(declaration.node_uuid),
            source_order=source_order[declaration.node_uuid],
        )
        _record_action_material_passthrough_schemas(
            declaration,
            node=node,
            action=catalog_action,
            catalog=catalog,
            result_nodes=result_nodes,
            result_output_schemas=result_output_schemas,
            input_contract=effective_input_contract,
            resource_reference_resolver=resource_reference_resolver,
        )
        result_nodes[declaration.result_name] = (declaration, catalog_action)
        nodes.append(node)

    for declaration in program.actions:
        target_catalog = action_catalog[declaration.node_uuid]
        for argument_name, binding in declaration.arguments:
            if binding.kind != "node_output":
                continue
            source_declaration, source_catalog = result_nodes[binding.result_name or ""]
            source_handle = _require_handle(
                source_catalog,
                key=str(binding.value),
                io_type="source",
            )
            target_handle = _require_handle(
                target_catalog,
                key=argument_name,
                io_type="target",
            )
            edges.append(
                _candidate_edge(
                    workflow_uuid=program.workflow_uuid,
                    source_node_uuid=source_declaration.node_uuid,
                    source_handle_uuid=str(source_handle["uuid"]),
                    target_node_uuid=declaration.node_uuid,
                    target_handle_uuid=str(target_handle["uuid"]),
                )
            )

    # ``order_dependencies`` 只在相邻执行片段没有真实数据边时补 ready 控制边。
    data_pairs = {
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in edges
    }
    for source_node_uuid, target_node_uuid in dict.fromkeys(
        program.order_dependencies
    ):
        if (source_node_uuid, target_node_uuid) in data_pairs:
            continue
        source_handle = _require_handle(
            action_catalog[source_node_uuid],
            key="ready",
            io_type="source",
        )
        target_handle = _require_handle(
            action_catalog[target_node_uuid],
            key="ready",
            io_type="target",
        )
        edges.append(
            _candidate_edge(
                workflow_uuid=program.workflow_uuid,
                source_node_uuid=source_node_uuid,
                source_handle_uuid=str(source_handle["uuid"]),
                target_node_uuid=target_node_uuid,
                target_handle_uuid=str(target_handle["uuid"]),
            )
        )

    # 候选图（Candidate Graph）必须在写入前采用与 ``WorkflowNodeWrite`` 相同
    # 的可选文本规范形；否则模板默认空字符串会被数据库恢复为 ``None``，破坏
    # 组合工作流调用（CompositeWorkflowInvocation）的语义固定点。
    for node in nodes:
        description = node.get("description")
        if isinstance(description, str):
            node["description"] = description.strip() or None

    workflow = deepcopy(applied["workflow"])
    workflow["uuid"] = program.workflow_uuid
    workflow["name"] = program.display_name
    workflow["description"] = program.description
    workflow_meta = dict(workflow.get("meta_data") or {})
    unilab_meta = dict(workflow_meta.get("unilab") or {})
    unilab_meta.update(
        {
            "authoring_function_name": program.function_name,
            "authoring_result_record_name": (
                program.result_record_name
                or (
                    "".join(
                        part.capitalize() for part in program.function_name.split("_")
                    )
                    + "Result"
                    if program.outputs
                    else None
                )
            ),
            "input_contract": effective_input_contract,
            "output_contract": _output_contract(
                program,
                result_nodes,
                input_contract=effective_input_contract,
                declared_output_schemas=resolved_output_schemas,
                result_output_schemas=result_output_schemas,
            ),
            "output_bindings": _output_bindings(program, result_nodes),
        }
    )
    workflow_meta["unilab"] = unilab_meta
    workflow["meta_data"] = workflow_meta

    def generated_node_source_sort_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
        """读取生成节点的作者源码顺序。

        参数：``item`` 是已生成工作流节点。返回：该节点在源码中的非负顺序。
        异常：身份缺失时由映射访问抛出并由创作入口失败关闭。
        """

        node_uuid = str(item["uuid"])
        if node_uuid in source_order:
            return source_order[node_uuid], 0, node_uuid
        parent_uuid = item.get("parent_uuid")
        visited: set[str] = set()
        while isinstance(parent_uuid, str) and parent_uuid not in visited:
            if parent_uuid in source_order:
                return source_order[parent_uuid], 1, node_uuid
            visited.add(parent_uuid)
            parent = next(
                (
                    candidate
                    for candidate in nodes
                    if str(candidate.get("uuid")) == parent_uuid
                ),
                None,
            )
            parent_uuid = parent.get("parent_uuid") if parent is not None else None
        raise AuthoringGraphError(
            "composite_catalog_mismatch",
            "组合工作流内部节点缺少可追溯调用父级",
        )

    def generated_edge_uuid_sort_key(item: Mapping[str, Any]) -> str:
        """读取生成边的稳定 UUID 排序键。

        参数：``item`` 是已生成工作流边。返回：字符串 UUID。异常：无；边身份
        已在图构造阶段校验。
        """

        return str(item["uuid"])

    try:
        # ``projection`` 在一个深模块（Deep Module）内完成已应用读形状、当前目录
        # 语义与新生成实体的固定点合并，不把混代规则泄漏给图构造调用者。
        projection = reconcile_applied_authoring_projection(
            workflow_uuid=program.workflow_uuid,
            applied_graph=applied,
            generated_nodes=sorted(
                nodes,
                key=generated_node_source_sort_key,
            ),
            generated_edges=sorted(edges, key=generated_edge_uuid_sort_key),
            action_catalog=action_catalog,
            compatible_catalog_replacements=compatible_catalog_replacements,
        )
    except AppliedAuthoringProjectionError as error:
        raise AuthoringGraphError(error.code, error.message) from error

    graph = {
        "workflow": workflow,
        "nodes": projection.nodes,
        "edges": projection.edges,
        "node_templates": projection.node_templates,
        "handle_templates": projection.handle_templates,
    }
    try:
        validate_material_graph_projection(graph)
    except MaterialGraphValidationError as error:
        raise AuthoringGraphError(error.code, error.message) from error
    changeset = candidate_changeset(graph=graph, applied_graph=applied)
    return graph, changeset


def _composite_keyword_arguments(
    declaration: CompositeDeclaration,
    *,
    result_nodes: Mapping[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ],
    resource_reference_resolver: ResourceReferenceResolver | None,
) -> tuple[dict[str, object], dict[str, dict[str, str | None]]]:
    """把静态值绑定转换为组合展开端口接受的边界来源。

    参数：``declaration`` 是调用声明，``result_nodes`` 解析前序节点输出连接点。
    返回：按参数名索引的边界来源及已解析资源引用；后者保留模板身份供调用
    连接点兼容性校验。异常：未知绑定、资源身份或输出连接点不唯一时抛出
    ``AuthoringGraphError``。
    """

    result: dict[str, object] = {}
    resolved_resources: dict[str, dict[str, str | None]] = {}
    for name, binding in declaration.arguments:
        if binding.kind == "literal":
            result[name] = deepcopy(binding.value)
        elif binding.kind == "workflow_input":
            result[name] = {
                "kind": "workflow_input",
                "parameter": str(binding.value),
            }
        elif binding.kind == "node_output":
            source_declaration, source_action = result_nodes[
                binding.result_name or ""
            ]
            source_handle = _require_handle(
                source_action,
                key=str(binding.value),
                io_type="source",
            )
            result[name] = {
                "kind": "node_output",
                "workflow_node_uuid": source_declaration.node_uuid,
                "source_handle_uuid": str(source_handle["uuid"]),
            }
        elif binding.kind == "resource_ref":
            try:
                resolved_reference = resolve_resource_reference(
                    str(binding.value),
                    resource_reference_resolver,
                )
            except ResourceReferenceResolutionError as error:
                raise AuthoringGraphError(
                    "resource_reference_resolution_error",
                    str(error),
                ) from error
            result[name] = {"uuid": resolved_reference["uuid"]}
            resolved_resources[name] = resolved_reference
        else:
            raise AuthoringGraphError(
                "composite_boundary_mapping_invalid",
                "已发布工作流参数来源不受支持",
            )
    return result, resolved_resources


def _require_composite_expansion(expansion: CompositeExpansion) -> None:
    """把组合展开的首个稳定诊断提升为创作图错误。

    参数：``expansion`` 是只读组合端口结果。返回：成功时无。异常：结果类型或
    候选不完整时抛出保留公共错误码的 ``AuthoringGraphError``。
    """

    if not isinstance(expansion, CompositeExpansion):
        raise AuthoringGraphError(
            "composite_catalog_mismatch",
            "组合展开端口返回了非法结果",
        )
    if expansion.invocation_node is not None and not expansion.diagnostics:
        return
    diagnostic = expansion.diagnostics[0] if expansion.diagnostics else {}
    raise AuthoringGraphError(
        str(diagnostic.get("code") or "composite_catalog_mismatch"),
        str(diagnostic.get("message") or "组合工作流展开失败"),
    )


def _record_composite_output_schemas(
    declaration: CompositeDeclaration,
    *,
    invocation_node: dict[str, Any],
    action: AuthoringCatalogAction,
    catalog: AuthoringCatalogSnapshot,
    expansion: CompositeExpansion,
    result_nodes: Mapping[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ],
    result_output_schemas: dict[tuple[str, str], dict[str, Any]],
    input_contract: Mapping[str, Any],
    resolved_resource_references: Mapping[str, Mapping[str, str | None]],
) -> None:
    """记录组合调用中隐式物料透传输出的实际来源类型。

    参数：调用声明、发布动作、展开映射及已解析上游事实。返回：
    无；原地追加按结果变量与输出键索引的 Schema。异常：边界映射
    引用不存在的调用参数或来源时抛出 ``AuthoringGraphError``。
    """

    arguments = dict(declaration.arguments)
    input_schemas = {
        str(item["name"]): deepcopy(item["schema"])
        for item in input_contract["parameters"]
    }
    for handle in action.detached_handles():
        if handle.get("io_type") != "source":
            continue
        handle_uuid = str(handle.get("uuid") or "")
        mapping = expansion.source_mappings.get(handle_uuid)
        if not isinstance(mapping, Mapping) or mapping.get("kind") != "workflow_input":
            continue
        parameter_name = str(mapping.get("parameter") or "")
        binding = arguments.get(parameter_name)
        if binding is None:
            raise AuthoringGraphError(
                "composite_boundary_mapping_invalid",
                "组合工作流隐式输出缺少调用参数来源",
            )
        if binding.kind == "workflow_input":
            schema = input_schemas.get(str(binding.value))
        elif binding.kind == "node_output":
            source_declaration, source_action = result_nodes[
                binding.result_name or ""
            ]
            if isinstance(source_declaration, MaterialSourceDeclaration):
                schema = {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [
                        catalog.require_resource_template_uuid(
                            source_declaration.resource_template_symbol
                        )
                    ],
                }
            else:
                schema = result_output_schemas.get(
                    (source_declaration.result_name, str(binding.value))
                )
            if schema is None:
                schema = _handle_schema(
                    _require_handle(
                        source_action,
                        key=str(binding.value),
                        io_type="source",
                    )
                )
        elif binding.kind == "resource_ref":
            reference = resolved_resource_references.get(parameter_name)
            template_uuid = (
                reference.get("resource_template_uuid")
                if isinstance(reference, Mapping)
                else None
            )
            schema = {"$slot": "ResourceSlot"}
            if isinstance(template_uuid, str):
                schema["allowed_resource_template_uuids"] = [template_uuid]
        else:
            schema = None
        if not isinstance(schema, Mapping):
            raise AuthoringGraphError(
                "composite_boundary_mapping_invalid",
                "组合工作流隐式输出来源类型无法证明",
            )
        result_output_schemas[
            (declaration.result_name, str(handle["handle_key"]))
        ] = deepcopy(dict(schema))
        unilab = invocation_node.setdefault("meta_data", {}).setdefault(
            "unilab", {}
        )
        overrides = unilab.setdefault("output_schema_overrides", {})
        if not isinstance(overrides, dict):
            raise AuthoringGraphError(
                "composite_boundary_mapping_invalid",
                "组合工作流输出类型覆盖必须是对象",
            )
        overrides[handle_uuid] = deepcopy(dict(schema))


def _record_action_material_passthrough_schemas(
    declaration: ActionDeclaration,
    *,
    node: dict[str, Any],
    action: AuthoringCatalogAction,
    catalog: AuthoringCatalogSnapshot,
    result_nodes: Mapping[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ],
    result_output_schemas: dict[tuple[str, str], dict[str, Any]],
    input_contract: Mapping[str, Any],
    resource_reference_resolver: ResourceReferenceResolver | None,
) -> None:
    """为普通动作的同名物料输入输出保留上游实际类型。

    参数：动作声明、候选节点、目录合同及已解析上游事实。返回：
    无；原地记录结果 Schema 和可验证的连接点对。异常：资源引用无法
    解析时抛出 ``AuthoringGraphError``。
    """

    arguments = dict(declaration.arguments)
    input_schemas = {
        str(item["name"]): deepcopy(item["schema"])
        for item in input_contract["parameters"]
    }
    for source_handle in action.detached_handles():
        handle_key = str(source_handle.get("handle_key") or "")
        if (
            source_handle.get("io_type") != "source"
            or handle_key == "ready"
            or not schema_contains_resource_slot(_handle_schema(source_handle))
            or handle_key not in arguments
        ):
            continue
        try:
            target_handle = _require_handle(
                action,
                key=handle_key,
                io_type="target",
            )
        except AuthoringGraphError:
            continue
        if not schema_contains_resource_slot(_handle_schema(target_handle)):
            continue
        binding = arguments[handle_key]
        if binding.kind == "workflow_input":
            schema = input_schemas.get(str(binding.value))
        elif binding.kind == "node_output":
            source_declaration, source_action = result_nodes[
                binding.result_name or ""
            ]
            if isinstance(source_declaration, MaterialSourceDeclaration):
                schema = {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [
                        catalog.require_resource_template_uuid(
                            source_declaration.resource_template_symbol
                        )
                    ],
                }
            else:
                schema = result_output_schemas.get(
                    (source_declaration.result_name, str(binding.value))
                )
            if schema is None:
                schema = _handle_schema(
                    _require_handle(
                        source_action,
                        key=str(binding.value),
                        io_type="source",
                    )
                )
        elif binding.kind == "resource_ref":
            try:
                reference = resolve_resource_reference(
                    str(binding.value),
                    resource_reference_resolver,
                )
            except ResourceReferenceResolutionError as error:
                raise AuthoringGraphError(
                    "resource_reference_resolution_error",
                    str(error),
                ) from error
            schema = {"$slot": "ResourceSlot"}
            template_uuid = reference.get("resource_template_uuid")
            if isinstance(template_uuid, str):
                schema["allowed_resource_template_uuids"] = [template_uuid]
        else:
            schema = None
        if (
            not isinstance(schema, Mapping)
            or not schema_is_assignable(schema, _handle_schema(source_handle))
        ):
            continue
        source_uuid = str(source_handle["uuid"])
        result_output_schemas[(declaration.result_name, handle_key)] = deepcopy(
            dict(schema)
        )
        unilab = node.setdefault("meta_data", {}).setdefault("unilab", {})
        overrides = unilab.setdefault("output_schema_overrides", {})
        passthroughs = unilab.setdefault("material_passthrough_handles", {})
        if not isinstance(overrides, dict) or not isinstance(passthroughs, dict):
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "物料透传类型元数据必须是对象",
            )
        overrides[source_uuid] = deepcopy(dict(schema))
        passthroughs[source_uuid] = str(target_handle["uuid"])


def _assert_composite_pin_compatible(
    applied_graph: Mapping[str, Any],
    invocation_uuid: str,
    expansion: CompositeExpansion,
) -> str | None:
    """拒绝已应用调用节点的发布合同发生破坏性漂移。

    参数：已应用图、调用 UUID 和当前展开。返回：首次调用时为 ``None``；旧
    调用精确或可加兼容时返回允许整代替换的模板 UUID。异常：旧投影未经认证、
    身份混代或合同破坏时抛出 ``AuthoringGraphError``。
    """

    applied_node = next(
        (
            node
            for node in applied_graph["nodes"]
            if isinstance(node, Mapping) and node.get("uuid") == invocation_uuid
        ),
        None,
    )
    if applied_node is None:
        return None
    current_node = expansion.invocation_node
    if not isinstance(current_node, Mapping):
        raise AuthoringGraphError(
            "composite_contract_stale",
            "当前已发布工作流调用缺少冻结合同投影",
        )
    compatibility = classify_pinned_published_workflow_invocation(
        previous_node=applied_node,
        current_node=current_node,
        previous_templates=applied_graph["node_templates"],
        previous_handles=applied_graph["handle_templates"],
    )
    if compatibility == "breaking":
        raise AuthoringGraphError(
            "composite_contract_stale",
            "已发布工作流合同发生破坏性变化",
        )
    template_uuid = current_node.get("workflow_node_template_uuid")
    if not isinstance(template_uuid, str):
        raise AuthoringGraphError(
            "composite_contract_stale",
            "当前已发布工作流模板身份无效",
        )
    return template_uuid


def _composite_invocation_node(
    declaration: CompositeDeclaration,
    *,
    expansion: CompositeExpansion,
    catalog: AuthoringCatalogSnapshot,
    source_order: int,
    resolved_resource_references: Mapping[str, Mapping[str, str | None]],
) -> dict[str, Any]:
    """把展开调用节点补齐作者结果、输入绑定和展示元数据。

    参数：调用声明、成功展开、不可变目录、源码顺序及已解析资源引用。返回：
    不含数据库读字段的候选调用节点。异常：边界连接点、资源模板或目录模板
    缺失时抛出 ``AuthoringGraphError``。
    """

    assert expansion.invocation_node is not None
    node = deepcopy(dict(expansion.invocation_node))
    for field in ("create_time", "update_time", "workflow_uuid", "status"):
        node.pop(field, None)
    action = catalog.require_template(str(node["workflow_node_template_uuid"]))
    params: dict[str, Any] = {}
    input_bindings: dict[str, dict[str, str]] = {}
    resource_refs: dict[str, dict[str, str]] = {}
    for name, binding in declaration.arguments:
        handle = _require_handle(action, key=name, io_type="target")
        handle_uuid = str(handle["uuid"])
        if binding.kind == "literal":
            params[name] = deepcopy(binding.value)
        elif binding.kind == "workflow_input":
            input_bindings[handle_uuid] = {"parameter": str(binding.value)}
        elif binding.kind == "resource_ref":
            resolved_reference = resolved_resource_references.get(name)
            if not isinstance(resolved_reference, Mapping):
                raise AuthoringGraphError(
                    "resource_reference_resolution_error",
                    f"已发布工作流参数 {name} 缺少已解析资源身份",
                )
            _validate_action_resource_reference(
                resolved_reference,
                target_handle=handle,
                argument_name=name,
            )
            params[name] = {"uuid": str(resolved_reference["uuid"])}
            resource_refs[handle_uuid] = {"resource_id": str(binding.value)}
    node["param"] = params
    template = action.template
    node["name"] = declaration.title or str(
        template.get("display_name") or template.get("name") or declaration.symbol
    )
    node["description"] = (
        declaration.description
        if declaration.description is not None
        else template.get("description")
    )
    meta_data = node.setdefault("meta_data", {})
    unilab = meta_data.setdefault("unilab", {})
    unilab.update(
        {
            "input_bindings": input_bindings,
            "authoring_result_name": declaration.result_name,
            "authoring_source_order": source_order,
        }
    )
    if resource_refs:
        unilab["resource_refs"] = dict(sorted(resource_refs.items()))
    return node


def _generated_composite_node(
    node: Mapping[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, Any]:
    """移除只属于数据库读投影的组合内部节点字段。

    参数：``node`` 是只读子快照节点，``catalog`` 提供展示默认值。返回：可交给
    候选投影的分离节点。异常：模板不存在时抛出 ``AuthoringCatalogError``。
    """

    result = deepcopy(dict(node))
    for field in ("create_time", "update_time", "workflow_uuid", "status"):
        result.pop(field, None)
    action = catalog.require_template(str(result["workflow_node_template_uuid"]))
    template = action.template
    if result.get("action_name"):
        result["action_type"] = str(
            template.get("type") or "UniLabJsonCommand"
        )
    if result.get("description") is None:
        result["description"] = template.get("description")
    return result


def _group_node(
    *,
    declaration: GroupDeclaration,
    catalog_action: AuthoringCatalogAction,
    source_order: int,
) -> dict[str, Any]:
    """构造一个不参与执行边的展示分组节点（Presentation Group Node）。

    参数说明：``declaration`` 提供稳定节点身份、展示名和并行归属；
    ``catalog_action`` 是唯一框架模板；``source_order`` 是确定性源码顺序。返回：
    后端形状分组节点，其 ``meta_data.unilab`` 足以恢复 ``group/parallel`` 源码；
    异常：目录模板字段缺失时由调用后的候选校验失败关闭。
    """

    template = catalog_action.template
    # ``parallel_scope`` 只关联同一个并行结构内的同级展示分组，不成为执行身份。
    parallel_scope = declaration.parallel_scope
    return {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": None,
        "name": declaration.title or declaration.name,
        "type": "group",
        "icon": template.get("icon"),
        "pose": {},
        "param": {"name": declaration.name},
        "footer": template.get("footer"),
        "action_name": None,
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": (
            declaration.description
            if declaration.description is not None
            else template.get("description")
        ),
        "meta_data": {
            "unilab": {
                "authoring_source_order": source_order,
                "presentation_group": True,
                "parallel_scope": parallel_scope,
                "parallel_order": declaration.parallel_order,
            }
        },
    }


def _apply_authoring_structure(
    node: dict[str, Any],
    *,
    parent_uuid: str | None,
    source_order: int,
) -> dict[str, Any]:
    """把展示父关系与确定性源码顺序加入一个已构造候选节点。

    参数说明：``node`` 是本轮新建、可原位修改的动作或物料来源节点；
    ``parent_uuid`` 是可选展示分组 UUID；``source_order`` 是节点在作者源码中的
    零基顺序。返回：同一节点字典。异常：既有元数据形状非法时抛出 ``TypeError``，
    防止覆盖其他创作事实。
    """

    node["parent_uuid"] = parent_uuid
    meta_data = node.setdefault("meta_data", {})
    unilab = meta_data.setdefault("unilab", {})
    if not isinstance(unilab, dict):
        raise TypeError("候选节点创作元数据必须是对象")
    unilab["authoring_source_order"] = source_order
    return node


def _candidate_node(
    *,
    declaration: ActionDeclaration,
    device: DeviceDeclaration,
    catalog_action: AuthoringCatalogAction,
    resource_reference_resolver: ResourceReferenceResolver | None = None,
) -> dict[str, Any]:
    """构造一个后端写形状节点。

    参数说明：动作声明（ActionDeclaration）提供源码身份；设备声明提供
    执行器绑定（ExecutorBinding）；目录动作（AuthoringCatalogAction）提供
    动作模板（Action Template）和连接点（Handle）定义。返回：不含
    数据库时间字段的节点字典；固定执行器（Fixed Executor）的
    实际设备物料（Material）UUID 同时进入顶层 ``material_uuid`` 和保留
    执行器绑定（ExecutorBinding）元数据；动态执行器绑定（ExecutorBinding）
    保持空值；``resource_reference_resolver`` 把部署业务资源 ID 关闭式解析为
    实际物料 UUID。异常：动作参数连接点或资源身份无法证明时抛出
    ``AuthoringGraphError``。
    """

    params: dict[str, Any] = {}
    input_bindings: dict[str, dict[str, str]] = {}
    # ``resource_refs`` 仅保留规范源码往返需要的部署业务 ID，键使用真实目标
    # 连接点（Handle）UUID；实际物料身份单独进入 ``params``。
    resource_refs: dict[str, dict[str, str]] = {}
    for argument_name, binding in declaration.arguments:
        target_handle = _require_handle(
            catalog_action,
            key=argument_name,
            io_type="target",
        )
        handle_uuid = str(target_handle["uuid"])
        if binding.kind == "literal":
            params[argument_name] = deepcopy(binding.value)
        elif binding.kind == "workflow_input":
            input_bindings[handle_uuid] = {"parameter": str(binding.value)}
        elif binding.kind == "resource_ref":
            try:
                # ``resolved_reference`` 是库存权威证明的实际物料与模板身份。
                resolved_reference = resolve_resource_reference(
                    str(binding.value),
                    resource_reference_resolver,
                )
                _validate_action_resource_reference(
                    resolved_reference,
                    target_handle=target_handle,
                    argument_name=argument_name,
                )
            except ResourceReferenceResolutionError as error:
                raise AuthoringGraphError(
                    "resource_reference_resolution_error",
                    str(error),
                ) from error
            params[argument_name] = {"uuid": resolved_reference["uuid"]}
            resource_refs[handle_uuid] = {"resource_id": str(binding.value)}
    # 作者结果变量是 Python 数据依赖身份，必须与可编辑的节点标题分离保存。
    unilab: dict[str, Any] = {
        "input_bindings": input_bindings,
        "authoring_result_name": declaration.result_name,
    }
    if resource_refs:
        unilab["resource_refs"] = dict(sorted(resource_refs.items()))
    template = catalog_action.template
    if device.device_id is not None:
        unilab["executor_binding"] = {
            "mode": "fixed",
            "device_id": device.device_id,
        }
    # ``device_material_uuid`` 必须是库存权威证明的实际设备物料身份；部署业务
    # ID 只保留在执行器绑定（ExecutorBinding）中供运行时派发。未注入解析端口
    # 时仅兼容作者直接声明规范 UUID，动态 ``device()`` 继续保持空值。
    device_material_uuid = None
    if device.device_id is not None:
        try:
            resolved_device = resolve_resource_reference(
                device.device_id,
                resource_reference_resolver,
            )
        except ResourceReferenceResolutionError as error:
            raise AuthoringGraphError(
                "invalid_executor_binding",
                "固定执行器无法解析为实际设备物料身份",
            ) from error
        # ``expected_device_template_uuid`` 是动作模板声明的设备类型；库存回执若
        # 提供模板身份，必须与它一致，不能把另一类设备绑定到该动作。
        expected_device_template_uuid = template.get("resource_template_uuid")
        resolved_device_template_uuid = resolved_device.get("resource_template_uuid")
        if (
            resolved_device_template_uuid is not None
            and resolved_device_template_uuid != expected_device_template_uuid
        ):
            raise AuthoringGraphError(
                "invalid_executor_binding",
                "固定执行器资源模板与动作模板不一致",
            )
        device_material_uuid = resolved_device["uuid"]
    # 模板标题是未显式覆盖时的节点展示默认值；动作业务名仅作旧目录回退。
    template_title = (
        template.get("display_name") or template.get("name") or declaration.result_name
    )
    return {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": device_material_uuid,
        "name": declaration.title or template_title,
        "type": str(template.get("node_type") or template.get("type") or "compute"),
        "icon": template.get("icon"),
        "pose": {},
        "param": params,
        "footer": template.get("footer"),
        "action_name": declaration.action_name,
        "action_type": str(template.get("type") or "UniLabJsonCommand"),
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": (
            declaration.description
            if declaration.description is not None
            else template.get("description")
        ),
        "meta_data": {"unilab": unilab},
    }


def _validate_action_resource_reference(
    reference: Mapping[str, str | None],
    *,
    target_handle: Mapping[str, Any],
    argument_name: str,
) -> None:
    """证明动作 ``resource_ref`` 只绑定兼容物料占位符（ResourceSlot）。

    参数：``reference`` 是已解析实际物料身份，``target_handle`` 是动作参数的
    真实目标连接点，``argument_name`` 用于稳定中文诊断。返回：无。异常：目标
    不是物料占位符（ResourceSlot），或资源模板不在允许集合时抛出
    ``AuthoringGraphError``，不能把业务 ID 当普通 JSON 参数放行。
    """

    # ``value_schema`` 是当前目录代际冻结的动作输入值合同。
    value_schema = _handle_schema(target_handle)
    # ``is_resource_slot`` 同时接受规范连接点类型和旧值 Schema 标记；注册表
    # 投影已经把物料参数发布为 ``ResourceSlot``，无需再重复写入 ``$slot``。
    is_resource_slot = (
        target_handle.get("type") == "ResourceSlot"
        or value_schema.get("$slot") == "ResourceSlot"
    )
    if not is_resource_slot:
        raise AuthoringGraphError(
            "resource_reference_resolution_error",
            f"动作参数 {argument_name} 不是物料占位符（ResourceSlot）",
        )
    # ``allowed_templates`` 是该动作输入明确接受的资源模板 UUID 集合；省略表示
    # 不在创作期缩窄模板，但实际物料 UUID 仍已由库存权威验证。
    allowed_templates = value_schema.get("allowed_resource_template_uuids")
    if allowed_templates not in (None, [], ()) and reference.get(
        "resource_template_uuid"
    ) not in allowed_templates:
        raise AuthoringGraphError(
            "resource_reference_resolution_error",
            f"动作参数 {argument_name} 不接受该物料资源模板",
        )


def _candidate_edge(
    *,
    workflow_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> dict[str, Any]:
    """构造一条带确定性身份的数据边。

    参数说明：工作流及四个端点 UUID 完整描述边；返回后端写形状字典。
    """

    return {
        "uuid": authoring_edge_uuid(
            workflow_uuid=workflow_uuid,
            source_node_uuid=source_node_uuid,
            source_handle_uuid=source_handle_uuid,
            target_node_uuid=target_node_uuid,
            target_handle_uuid=target_handle_uuid,
        ),
        "source_node_uuid": source_node_uuid,
        "target_node_uuid": target_node_uuid,
        "source_handle_uuid": source_handle_uuid,
        "target_handle_uuid": target_handle_uuid,
        "description": None,
        "meta_data": {},
    }


def _require_handle(
    action: AuthoringCatalogAction,
    *,
    key: str,
    io_type: str,
) -> dict[str, Any]:
    """按业务键和方向取得唯一连接点（Handle）。

    参数说明：``action`` 是目录 aggregate，``key`` 和 ``io_type`` 来自作者绑定；
    返回与不可变目录分离的连接点投影，缺失或歧义抛出
    ``AuthoringGraphError``；递归解冻由目录 aggregate 的公共接口完成。
    """

    # ``detached_handles`` 把目录内嵌套只读映射递归还原为普通 JSON 容器；调用方
    # 后续读取或深拷贝值 Schema 时不会尝试 pickle ``mappingproxy``。
    detached_handles = action.detached_handles()
    matches = [
        handle
        for handle in detached_handles
        if handle.get("handle_key") == key and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise AuthoringGraphError(
            "template_catalog_mismatch",
            f"动作连接点 {io_type}:{key} 缺失或不唯一",
        )
    return matches[0]


def _output_contract(
    program: WorkflowProgram,
    result_nodes: Mapping[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ],
    *,
    input_contract: Mapping[str, Any],
    declared_output_schemas: Mapping[str, Mapping[str, Any]],
    result_output_schemas: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """从输出绑定构造版本 1 工作流输出合同。

    参数说明：``program`` 含输出声明，``result_nodes`` 提供节点输出连接点
    （Handle）类型，另两个参数是已按目录代际解析资源模板身份的输入合同和结果
    Schema。返回：包含版本和规范输出描述列表的工作流输出合同；输出引用缺失或
    歧义、显式结果记录 Schema 与绑定类型不一致、结果记录字段集与返回字典不
    一致时抛出 ``AuthoringGraphError``，不生成部分合同。
    异常：上述输出引用或 Schema 合同不成立时抛出 ``AuthoringGraphError``。
    """

    inputs = {
        item["name"]: item["schema"] for item in input_contract["parameters"]
    }
    declared = {
        name: deepcopy(schema)
        for name, schema in declared_output_schemas.items()
    }
    outputs: list[dict[str, Any]] = []
    for name, binding in program.outputs:
        if binding.kind == "workflow_input":
            schema = deepcopy(inputs[str(binding.value)])
        else:
            declaration, action = result_nodes[binding.result_name or ""]
            handle = _require_handle(action, key=str(binding.value), io_type="source")
            schema = deepcopy(
                result_output_schemas.get(
                    (declaration.result_name, str(binding.value)),
                    _handle_schema(handle),
                )
            )
        if name in declared and not schema_is_assignable(schema, declared[name]):
            raise AuthoringGraphError(
                "invalid_workflow_output",
                f"结果记录字段 {name} 与绑定类型不一致",
            )
        outputs.append({"name": name, "schema": schema, "implicit": False})
    if declared and set(declared) != {item["name"] for item in outputs}:
        raise AuthoringGraphError(
            "invalid_workflow_output",
            "结果记录字段与返回字典不一致",
        )
    # ``outputs_by_name`` 只用于检查作者显式输出；服务端隐式输出随后按工作流
    # 输入合同顺序追加，保持 integration D-068 的确定性身份和渲染固定点。
    outputs_by_name = {str(item["name"]): item for item in outputs}
    for parameter in input_contract["parameters"]:
        parameter_name = str(parameter["name"])
        parameter_schema = parameter["schema"]
        if not schema_contains_resource_slot(parameter_schema):
            continue
        existing = outputs_by_name.get(parameter_name)
        if existing is not None:
            if not resource_slot_passthrough_is_compatible(
                parameter_schema,
                existing["schema"],
            ):
                raise AuthoringGraphError(
                    "invalid_workflow_output",
                    "同名显式输出与物料占位符输入透传不兼容",
                )
            continue
        implicit_output = {
            "name": parameter_name,
            "schema": deepcopy(parameter_schema),
            "implicit": True,
        }
        for presentation_field in ("title", "description"):
            if presentation_field in parameter:
                implicit_output[presentation_field] = deepcopy(
                    parameter[presentation_field]
                )
        outputs.append(implicit_output)
        outputs_by_name[parameter_name] = implicit_output
    return {"version": 1, "outputs": outputs}


def _resolved_input_contract(
    program: WorkflowProgram,
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, Any]:
    """把工作流输入注解中的资源模板源码身份冻结为本代 UUID。"""

    contract = deepcopy(program.input_contract)
    schemas = {
        str(parameter["name"]): parameter["schema"]
        for parameter in contract["parameters"]
    }
    _resolve_resource_template_symbols(
        schemas,
        program.input_resource_template_symbols,
        catalog=catalog,
    )
    return contract


def _resolved_declared_output_schemas(
    program: WorkflowProgram,
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, dict[str, Any]]:
    """把显式结果记录的资源模板源码身份冻结为本代 UUID。"""

    schemas = {
        name: deepcopy(schema)
        for name, schema in program.declared_output_schemas
    }
    _resolve_resource_template_symbols(
        schemas,
        program.output_resource_template_symbols,
        catalog=catalog,
    )
    return schemas


def _resolve_resource_template_symbols(
    schemas: Mapping[str, dict[str, Any]],
    symbol_groups: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> None:
    """在已解析物料占位符（ResourceSlot）Schema 上写入目录 UUID 允许集合。"""

    for name, symbols in symbol_groups:
        schema = schemas.get(name)
        if schema is None:
            raise AuthoringGraphError(
                "candidate_invalid",
                "资源模板注解没有对应的工作流输入或结果字段",
            )
        try:
            template_uuids = [
                catalog.require_resource_template_uuid(symbol)
                for symbol in symbols
            ]
        except AuthoringCatalogError as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "工作流注解引用了目录外资源模板",
            ) from error
        slot_schemas = _resource_slot_schemas(schema)
        if len(slot_schemas) != 1:
            raise AuthoringGraphError(
                "candidate_invalid",
                "资源模板注解必须唯一约束物料占位符（ResourceSlot）",
            )
        slot_schemas[0]["allowed_resource_template_uuids"] = template_uuids


def _resource_slot_schemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """返回值 Schema 中全部物料占位符（ResourceSlot）子 Schema。"""

    result: list[dict[str, Any]] = []
    pending = [schema]
    while pending:
        item = pending.pop()
        if item.get("$slot") == "ResourceSlot":
            result.append(item)
        members = item.get("anyOf")
        if isinstance(members, list):
            pending.extend(
                member for member in members if isinstance(member, dict)
            )
        child = item.get("items")
        if isinstance(child, dict):
            pending.append(child)
    return result


def _output_bindings(
    program: WorkflowProgram,
    result_nodes: Mapping[
        str,
        tuple[
            ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
            AuthoringCatalogAction,
        ],
    ],
) -> dict[str, dict[str, str]]:
    """把作者输出声明映射为稳定工作流输出绑定。

    参数说明：程序输出引用工作流输入或节点结果；返回按输出名索引的绑定字典。
    异常：节点结果连接点（Handle）缺失或歧义时抛出 ``AuthoringGraphError``。
    """

    bindings: dict[str, dict[str, str]] = {}
    for name, binding in program.outputs:
        if binding.kind == "workflow_input":
            bindings[name] = {"kind": "workflow_input", "parameter": str(binding.value)}
        else:
            declaration, action = result_nodes[binding.result_name or ""]
            handle = _require_handle(action, key=str(binding.value), io_type="source")
            bindings[name] = {
                "kind": "node_output",
                "workflow_node_uuid": declaration.node_uuid,
                "source_handle_uuid": str(handle["uuid"]),
            }
    explicit_names = set(bindings)
    for parameter in program.input_contract["parameters"]:
        parameter_name = str(parameter["name"])
        if (
            parameter_name not in explicit_names
            and schema_contains_resource_slot(parameter["schema"])
        ):
            bindings[parameter_name] = {
                "kind": "workflow_input",
                "parameter": parameter_name,
            }
    return bindings


def _handle_schema(handle: Mapping[str, Any]) -> dict[str, Any]:
    """读取连接点（Handle）的规范值 Schema。

    参数说明：优先读取 ``meta_data.unilab.value_schema``，缺失时兼容旧 ``type``；
    返回独立 Schema 字典，未知类型退化为无约束 JSON 对象。
    """

    try:
        # ``handle_value_schema`` 是动作 JSON Schema 到工作流值 Schema 的唯一
        # 适配器，统一处理冻结容器、物料引用、数组、可空和允许集合。
        return handle_value_schema(handle).to_dict()
    except WorkflowIOValidationError as error:
        raise AuthoringGraphError(
            "template_catalog_mismatch",
            "动作连接点值 Schema 无法解析",
        ) from error


__all__ = [
    "AuthoringGraphError",
    "build_candidate_graph",
    "candidate_changeset",
    "semantic_graph_equal",
]
