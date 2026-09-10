"""组合工作流调用（CompositeWorkflowInvocation）的只读静态展开算法。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from unilabos.workflow.authoring_identity import (
    authoring_edge_uuid,
    expanded_node_uuid,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.catalog import (
    PublishedSourceCatalogError,
    PublishedWorkflowSource,
)
from unilabos.workflow.composite_compatibility import (
    classify_pinned_published_workflow_invocation,
    published_workflow_compatibility_projection,
)
from unilabos.workflow.handle_projection import resource_slot_schema
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    schema_is_assignable,
    validate_workflow_graph_io,
)


class PublishedWorkflowSnapshotProvider(Protocol):
    """按稳定工作流身份读取同视图已应用快照的窄端口。"""

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Mapping[str, Any]:
        """返回已应用工作流快照。

        参数：``workflow_uuid`` 是工作流身份。返回：同视图冻结快照。异常：身份
        不存在时抛出 ``LookupError``。
        """


class PublishedWorkflowResolver(Protocol):
    """按绝对模块与静态符号解析已发布工作流来源的窄端口。"""

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
        """返回唯一冻结来源。

        参数：``module`` 与 ``symbol`` 是绝对导入身份。返回：发布来源值对象。
        异常：不存在或歧义时抛出目录错误。
        """


@dataclass(frozen=True, slots=True)
class CompositeExpansion:
    """一次组合调用的完整服务端所有创作投影。"""

    invocation_node: Mapping[str, Any] | None
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    target_mappings: Mapping[str, tuple[Mapping[str, str], ...]]
    source_mappings: Mapping[str, Mapping[str, str]]
    structural_mappings: Mapping[str, tuple[Mapping[str, str], ...]]
    node_templates: tuple[Mapping[str, Any], ...]
    handle_templates: tuple[Mapping[str, Any], ...]
    contract_pin: Mapping[str, Any]
    effective_parent_input_contract: Mapping[str, Any]
    diagnostics: tuple[Mapping[str, str], ...]


class _CompositeFailure(RuntimeError):
    """把内部失败收敛成稳定诊断而不泄漏快照内容。"""

    def __init__(self, code: str, path: str) -> None:
        """保存公共错误码和 JSON Pointer 路径。

        参数：``code`` 是稳定诊断码，``path`` 是失败位置。返回：无。异常：无；
        构造器只保存已判定结果。
        """

        self.code = code
        self.path = path
        super().__init__(code)


class CompositeAuthoring:
    """通过一个只读入口把已发布子工作流展开为父候选图。"""

    def __init__(
        self,
        *,
        snapshot_provider: PublishedWorkflowSnapshotProvider,
        catalog: AuthoringCatalogSnapshot,
        resolver: PublishedWorkflowResolver,
    ) -> None:
        """绑定只读快照、不可变模板目录和静态来源解析器。

        参数：三个依赖分别提供图事实、模板代际和 package 来源身份。返回：无。
        异常：接口形状不合法时抛出 ``TypeError``；构造时不读取或写入外部状态。
        """

        if not callable(
            getattr(snapshot_provider, "get_published_workflow_snapshot", None)
        ):
            raise TypeError("snapshot_provider 必须实现已发布快照读取端口")
        if not isinstance(catalog, AuthoringCatalogSnapshot):
            raise TypeError("catalog 必须是 AuthoringCatalogSnapshot")
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver 必须实现已发布来源解析端口")
        self._snapshot_provider = snapshot_provider
        self._catalog = catalog
        self._resolver = resolver

    def compile_invocation(
        self,
        *,
        parent_workflow_uuid: str,
        invocation_uuid: str,
        module: str,
        symbol: str,
        keyword_arguments: Mapping[str, object],
        parent_input_contract: Mapping[str, object] | None = None,
    ) -> CompositeExpansion:
        """只读编译一个组合工作流调用并把失败转换为零写诊断。

        参数：父工作流/调用 UUID 决定身份；模块和符号选择已发布子工作流；关键字
        参数绑定其输入边界；``parent_input_contract`` 预留给 R3 有效约束传播。
        返回：成功时包含调用节点、平面内部图、映射和 pin，失败时只含一个稳定
        诊断。异常：非领域编程错误不吞并；该接口没有写端口。
        """

        try:
            parent_uuid = _canonical_uuid(
                parent_workflow_uuid,
                "composite_boundary_mapping_invalid",
                "/parent_workflow_uuid",
            )
            invocation = _canonical_uuid(
                invocation_uuid,
                "composite_boundary_mapping_invalid",
                "/invocation_uuid",
            )
            if not isinstance(keyword_arguments, Mapping) or any(
                not isinstance(key, str) for key in keyword_arguments
            ):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/keyword_arguments",
                )
            try:
                source = self._resolver.resolve(module, symbol)
            except (LookupError, PublishedSourceCatalogError):
                raise _CompositeFailure("composite_child_not_found", "/source") from None
            if not isinstance(source, PublishedWorkflowSource):
                raise _CompositeFailure("composite_catalog_mismatch", "/source")
            if source.workflow_uuid == parent_uuid:
                raise _CompositeFailure(
                    "composite_recursive_reference",
                    "/composite/child_workflow_uuid",
                )
            try:
                snapshot = self._snapshot_provider.get_published_workflow_snapshot(
                    source.workflow_uuid
                )
            except LookupError:
                raise _CompositeFailure(
                    "composite_child_not_found",
                    "/child/workflow_uuid",
                ) from None
            return self._compile_snapshot(
                parent_workflow_uuid=parent_uuid,
                invocation_uuid=invocation,
                invocation_parent_uuid=None,
                source=source,
                keyword_arguments=dict(keyword_arguments),
                snapshot=snapshot,
                workflow_stack=(parent_uuid,),
                base_node=None,
                parent_input_contract=parent_input_contract,
            )
        except _CompositeFailure as error:
            return _failed_expansion(error.code, error.path)

    def _compile_snapshot(
        self,
        *,
        parent_workflow_uuid: str,
        invocation_uuid: str,
        invocation_parent_uuid: str | None,
        source: PublishedWorkflowSource,
        keyword_arguments: dict[str, object],
        snapshot: Mapping[str, Any],
        workflow_stack: tuple[str, ...],
        base_node: Mapping[str, Any] | None,
        parent_input_contract: Mapping[str, object] | None,
    ) -> CompositeExpansion:
        """验证一个快照并构造直接子工作流的平面展开结果。

        参数：父/调用/父层级身份决定展开命名空间，``source`` 与 ``snapshot``
        是同一发布来源，关键字参数、工作流栈、基础节点和父输入合同提供递归上下文。
        返回：完整组合展开结果。异常：快照、目录、边界、pin 或递归不安全时抛出
        ``_CompositeFailure``，由公共入口收敛为零写诊断。
        """

        if source.workflow_uuid in workflow_stack:
            raise _CompositeFailure(
                "composite_recursive_reference",
                "/composite/child_workflow_uuid",
            )

        workflow = _mapping(snapshot.get("workflow"), "/child/workflow")
        if workflow.get("uuid") != source.workflow_uuid:
            raise _CompositeFailure("composite_catalog_mismatch", "/child/workflow/uuid")
        revision = workflow.get("revision")
        applied_source = snapshot.get("applied_source")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(applied_source, Mapping)
            or applied_source.get("workflow_revision") != revision
        ):
            raise _CompositeFailure("composite_child_unapplied", "/child/applied_source")
        template_action, extension = _published_template(
            self._catalog,
            source,
            revision=revision,
            applied_source_hash=applied_source.get("source_hash"),
        )
        graph = {
            "workflow": _plain(workflow),
            "nodes": _sequence(snapshot.get("nodes"), "/child/nodes"),
            "edges": _sequence(snapshot.get("edges"), "/child/edges"),
            "node_templates": _sequence(
                snapshot.get("node_templates"),
                "/child/node_templates",
            ),
            "handle_templates": _sequence(
                snapshot.get("handle_templates"),
                "/child/handle_templates",
            ),
        }
        try:
            workflow_io = validate_workflow_graph_io(graph)
        except (WorkflowIOValidationError, KeyError, TypeError, ValueError):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/io_contract",
            ) from None
        input_contract = workflow_io.input_contract.to_dict()
        output_contract = workflow_io.output_contract.to_dict()
        normalized_arguments = _normalize_arguments(
            input_contract,
            keyword_arguments,
        )
        nodes, edges, node_uuid_map, effective_child_input_contract = (
            self._expand_graph(
            graph,
            source=source,
            invocation_uuid=invocation_uuid,
            parent_workflow_uuid=parent_workflow_uuid,
            workflow_stack=workflow_stack,
            input_contract=input_contract,
            )
        )
        boundary_handles = template_action.handles
        target_mappings = _target_mappings(
            input_contract,
            workflow_io.input_bindings,
            boundary_handles,
            node_uuid_map,
        )
        _materialize_boundary_arguments(
            nodes,
            target_mappings=target_mappings,
            boundary_handles=boundary_handles,
            keyword_arguments=normalized_arguments,
            catalog=self._catalog,
        )
        source_mappings = _source_mappings(
            output_contract,
            workflow_io.output_bindings,
            boundary_handles,
            node_uuid_map,
        )
        structural = _structural_mappings(
            nodes,
            edges,
            catalog=self._catalog,
        )
        contract_pin = {
            "child_workflow_uuid": source.workflow_uuid,
            "child_workflow_revision": revision,
            "child_applied_source_hash": str(applied_source["source_hash"]),
            "contract_digest": str(extension["contract_digest"]),
            "composition_allow_transparent": bool(
                extension["composition_allow_transparent"]
            ),
        }
        try:
            contract_compatibility = published_workflow_compatibility_projection(
                template_action.template,
                boundary_handles,
            )
        except (KeyError, TypeError, ValueError):
            raise _CompositeFailure(
                "composite_catalog_mismatch",
                "/catalog/compatibility",
            ) from None
        invocation_node = _invocation_node(
            parent_workflow_uuid=parent_workflow_uuid,
            invocation_uuid=invocation_uuid,
            parent_uuid=invocation_parent_uuid,
            template_uuid=str(template_action.template["uuid"]),
            symbol=source.symbol,
            keyword_arguments=normalized_arguments,
            contract_pin=contract_pin,
            contract_compatibility=contract_compatibility,
            target_mappings=target_mappings,
            source_mappings=source_mappings,
            structural_mappings=structural,
            base_node=base_node,
        )
        referenced_nodes, referenced_handles = _referenced_templates(
            self._catalog,
            template_action=template_action,
            nodes=nodes,
        )
        _reject_private_providers(normalized_arguments, nodes)
        effective_parent_input_contract = (
            effective_child_input_contract
            if parent_input_contract is None
            else _effective_parent_input_contract(
                parent_input_contract,
                effective_child_input_contract,
                normalized_arguments,
            )
        )
        return CompositeExpansion(
            invocation_node=invocation_node,
            nodes=tuple(nodes),
            edges=tuple(edges),
            target_mappings={key: tuple(value) for key, value in target_mappings.items()},
            source_mappings=source_mappings,
            structural_mappings={key: tuple(value) for key, value in structural.items()},
            node_templates=tuple(referenced_nodes),
            handle_templates=tuple(referenced_handles),
            contract_pin=contract_pin,
            effective_parent_input_contract=effective_parent_input_contract,
            diagnostics=(),
        )

    def _expand_graph(
        self,
        graph: Mapping[str, Any],
        *,
        source: PublishedWorkflowSource,
        invocation_uuid: str,
        parent_workflow_uuid: str,
        workflow_stack: tuple[str, ...],
        input_contract: Mapping[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, str],
        dict[str, Any],
    ]:
        """递归复制直接节点，并把嵌套调用收敛到同一父候选图。

        参数：``graph`` 是已验证子快照，其他身份决定层级与环检测。返回：完整
        节点、边和直接子节点 UUID 映射。异常：目录、pin、父关系或递归不安全时
        抛出稳定组合失败；只通过只读端口取得下一层快照。
        """

        raw_nodes = [_mapping(item, "/child/nodes") for item in graph["nodes"]]
        by_uuid: dict[str, dict[str, Any]] = {}
        templates: dict[str, AuthoringCatalogAction] = {}
        for node in raw_nodes:
            node_uuid = _canonical_uuid(
                node.get("uuid"),
                "composite_boundary_mapping_invalid",
                "/child/nodes/uuid",
            )
            if node_uuid in by_uuid:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/uuid",
                )
            try:
                action = self._catalog.require_template(
                    str(node["workflow_node_template_uuid"])
                )
            except (AuthoringCatalogError, KeyError):
                raise _CompositeFailure(
                    "composite_catalog_mismatch",
                    "/child/nodes/template",
                ) from None
            by_uuid[node_uuid] = node
            templates[node_uuid] = action
        _validate_parent_tree(by_uuid)
        node_uuid_map = {
            node_uuid: expanded_node_uuid(invocation_uuid, node_uuid)
            for node_uuid in by_uuid
        }
        nodes: list[dict[str, Any]] = []
        nested_edges: list[dict[str, Any]] = []
        effective_input_contract = _plain(input_contract)
        next_stack = (*workflow_stack, source.workflow_uuid)
        for node_uuid in sorted(by_uuid):
            node = by_uuid[node_uuid]
            mapped_uuid = node_uuid_map[node_uuid]
            raw_parent = node.get("parent_uuid")
            mapped_parent = (
                invocation_uuid
                if raw_parent is None
                else node_uuid_map.get(str(raw_parent))
            )
            if mapped_parent is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
            action = templates[node_uuid]
            if action.template.get("node_type") != "workflow":
                copied = _plain(node)
                copied["uuid"] = mapped_uuid
                copied["parent_uuid"] = mapped_parent
                nodes.append(copied)
                continue
            nested_source = _source_from_template(self._resolver, action)
            if nested_source.workflow_uuid in next_stack:
                raise _CompositeFailure(
                    "composite_recursive_reference",
                    "/composite/child_workflow_uuid",
                )
            try:
                nested_snapshot = (
                    self._snapshot_provider.get_published_workflow_snapshot(
                        nested_source.workflow_uuid
                    )
                )
            except LookupError:
                raise _CompositeFailure(
                    "composite_child_not_found",
                    "/child/workflow_uuid",
                ) from None
            nested = self._compile_snapshot(
                parent_workflow_uuid=parent_workflow_uuid,
                invocation_uuid=mapped_uuid,
                invocation_parent_uuid=mapped_parent,
                source=nested_source,
                keyword_arguments=_node_keyword_arguments(node),
                snapshot=nested_snapshot,
                workflow_stack=next_stack,
                base_node=node,
                parent_input_contract=effective_input_contract,
            )
            if nested.invocation_node is None:
                raise _CompositeFailure(
                    "composite_catalog_mismatch",
                    "/child/nodes/composite",
                )
            _assert_nested_pin(
                node,
                nested.invocation_node,
                previous_templates=graph["node_templates"],
                previous_handles=graph["handle_templates"],
            )
            nodes.append(_plain(nested.invocation_node))
            nodes.extend(_plain(nested.nodes))
            nested_edges.extend(_plain(nested.edges))
            effective_input_contract = _plain(
                nested.effective_parent_input_contract
            )
        direct_edges = _expand_edges(
            graph["edges"],
            node_uuid_map=node_uuid_map,
            parent_workflow_uuid=parent_workflow_uuid,
        )
        edges = _unique_edges([*direct_edges, *nested_edges])
        _assert_acyclic(nodes, edges)
        return nodes, edges, node_uuid_map, effective_input_contract


def _published_template(
    catalog: AuthoringCatalogSnapshot,
    source: PublishedWorkflowSource,
    *,
    revision: int,
    applied_source_hash: Any,
) -> tuple[AuthoringCatalogAction, dict[str, Any]]:
    """取得并鉴别与来源、修订和应用哈希一致的工作流模板。

    参数：``catalog`` 是冻结目录，``source`` 是发布来源，``revision`` 与
    ``applied_source_hash`` 是快照 pin。返回：目录聚合及其合同扩展。异常：模板
    缺失、重复、来源或 pin 不一致时抛出 ``_CompositeFailure``。
    """

    matches = [
        action
        for action in catalog.actions
        if action.template.get("type") == "workflow"
        and action.template.get("node_type") == "workflow"
        and action.template.get("class") == f"{source.module}:{source.symbol}"
    ]
    if len(matches) != 1:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/workflow")
    action = matches[0]
    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    provenance = (
        unilab.get("workflow_source") if isinstance(unilab, Mapping) else None
    )
    expected_provenance = {
        "kind": "package",
        "definition_fqid": source.definition_fqid,
        "module": source.module,
        "symbol": source.symbol,
        "package_catalog_digest": source.package_catalog_digest,
        "definition_content_hash": source.definition_content_hash,
    }
    if not isinstance(provenance, Mapping) or _plain(provenance) != expected_provenance:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/provenance")
    schema = _schema_object(template.get("schema"))
    extension = schema.get("x-unilabos-workflow-contract")
    if (
        not isinstance(extension, Mapping)
        or extension.get("version") != 1
        or extension.get("workflow_uuid") != source.workflow_uuid
        or extension.get("workflow_revision") != revision
        or extension.get("applied_source_hash") != applied_source_hash
    ):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/contract")
    return action, _plain(extension)


def _normalize_arguments(
    input_contract: Mapping[str, Any],
    keyword_arguments: Mapping[str, object],
) -> dict[str, object]:
    """核对关键字边界覆盖并补入合同默认值。

    参数：``input_contract`` 是规范输入合同，``keyword_arguments`` 是作者实参。
    返回：按合同顺序补齐默认值的独立实参字典。异常：字段、覆盖、必填或额外
    参数不合法时抛出 ``_CompositeFailure``。
    """

    parameters = input_contract.get("parameters")
    if not isinstance(parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/input_contract",
        )
    descriptors = {str(item["name"]): item for item in parameters}
    if set(keyword_arguments) - set(descriptors):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/keyword_arguments",
        )
    normalized = dict(keyword_arguments)
    for name, descriptor in descriptors.items():
        if name in normalized:
            continue
        if "default" in descriptor:
            normalized[name] = _plain(descriptor["default"])
        elif descriptor.get("required") is True:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )
    return normalized


def _source_from_template(
    resolver: PublishedWorkflowResolver,
    action: AuthoringCatalogAction,
) -> PublishedWorkflowSource:
    """从封闭工作流模板取得绝对导入身份并解析冻结来源。

    参数：``resolver`` 是只读来源端口，``action`` 是封闭工作流模板聚合。返回：
    唯一发布来源。异常：类身份无效、来源缺失或解析结果类型错误时抛出
    ``_CompositeFailure``。
    """

    class_name = action.template.get("class")
    if not isinstance(class_name, str) or class_name.count(":") != 1:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/class")
    module, symbol = class_name.split(":", 1)
    try:
        source = resolver.resolve(module, symbol)
    except (LookupError, PublishedSourceCatalogError):
        raise _CompositeFailure("composite_child_not_found", "/source") from None
    if not isinstance(source, PublishedWorkflowSource):
        raise _CompositeFailure("composite_catalog_mismatch", "/source")
    return source


def _node_keyword_arguments(node: Mapping[str, Any]) -> dict[str, object]:
    """复制已应用组合节点保存的边界参数。

    参数：``node`` 是已应用组合调用节点。返回：字符串键的独立参数字典。异常：
    参数不是映射或含非字符串键时抛出 ``_CompositeFailure``。
    """

    arguments = node.get("param")
    if not isinstance(arguments, Mapping) or any(
        not isinstance(key, str) for key in arguments
    ):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/param",
        )
    return _plain(arguments)


def _assert_nested_pin(
    previous_node: Mapping[str, Any],
    current_node: Mapping[str, Any],
    *,
    previous_templates: Sequence[Mapping[str, Any]],
    previous_handles: Sequence[Mapping[str, Any]],
) -> None:
    """认证旧嵌套投影并拒绝破坏性发布合同演进。

    参数：新旧调用节点与旧子图的模板/连接点全集。返回：精确或可加演进时无。
    异常：投影被篡改、混代或破坏性变化时抛出稳定目录不匹配。
    """

    compatibility = classify_pinned_published_workflow_invocation(
        previous_node=previous_node,
        current_node=current_node,
        previous_templates=previous_templates,
        previous_handles=previous_handles,
    )
    if compatibility == "breaking":
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/child/nodes/composite/contract_compatibility",
        )


def _validate_parent_tree(nodes: Mapping[str, Mapping[str, Any]]) -> None:
    """验证直接子图父引用存在且层级无环。

    参数：``nodes`` 是按 UUID 索引的直接子图。返回：无。异常：父引用不存在或
    父层级成环时抛出 ``_CompositeFailure``。
    """

    for node_uuid, node in nodes.items():
        seen = {node_uuid}
        parent = node.get("parent_uuid")
        while parent is not None:
            parent_uuid = str(parent)
            if parent_uuid not in nodes:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
            if parent_uuid in seen:
                raise _CompositeFailure(
                    "composite_recursive_reference",
                    "/child/nodes/parent_uuid",
                )
            seen.add(parent_uuid)
            parent = nodes[parent_uuid].get("parent_uuid")


def _unique_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 UUID 去重完整展开边并拒绝身份碰撞。

    参数：``edges`` 是直接与递归展开边。返回：按 UUID 排序的独立边列表。
    异常：相同 UUID 对应不同边事实时抛出 ``_CompositeFailure``。
    """

    result: dict[str, dict[str, Any]] = {}
    for raw in edges:
        edge = _plain(raw)
        edge_uuid = str(edge.get("uuid"))
        existing = result.get(edge_uuid)
        if existing is not None and existing != edge:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/uuid",
            )
        result[edge_uuid] = edge
    return [result[key] for key in sorted(result)]


