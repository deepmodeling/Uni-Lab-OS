"""CompositeWorkflowInvocation 的只读递归静态展开算法。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from unilabos.server.workflow.authoring_identity import (
    authoring_edge_uuid,
    expanded_node_uuid,
)
from unilabos.server.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.server.workflow.catalog import (
    PublishedSourceCatalogError,
    PublishedWorkflowSource,
)
from unilabos.server.workflow.composite_compatibility import (
    classify_pinned_published_workflow_invocation,
    published_workflow_compatibility_projection,
)
from unilabos.server.workflow.models import validate_uuid
from unilabos.server.workflow.value_schema import (
    WorkflowValueSchemaError,
    schema_is_assignable,
    validate_value,
)
from unilabos.server.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)


class PublishedWorkflowSnapshotProvider(Protocol):
    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Mapping[str, Any]: ...


class PublishedWorkflowResolver(Protocol):
    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource: ...


@dataclass(frozen=True, slots=True)
class CompositeExpansion:
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
    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code)


class CompositeAuthoring:
    """通过只读快照、模板目录和源码目录展开已发布子工作流。"""

    def __init__(
        self,
        *,
        snapshot_provider: PublishedWorkflowSnapshotProvider,
        catalog: AuthoringCatalogSnapshot,
        resolver: PublishedWorkflowResolver,
    ) -> None:
        if not callable(
            getattr(snapshot_provider, "get_published_workflow_snapshot", None)
        ):
            raise TypeError("snapshot_provider 必须实现已发布快照读取端口")
        if not isinstance(catalog, AuthoringCatalogSnapshot):
            raise TypeError("catalog 必须是 AuthoringCatalogSnapshot")
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver 必须实现已发布源码解析端口")
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
            or not isinstance(applied_source.get("source_hash"), str)
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
        normalized_arguments = _normalize_arguments(input_contract, keyword_arguments)
        (
            nodes,
            edges,
            node_uuid_map,
            effective_child_input_contract,
        ) = self._expand_graph(
            graph,
            source=source,
            invocation_uuid=invocation_uuid,
            parent_workflow_uuid=parent_workflow_uuid,
            workflow_stack=workflow_stack,
            input_contract=input_contract,
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
            compatibility = published_workflow_compatibility_projection(
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
            contract_compatibility=compatibility,
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
        effective_parent = (
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
            effective_parent_input_contract=effective_parent,
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
                nested_snapshot = self._snapshot_provider.get_published_workflow_snapshot(
                    nested_source.workflow_uuid
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
            if (
                classify_pinned_published_workflow_invocation(
                    node,
                    action.template,
                    action.handles,
                )
                == "breaking"
            ):
                raise _CompositeFailure(
                    "composite_contract_stale",
                    "/child/nodes/composite/contract_compatibility",
                )
            nodes.append(_plain(nested.invocation_node))
            nodes.extend(_plain(nested.nodes))
            nested_edges.extend(_plain(nested.edges))
            effective_input_contract = _plain(nested.effective_parent_input_contract)
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
    schema = action.template.get("schema")
    extension = (
        schema.get("x-unilabos-workflow-contract")
        if isinstance(schema, Mapping)
        else None
    )
    provenance = _unilab(action.template).get("workflow_source")
    if (
        not isinstance(extension, Mapping)
        or extension.get("workflow_uuid") != source.workflow_uuid
        or extension.get("workflow_revision") != revision
        or extension.get("applied_source_hash") != applied_source_hash
        or not isinstance(provenance, Mapping)
        or provenance.get("module") != source.module
        or provenance.get("symbol") != source.symbol
        or provenance.get("definition_content_hash")
        != source.definition_content_hash
        or provenance.get("package_catalog_digest")
        != source.package_catalog_digest
    ):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/provenance")
    return action, _plain(extension)


def _normalize_arguments(
    input_contract: Mapping[str, Any],
    keyword_arguments: Mapping[str, object],
) -> dict[str, object]:
    descriptors = {
        str(item["name"]): item for item in input_contract.get("parameters", [])
    }
    if set(keyword_arguments) - set(descriptors):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/keyword_arguments",
        )
    normalized: dict[str, object] = {}
    for name, descriptor in descriptors.items():
        if name in keyword_arguments:
            value = keyword_arguments[name]
        elif "default" in descriptor:
            value = _plain(descriptor["default"])
        elif descriptor.get("required") is True:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )
        else:
            continue
        if isinstance(value, Mapping) and value.get("kind") in {
            "workflow_input",
            "node_output",
        }:
            kind = value.get("kind")
            if kind == "workflow_input" and (
                set(value) != {"kind", "parameter"}
                or not isinstance(value.get("parameter"), str)
            ):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/keyword_arguments/{name}",
                )
            if kind == "node_output":
                try:
                    if set(value) != {
                        "kind",
                        "workflow_node_uuid",
                        "source_handle_uuid",
                    }:
                        raise ValueError
                    validate_uuid(value["workflow_node_uuid"])
                    validate_uuid(value["source_handle_uuid"])
                except (KeyError, TypeError, ValueError):
                    raise _CompositeFailure(
                        "composite_boundary_mapping_invalid",
                        f"/keyword_arguments/{name}",
                    ) from None
            normalized[name] = _plain(value)
            continue
        try:
            normalized[name] = validate_value(descriptor["schema"], value)
        except WorkflowValueSchemaError:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            ) from None
    return normalized


def _source_from_template(
    resolver: PublishedWorkflowResolver,
    action: AuthoringCatalogAction,
) -> PublishedWorkflowSource:
    provenance = _unilab(action.template).get("workflow_source")
    if not isinstance(provenance, Mapping):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/provenance")
    try:
        source = resolver.resolve(str(provenance["module"]), str(provenance["symbol"]))
    except (KeyError, LookupError, PublishedSourceCatalogError):
        raise _CompositeFailure("composite_child_not_found", "/source") from None
    return source


def _node_keyword_arguments(node: Mapping[str, Any]) -> dict[str, object]:
    value = node.get("param")
    if not isinstance(value, Mapping):
        raise _CompositeFailure("composite_boundary_mapping_invalid", "/child/node/param")
    return {str(key): _plain(item) for key, item in value.items()}


def _target_mappings(
    input_contract: Mapping[str, Any],
    input_bindings: Mapping[str, Sequence[Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for descriptor in input_contract.get("parameters", []):
        name = str(descriptor["name"])
        boundary = _boundary_handle_uuid(boundary_handles, name, "target")
        mappings = []
        for binding in input_bindings.get(name, ()):
            mapped = node_uuid_map.get(str(binding["workflow_node_uuid"]))
            if mapped is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/input_bindings/{name}",
                )
            mappings.append(
                {
                    "workflow_node_uuid": mapped,
                    "target_handle_uuid": str(binding["target_handle_uuid"]),
                }
            )
        result[boundary] = mappings
    return result


def _source_mappings(
    output_contract: Mapping[str, Any],
    output_bindings: Mapping[str, Mapping[str, str]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for descriptor in output_contract.get("outputs", []):
        name = str(descriptor["name"])
        boundary = _boundary_handle_uuid(boundary_handles, name, "source")
        binding = _plain(output_bindings[name])
        if binding.get("kind") == "node_output":
            mapped = node_uuid_map.get(str(binding["workflow_node_uuid"]))
            if mapped is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/output_bindings/{name}",
                )
            binding["workflow_node_uuid"] = mapped
        result[boundary] = binding
    return result


def _structural_mappings(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, list[dict[str, str]]]:
    executable: dict[str, AuthoringCatalogAction] = {}
    for node in nodes:
        try:
            action = catalog.require_template(str(node["workflow_node_template_uuid"]))
        except (AuthoringCatalogError, KeyError):
            raise _CompositeFailure("composite_catalog_mismatch", "/nodes/template") from None
        if action.template.get("node_type") not in {"group", "workflow"}:
            executable[str(node["uuid"])] = action
    incoming = {identity: 0 for identity in executable}
    outgoing = {identity: 0 for identity in executable}
    for edge in edges:
        source = str(edge.get("source_node_uuid") or "")
        target = str(edge.get("target_node_uuid") or "")
        if source in executable and target in executable:
            outgoing[source] += 1
            incoming[target] += 1
    entries = [identity for identity, degree in incoming.items() if degree == 0]
    completions = [identity for identity, degree in outgoing.items() if degree == 0]
    return {
        "entry_targets": [
            {
                "workflow_node_uuid": identity,
                "target_handle_uuid": _ready_handle_uuid(
                    executable[identity].handles,
                    "target",
                ),
            }
            for identity in sorted(entries)
        ],
        "completion_sources": [
            {
                "workflow_node_uuid": identity,
                "source_handle_uuid": _ready_handle_uuid(
                    executable[identity].handles,
                    "source",
                ),
            }
            for identity in sorted(completions)
        ],
    }


def _ready_handle_uuid(
    handles: Sequence[Mapping[str, Any]],
    io_type: str,
) -> str:
    return _boundary_handle_uuid(handles, "ready", io_type)


def _boundary_handle_uuid(
    handles: Sequence[Mapping[str, Any]],
    name: str,
    io_type: str,
) -> str:
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
    result.update(
        {
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
        }
    )
    return result


def _materialize_boundary_arguments(
    nodes: Sequence[dict[str, Any]],
    *,
    target_mappings: Mapping[str, Sequence[Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    keyword_arguments: Mapping[str, object],
    catalog: AuthoringCatalogSnapshot,
) -> None:
    node_by_uuid = {str(node["uuid"]): node for node in nodes}
    boundary_by_name = {
        str(handle["handle_key"]): str(handle["uuid"])
        for handle in boundary_handles
        if handle.get("io_type") == "target" and handle.get("handle_key") != "ready"
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
            action = catalog.require_template(str(node["workflow_node_template_uuid"]))
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
            if isinstance(value, Mapping) and value.get("kind") == "workflow_input":
                bindings[target_uuid] = {"parameter": str(value["parameter"])}
            elif isinstance(value, Mapping) and value.get("kind") == "node_output":
                bindings.pop(target_uuid, None)
            else:
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
    actions = {str(template_action.template["uuid"]): template_action}
    for node in nodes:
        action = catalog.require_template(str(node["workflow_node_template_uuid"]))
        actions[str(action.template["uuid"])] = action
    templates = [actions[key].detached_template() for key in sorted(actions)]
    handles = [
        handle
        for key in sorted(actions)
        for handle in actions[key].detached_handles()
    ]
    return templates, handles


def _effective_parent_input_contract(
    parent_contract: Mapping[str, object],
    child_contract: Mapping[str, Any],
    arguments: Mapping[str, object],
) -> dict[str, Any]:
    parent = _plain(parent_contract)
    parent_parameters = {
        str(item["name"]): item for item in parent.get("parameters", [])
    }
    child_parameters = {
        str(item["name"]): item for item in child_contract.get("parameters", [])
    }
    for child_name, value in arguments.items():
        if not isinstance(value, Mapping) or value.get("kind") != "workflow_input":
            continue
        parent_name = str(value.get("parameter") or "")
        parent_descriptor = parent_parameters.get(parent_name)
        child_descriptor = child_parameters.get(child_name)
        if parent_descriptor is None or child_descriptor is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}",
            )
        parent_schema = parent_descriptor["schema"]
        child_schema = child_descriptor["schema"]
        try:
            if not schema_is_assignable(parent_schema, child_schema):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/keyword_arguments/{child_name}",
                )
        except WorkflowValueSchemaError as error:
            raise _CompositeFailure(error.code, error.path) from error
    return parent


def _expand_edges(
    edges: Sequence[Mapping[str, Any]],
    *,
    node_uuid_map: Mapping[str, str],
    parent_workflow_uuid: str,
) -> list[dict[str, Any]]:
    result = []
    for edge in edges:
        source = node_uuid_map.get(str(edge.get("source_node_uuid")))
        target = node_uuid_map.get(str(edge.get("target_node_uuid")))
        if source is None or target is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges",
            )
        source_handle = str(edge.get("source_handle_uuid") or "")
        target_handle = str(edge.get("target_handle_uuid") or "")
        copied = _plain(edge)
        copied.update(
            {
                "uuid": authoring_edge_uuid(
                    workflow_uuid=parent_workflow_uuid,
                    source_node_uuid=source,
                    source_handle_uuid=source_handle,
                    target_node_uuid=target,
                    target_handle_uuid=target_handle,
                ),
                "source_node_uuid": source,
                "target_node_uuid": target,
            }
        )
        result.append(copied)
    return result


def _validate_parent_tree(nodes: Mapping[str, Mapping[str, Any]]) -> None:
    for node_uuid, node in nodes.items():
        seen = {node_uuid}
        parent = node.get("parent_uuid")
        while parent is not None:
            parent = str(parent)
            if parent not in nodes or parent in seen:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
            seen.add(parent)
            parent = nodes[parent].get("parent_uuid")


def _assert_acyclic(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> None:
    identities = {str(node["uuid"]) for node in nodes}
    indegree = {identity: 0 for identity in identities}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("source_node_uuid") or "")
        target = str(edge.get("target_node_uuid") or "")
        if source in identities and target in identities:
            outgoing[source].append(target)
            indegree[target] += 1
    available = [identity for identity, degree in indegree.items() if degree == 0]
    visited = 0
    while available:
        current = available.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                available.append(target)
    if visited != len(identities):
        raise _CompositeFailure("composite_recursive_reference", "/child/edges")


def _unique_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for edge in edges:
        identity = str(edge.get("uuid") or "")
        if not identity or identity in result:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/uuid",
            )
        result[identity] = _plain(edge)
    return [result[key] for key in sorted(result)]


def _failed_expansion(code: str, path: str) -> CompositeExpansion:
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
        diagnostics=({"severity": "error", "code": code, "path": path},),
    )


def _canonical_uuid(value: Any, code: str, path: str) -> str:
    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise _CompositeFailure(code, path) from None
    if value != identity:
        raise _CompositeFailure(code, path)
    return identity


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _CompositeFailure("composite_catalog_mismatch", path)
    return {str(key): _plain(item) for key, item in value.items()}


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _CompositeFailure("composite_catalog_mismatch", path)
    return [_plain(item) for item in value]


def _unilab(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    meta_data = entity.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab if isinstance(unilab, Mapping) else {}


def _plain(value: Any) -> Any:
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