def _assert_acyclic(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> None:
    """验证展开后的完整业务边图仍为有向无环图。

    参数：``nodes`` 与 ``edges`` 是完整展开图。返回：无。异常：边引用图外节点
    或图成环时抛出 ``_CompositeFailure``。
    """

    node_uuids = {str(node["uuid"]) for node in nodes}
    incoming = {node_uuid: 0 for node_uuid in node_uuids}
    outgoing: dict[str, list[str]] = {node_uuid: [] for node_uuid in node_uuids}
    for edge in edges:
        source = str(edge["source_node_uuid"])
        target = str(edge["target_node_uuid"])
        if source not in node_uuids or target not in node_uuids:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/node",
            )
        outgoing[source].append(target)
        incoming[target] += 1
    ready = sorted(key for key, degree in incoming.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for target in sorted(outgoing[current]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if visited != len(node_uuids):
        raise _CompositeFailure(
            "composite_recursive_reference",
            "/child/edges/cycle",
        )


def _expand_edges(
    raw_edges: Sequence[Any],
    *,
    node_uuid_map: Mapping[str, str],
    parent_workflow_uuid: str,
) -> list[dict[str, Any]]:
    """复制内部边并按父工作流创作边规则重算身份。

    参数：``raw_edges`` 是子图边，``node_uuid_map`` 是展开身份映射，
    ``parent_workflow_uuid`` 是父图命名空间。返回：重算身份后的独立边列表。
    异常：字段、端点、方向或重复身份无效时抛出 ``_CompositeFailure``。
    """

    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_edges:
        edge = _mapping(raw, "/child/edges")
        source_node = node_uuid_map.get(str(edge.get("source_node_uuid")))
        target_node = node_uuid_map.get(str(edge.get("target_node_uuid")))
        if source_node is None or target_node is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/node",
            )
        source_handle = _canonical_uuid(
            edge.get("source_handle_uuid"),
            "composite_boundary_mapping_invalid",
            "/child/edges/source_handle_uuid",
        )
        target_handle = _canonical_uuid(
            edge.get("target_handle_uuid"),
            "composite_boundary_mapping_invalid",
            "/child/edges/target_handle_uuid",
        )
        edge_uuid = authoring_edge_uuid(
            workflow_uuid=parent_workflow_uuid,
            source_node_uuid=source_node,
            source_handle_uuid=source_handle,
            target_node_uuid=target_node,
            target_handle_uuid=target_handle,
        )
        if edge_uuid in seen:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges",
            )
        seen.add(edge_uuid)
        edges.append(
            {
                "uuid": edge_uuid,
                "source_node_uuid": source_node,
                "target_node_uuid": target_node,
                "source_handle_uuid": source_handle,
                "target_handle_uuid": target_handle,
                "description": edge.get("description"),
                "meta_data": _plain(edge.get("meta_data") or {}),
            }
        )
    return sorted(edges, key=lambda item: item["uuid"])


def _target_mappings(
    input_contract: Mapping[str, Any],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, list[dict[str, str]]]:
    """把工作流输入绑定投影到真实展开节点目标连接点。

    参数：输入合同、节点输入绑定、边界连接点与节点身份映射来自同一快照。返回：
    按边界连接点索引的内部目标列表。异常：覆盖、节点或连接点身份无效时抛出
    ``_CompositeFailure``。
    """

    result: dict[str, list[dict[str, str]]] = {}
    for descriptor in input_contract["parameters"]:
        name = str(descriptor["name"])
        boundary_uuid = _boundary_handle_uuid(boundary_handles, name, "target")
        targets: list[dict[str, str]] = []
        for child_node_uuid, bindings in input_bindings.items():
            for handle_uuid, binding in bindings.items():
                if binding.get("parameter") == name:
                    targets.append(
                        {
                            "workflow_node_uuid": node_uuid_map[child_node_uuid],
                            "target_handle_uuid": handle_uuid,
                        }
                    )
        if not targets:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/target_mappings/{name}",
            )
        result[boundary_uuid] = sorted(
            targets,
            key=lambda item: (
                item["workflow_node_uuid"],
                item["target_handle_uuid"],
            ),
        )
    return dict(sorted(result.items()))


def _source_mappings(
    output_contract: Mapping[str, Any],
    output_bindings: Mapping[str, Mapping[str, str]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """把工作流输出绑定投影到展开节点输出或父输入。

    参数：输出合同、输出绑定、边界连接点与节点身份映射来自同一快照。返回：
    按边界连接点索引的来源绑定。异常：输出节点或边界身份无效时抛出
    ``_CompositeFailure``。
    """

    result: dict[str, dict[str, str]] = {}
    for descriptor in output_contract["outputs"]:
        name = str(descriptor["name"])
        boundary_uuid = _boundary_handle_uuid(boundary_handles, name, "source")
        binding = dict(output_bindings[name])
        if binding.get("kind") == "node_output":
            child_node_uuid = binding.get("workflow_node_uuid")
            mapped_node = node_uuid_map.get(str(child_node_uuid))
            if mapped_node is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/source_mappings/{name}",
                )
            binding["workflow_node_uuid"] = mapped_node
        result[boundary_uuid] = {str(key): str(value) for key, value in binding.items()}
    return dict(sorted(result.items()))


def _structural_mappings(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, list[dict[str, str]]]:
    """从平面有向无环图根/终点投影 ready 结构映射。

    参数：``nodes`` 与 ``edges`` 是已展开子图，``catalog`` 是冻结创作目录。
    返回：仅覆盖可执行节点的入口目标与完成来源映射。异常：节点模板缺失或可执行
    根/终点没有唯一 ready 连接点（Handle）时抛出 ``_CompositeFailure``。
    """

    by_uuid = {str(node["uuid"]): node for node in nodes}
    node_ids: set[str] = set()
    for node_uuid, node in by_uuid.items():
        try:
            action = catalog.require_template(
                str(node["workflow_node_template_uuid"])
            )
        except (AuthoringCatalogError, KeyError):
            raise _CompositeFailure(
                "composite_catalog_mismatch",
                "/catalog/structural",
            ) from None
        if (
            node.get("type") == "group"
            or action.template.get("node_type") == "group"
        ):
            continue
        node_ids.add(node_uuid)
    incoming = {str(edge["target_node_uuid"]) for edge in edges}
    outgoing = {str(edge["source_node_uuid"]) for edge in edges}
    entries = [
        {
            "workflow_node_uuid": node_uuid,
            "target_handle_uuid": _ready_handle_uuid(
                catalog,
                by_uuid[node_uuid],
                "target",
            ),
        }
        for node_uuid in sorted(node_ids - incoming)
    ]
    completions = [
        {
            "workflow_node_uuid": node_uuid,
            "source_handle_uuid": _ready_handle_uuid(
                catalog,
                by_uuid[node_uuid],
                "source",
            ),
        }
        for node_uuid in sorted(node_ids - outgoing)
    ]
    return {"entry_targets": entries, "completion_sources": completions}


def _ready_handle_uuid(
    catalog: AuthoringCatalogSnapshot,
    node: Mapping[str, Any],
    io_type: str,
) -> str:
    """取得一个内部节点模板唯一 ready 连接点身份。

    参数：``catalog`` 是冻结目录，``node`` 是内部节点，``io_type`` 是方向。
    返回：唯一 ready 连接点 UUID。异常：模板缺失或连接点不唯一时抛出
    ``_CompositeFailure``。
    """

    try:
        action = catalog.require_template(str(node["workflow_node_template_uuid"]))
    except (AuthoringCatalogError, KeyError):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/ready") from None
    matches = [
        handle
        for handle in action.handles
        if handle.get("handle_key") == "ready" and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/ready")
    return str(matches[0]["uuid"])


def _boundary_handle_uuid(
    handles: Sequence[Mapping[str, Any]],
    name: str,
    io_type: str,
) -> str:
    """取得发布工作流边界唯一业务连接点身份。

    参数：``handles`` 是边界全集，``name`` 是业务键，``io_type`` 是方向。返回：
    唯一边界连接点 UUID。异常：缺失或不唯一时抛出 ``_CompositeFailure``。
    """

    matches = [
        handle
        for handle in handles
        if handle.get("handle_key") == name and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            f"/boundary/{name}",
        )
    return str(matches[0]["uuid"])


def _invocation_node(
    *,
    parent_workflow_uuid: str,
    invocation_uuid: str,
    parent_uuid: str | None,
    template_uuid: str,
    symbol: str,
    keyword_arguments: Mapping[str, object],
    contract_pin: Mapping[str, Any],
    contract_compatibility: Mapping[str, Any],
    target_mappings: Mapping[str, Sequence[Mapping[str, str]]],
    source_mappings: Mapping[str, Mapping[str, str]],
    structural_mappings: Mapping[str, Sequence[Mapping[str, str]]],
    base_node: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """构造父图中真实存在但不拥有运行时任务权威的调用节点。

    参数：父/调用/父层级/模板身份与 ``symbol`` 定义节点；实参、pin、兼容性及
    三类映射形成组合元数据；``base_node`` 可保留已应用展示形状。返回：独立调用
    节点字典。异常：无；调用方已认证全部输入。
    """

    composite = {
        "version": 1,
        **_plain(contract_pin),
        "contract_compatibility": _plain(contract_compatibility),
        "target_mappings": _plain(target_mappings),
        "source_mappings": _plain(source_mappings),
        "structural_mappings": _plain(structural_mappings),
    }
    result = _plain(base_node) if base_node is not None else {}
    meta_data = result.get("meta_data")
    meta_data = _plain(meta_data) if isinstance(meta_data, Mapping) else {}
    unilab = meta_data.get("unilab")
    unilab = _plain(unilab) if isinstance(unilab, Mapping) else {}
    unilab["composite"] = composite
    meta_data["unilab"] = unilab
    result.update({
        "uuid": invocation_uuid,
        "workflow_uuid": parent_workflow_uuid,
        "workflow_node_template_uuid": template_uuid,
        "parent_uuid": parent_uuid,
        "name": str(result.get("name") or symbol),
        "status": "idle",
        "type": "workflow",
        "pose": _plain(result.get("pose") or {}),
        "param": _plain(keyword_arguments),
        "execution_policy": _plain(result.get("execution_policy") or {}),
        "disabled": bool(result.get("disabled", False)),
        "minimized": bool(result.get("minimized", False)),
        "meta_data": meta_data,
    })
    return result


def _materialize_boundary_arguments(
    nodes: Sequence[dict[str, Any]],
    *,
    target_mappings: Mapping[str, Sequence[Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    keyword_arguments: Mapping[str, object],
    catalog: AuthoringCatalogSnapshot,
) -> None:
    """把边界默认值和父输入引用下推到平面内部节点。

    参数：展开节点、边界到内部目标映射、边界连接点、规范实参和当前目录。
    返回：原地更新本次新建的分离节点；不写外部状态。异常：连接点身份或节点
    模板不属于同一目录代际时抛出稳定组合失败。
    """

    node_by_uuid = {str(node["uuid"]): node for node in nodes}
    boundary_by_name = {
        str(handle["handle_key"]): str(handle["uuid"])
        for handle in boundary_handles
        if handle.get("io_type") == "target"
        and handle.get("handle_key") != "ready"
    }
    for name, value in keyword_arguments.items():
        boundary_uuid = boundary_by_name.get(name)
        if boundary_uuid is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )
        for target in target_mappings.get(boundary_uuid, ()):
            node = node_by_uuid.get(str(target.get("workflow_node_uuid")))
            target_uuid = str(target.get("target_handle_uuid") or "")
            if node is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/target_mappings/{name}",
                )
            try:
                action = catalog.require_template(
                    str(node["workflow_node_template_uuid"])
                )
            except (AuthoringCatalogError, KeyError):
                raise _CompositeFailure(
                    "composite_catalog_mismatch",
                    f"/target_mappings/{name}",
                ) from None
            handles = [
                handle
                for handle in action.handles
                if str(handle.get("uuid")) == target_uuid
                and handle.get("io_type") == "target"
            ]
            if len(handles) != 1 or not isinstance(handles[0].get("data_key"), str):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/target_mappings/{name}",
                )
            meta_data = node.setdefault("meta_data", {})
            unilab = meta_data.setdefault("unilab", {})
            bindings = unilab.setdefault("input_bindings", {})
            if not isinstance(bindings, dict):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/target_mappings/{name}",
                )
            if (
                isinstance(value, Mapping)
                and value.get("kind") == "workflow_input"
                and isinstance(value.get("parameter"), str)
            ):
                bindings[target_uuid] = {"parameter": str(value["parameter"])}
                continue
            if isinstance(value, Mapping) and value.get("kind") == "node_output":
                # 节点输出仍由父调用边界和 ``target_mappings`` 表达；执行计划
                # （ExecutionPlan）在 F07 冻结时完成平面来源替换。子快照中指向
                # 其自身工作流参数的旧绑定必须移除，否则父图通用 I/O
                # 校验会把子边界名误解为父工作流参数。
                bindings.pop(target_uuid, None)
                continue
            bindings.pop(target_uuid, None)
            param = node.setdefault("param", {})
            if not isinstance(param, dict):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/target_mappings/{name}",
                )
            param[str(handles[0]["data_key"])] = _plain(value)


def _referenced_templates(
    catalog: AuthoringCatalogSnapshot,
    *,
    template_action: AuthoringCatalogAction,
    nodes: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回调用节点和内部节点实际引用的去重模板/连接点全集。

    参数：``catalog`` 是冻结目录，``template_action`` 是调用模板，``nodes`` 是
    展开内部节点。返回：按模板身份排序的模板及其连接点全集。异常：节点引用
    目录外模板时抛出 ``_CompositeFailure``。
    """

    actions = {str(template_action.template["uuid"]): template_action}
    for node in nodes:
        try:
            action = catalog.require_template(str(node["workflow_node_template_uuid"]))
        except (AuthoringCatalogError, KeyError):
            raise _CompositeFailure("composite_catalog_mismatch", "/catalog/templates")
        actions[str(action.template["uuid"])] = action
    node_templates = [
        actions[key].detached_template() for key in sorted(actions)
    ]
    handles = [
        handle
        for key in sorted(actions)
        for handle in actions[key].detached_handles()
    ]
    return node_templates, handles


def _effective_parent_input_contract(
    parent_input_contract: Mapping[str, object],
    child_input_contract: Mapping[str, Any],
    keyword_arguments: Mapping[str, object],
) -> dict[str, Any]:
    """沿工作流输入绑定传播物料占位符（ResourceSlot）允许集合交集。

    参数：父/子输入合同与调用实参描述同一边界绑定。返回：更新允许集合后的
    独立父输入合同。异常：合同形状、Schema 可赋值性或集合交集不成立时抛出
    ``_CompositeFailure``。
    """

    effective = _plain(parent_input_contract)
    parameters = effective.get("parameters")
    child_parameters = child_input_contract.get("parameters")
    if not isinstance(parameters, list) or not isinstance(child_parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/parent/input_contract",
        )
    parent_by_name = {
        item.get("name"): item
        for item in parameters
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    child_by_name = {
        item.get("name"): item
        for item in child_parameters
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    for child_name, provider in keyword_arguments.items():
        if (
            not isinstance(provider, Mapping)
            or provider.get("kind") != "workflow_input"
        ):
            continue
        parent_name = provider.get("parameter")
        parent_parameter = parent_by_name.get(parent_name)
        child_parameter = child_by_name.get(child_name)
        if parent_parameter is None or child_parameter is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}",
            )
        parent_schema = parent_parameter.get("schema")
        child_schema = child_parameter.get("schema")
        if not isinstance(parent_schema, Mapping) or not isinstance(
            child_schema,
            Mapping,
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}/schema",
            )
        parent_slot = resource_slot_schema(parent_schema)
        child_slot = resource_slot_schema(child_schema)
        if parent_slot is None and child_slot is None:
            continue
        if parent_slot is None or child_slot is None or not schema_is_assignable(
            _replace_slot_allowlist(parent_schema, None),
            _replace_slot_allowlist(child_schema, None),
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}/schema",
            )
        parent_allowed = _slot_allowlist(parent_slot)
        child_allowed = _slot_allowlist(child_slot)
        if parent_allowed is None:
            intersection = child_allowed
        elif child_allowed is None:
            intersection = parent_allowed
        else:
            intersection = sorted(set(parent_allowed) & set(child_allowed))
            if not intersection:
                raise _CompositeFailure(
                    "composite_resource_constraint_empty",
                    f"/keyword_arguments/{child_name}/schema",
                )
        parent_parameter["schema"] = _replace_slot_allowlist(
            parent_schema,
            intersection,
        )
    return effective


def _slot_allowlist(slot_schema: Mapping[str, Any]) -> list[str] | None:
    """读取规范物料占位符（ResourceSlot）的可选非空 UUID 允许集合。

    参数：``slot_schema`` 是已定位的物料占位符 Schema。返回：排序、去重验证过
    的 UUID 列表，省略约束时为 ``None``。异常：集合为空、重复或身份无效时抛出
    ``_CompositeFailure``。
    """

    raw = slot_schema.get("allowed_resource_template_uuids")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or any(
        not isinstance(item, str) for item in raw
    ):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
    values = [
        _canonical_uuid(
            item,
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
        for item in raw
    ]
    if len(set(values)) != len(values):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
    return sorted(values)


def _replace_slot_allowlist(
    schema: Mapping[str, Any],
    allowlist: list[str] | None,
) -> dict[str, Any]:
    """在保留数组/可空包装的同时替换唯一物料模板允许集合。

    参数：``schema`` 是物料值 Schema，``allowlist`` 是新集合或全集标志。返回：
    保留数组/可空外壳的独立 Schema。异常：无；调用方已认证唯一占位符形状。
    """

    result = _plain(schema)
    if result.get("$slot") == "ResourceSlot":
        if allowlist is None:
            result.pop("allowed_resource_template_uuids", None)
        else:
            result["allowed_resource_template_uuids"] = list(allowlist)
        return result
    items = result.get("items")
    if isinstance(items, Mapping) and resource_slot_schema(items) is not None:
        result["items"] = _replace_slot_allowlist(items, allowlist)
        return result
    members = result.get("anyOf")
    if isinstance(members, list):
        result["anyOf"] = [
            _replace_slot_allowlist(member, allowlist)
            if isinstance(member, Mapping)
            and resource_slot_schema(member) is not None
            else _plain(member)
            for member in members
        ]
    return result


def _reject_private_providers(
    keyword_arguments: Mapping[str, object],
    nodes: Sequence[Mapping[str, Any]],
) -> None:
    """拒绝父参数绕过调用边界引用本次展开的内部节点。

    参数：关键字参数可包含标准来源引用，``nodes`` 是本次展开的私有层级。
    返回：无。异常：命中内部节点来源时抛出稳定组合失败；不修改输入对象。
    """

    private_node_uuids = {str(node["uuid"]) for node in nodes}

    def visit(value: object, path: str) -> None:
        """递归检查一个 JSON 值中的节点输出来源引用。

        参数：``value`` 是当前 JSON 值，``path`` 是诊断路径。返回：无。异常：
        引用本次展开私有节点时抛出 ``_CompositeFailure``。
        """

        if isinstance(value, Mapping):
            if (
                value.get("kind") == "node_output"
                and value.get("workflow_node_uuid") in private_node_uuids
            ):
                raise _CompositeFailure("composite_external_private_edge", path)
            for key, item in value.items():
                visit(item, f"{path}/{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, f"{path}/{index}")

    for name, value in keyword_arguments.items():
        visit(value, f"/keyword_arguments/{name}")


def _failed_expansion(code: str, path: str) -> CompositeExpansion:
    """构造不含任何候选事实的单诊断失败结果。

    参数：``code`` 是稳定错误码，``path`` 是 JSON Pointer。返回：零图事实、
    单诊断的组合展开结果。异常：无。
    """

    return CompositeExpansion(
        invocation_node=None,
        nodes=(),
        edges=(),
        target_mappings={},
        source_mappings={},
        structural_mappings={},
        node_templates=(),
        handle_templates=(),
        contract_pin={},
        effective_parent_input_contract={},
        diagnostics=(
            {
                "code": code,
                "path": path,
                "severity": "error",
                "message": "组合工作流创作合同校验失败",
            },
        ),
    )


def _canonical_uuid(value: Any, code: str, path: str) -> str:
    """校验规范 UUID 并映射为稳定组合诊断。

    参数：``value`` 是身份候选，``code``/``path`` 是失败诊断。返回：规范 UUID。
    异常：身份非法或非规范表示时抛出 ``_CompositeFailure``。
    """

    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise _CompositeFailure(code, path) from None
    if identity != value:
        raise _CompositeFailure(code, path)
    return identity


def _mapping(value: Any, path: str) -> dict[str, Any]:
    """复制必填 JSON 对象并在形状非法时关闭失败。

    参数：``value`` 是对象候选，``path`` 是诊断路径。返回：递归分离字典。
    异常：候选不是映射时抛出 ``_CompositeFailure``。
    """

    if not isinstance(value, Mapping):
        raise _CompositeFailure("composite_boundary_mapping_invalid", path)
    return _plain(value)


def _sequence(value: Any, path: str) -> list[Any]:
    """复制必填 JSON 数组并在形状非法时关闭失败。

    参数：``value`` 是数组候选，``path`` 是诊断路径。返回：递归分离列表。
    异常：候选不是列表时抛出 ``_CompositeFailure``。
    """

    if not isinstance(value, list):
        raise _CompositeFailure("composite_boundary_mapping_invalid", path)
    return _plain(value)


def _schema_object(value: Any) -> dict[str, Any]:
    """把目录中对象或 JSON 文本 Schema 规范为分离字典。

    参数：``value`` 是映射或 JSON 文本 Schema。返回：独立 Schema 字典。异常：
    JSON 无效或解码结果不是对象时抛出 ``_CompositeFailure``。
    """

    if isinstance(value, Mapping):
        return _plain(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    raise _CompositeFailure("composite_catalog_mismatch", "/catalog/schema")


def _plain(value: Any) -> Any:
    """递归复制冻结映射/元组为普通 JSON 容器。

    参数：``value`` 是 JSON 兼容值。返回：容器递归分离后的等价值。异常：无。
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "CompositeAuthoring",
    "CompositeExpansion",
    "PublishedWorkflowResolver",
    "PublishedWorkflowSnapshotProvider",
]
