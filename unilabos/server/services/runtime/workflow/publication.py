"""工作流发布：冻结修订快照、投影为工作流节点模板，以及插入组合调用时的目录装配。

身份 = (workflow_uuid, revision) + 内容摘要 ``source_hash``。发布不经过 Python
authoring：快照里的 ``applied_source`` 只携带 ``{source_hash, workflow_revision}``，
再合成一个包形状的来源身份（``unilabos_publications.wf_<hex>``）交给
``build_published_workflow_generation`` / ``CompositeAuthoring`` 复用既有的投影与展开。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from unilabos.protocol.runtime.workflow import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.protocol.utils.json_codec import encode_json
from unilabos.server.services.runtime.registry import template_uuid as registry_template_uuid
from unilabos.server.services.runtime.workflow.catalog import PublishedWorkflowSource
from unilabos.server.services.runtime.workflow.composite import (
    PublishedWorkflowContractError,
)
from unilabos.server.services.runtime.workflow.composite_expansion import (
    CompositeExpansion,
)
from unilabos.server.services.runtime.workflow.published_workflow_runtime import (
    PublishedWorkflowGenerationError,
    build_published_workflow_generation,
)
from unilabos.server.services.runtime.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

#: 合成来源的模块名：发布的工作流不是 Python 包，但目录/展开按 (module, symbol) 解析。
PUBLICATION_MODULE = "unilabos_publications"
#: 写入 workflow_node_template / workflow_handle_template 的 authority_id。
PUBLICATION_TEMPLATE_AUTHORITY = "workflow_publication"
_VOLATILE_FIELDS = frozenset({"create_time", "update_time", "deleted_at"})


class PublicationError(ValueError):
    """发布或插入不能安全进行；``code`` / ``path`` 与组合诊断同一套取值。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}（{path}）")


@dataclass(frozen=True, slots=True)
class PublicationProjection:
    """一次发布投影出的工作流节点模板（schema 为对象）与来源身份。"""

    source: PublishedWorkflowSource
    template: dict[str, Any]
    handles: tuple[dict[str, Any], ...]
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    contract_digest: str


def publication_symbol(workflow_uuid: str) -> str:
    """由工作流 uuid 派生合成来源的符号名。"""

    return "wf_" + UUID(workflow_uuid).hex


def host_node_resource_template() -> dict[str, str]:
    """已发布工作流模板挂在 host_node 资源模板下（与注册表同一 uuid5 身份）。"""

    return {
        "uuid": registry_template_uuid("host_node"),
        "name": "host_node",
        "display_name": "Host Node",
    }


def publication_source_hash(graph: Mapping[str, Any]) -> str:
    """语义图（去掉时间戳与 revision）的规范 JSON 摘要。"""

    workflow = graph["workflow"]
    payload = {
        "workflow": {
            key: _plain(workflow[key])
            for key in ("uuid", "name", "description", "tags", "meta_data")
            if key in workflow
        },
        "nodes": sorted(
            (_semantic(node, WorkflowNodeWrite) for node in graph["nodes"]),
            key=lambda item: str(item["uuid"]),
        ),
        "edges": sorted(
            (_semantic(edge, WorkflowEdgeWrite) for edge in graph["edges"]),
            key=lambda item: str(item["uuid"]),
        ),
        "node_templates": sorted(
            (_without_volatile(item) for item in graph["node_templates"]),
            key=lambda item: str(item["uuid"]),
        ),
        "handle_templates": sorted(
            (_without_volatile(item) for item in graph["handle_templates"]),
            key=lambda item: str(item["uuid"]),
        ),
    }
    return "sha256:" + hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()


def freeze_publication_snapshot(
    graph: Mapping[str, Any],
    *,
    source_hash: str,
) -> dict[str, Any]:
    """把当前图冻结为发布快照（与 ``get_published_workflow_snapshot`` 同形）。"""

    workflow = _plain(graph["workflow"])
    return {
        "workflow": workflow,
        "nodes": _plain(graph["nodes"]),
        "edges": _plain(graph["edges"]),
        "node_templates": _plain(graph["node_templates"]),
        "handle_templates": _plain(graph["handle_templates"]),
        "applied_source": {
            "source_hash": source_hash,
            "workflow_revision": workflow["revision"],
        },
    }


def project_publication(snapshot: Mapping[str, Any]) -> PublicationProjection:
    """用单条合成注册构造目录代际，得到确定性的工作流节点模板与 Handle。"""

    workflow_uuid = str(snapshot["workflow"]["uuid"])
    symbol = publication_symbol(workflow_uuid)
    registration = {
        "workflow_uuid": workflow_uuid,
        "package_id": PUBLICATION_MODULE,
        "relative_path": f"{PUBLICATION_MODULE}/{symbol}.py",
        "source_uri": f"package://{PUBLICATION_MODULE}/{symbol}.py",
        "module": PUBLICATION_MODULE,
        "symbol": symbol,
        "definition_content_hash": str(snapshot["applied_source"]["source_hash"]),
    }
    try:
        generation = build_published_workflow_generation(
            registrations=[registration],
            snapshot_provider=FrozenSnapshotProvider({workflow_uuid: snapshot}),
            base_node_templates=[
                {"meta_data": {"resource_template": host_node_resource_template()}}
            ],
        )
    except PublishedWorkflowGenerationError as error:
        cause = error.__cause__
        if isinstance(cause, PublishedWorkflowContractError):
            raise PublicationError(cause.code, cause.path) from error
        raise PublicationError("composite_catalog_mismatch", "/published_workflow") from error
    if len(generation.node_templates) != 1 or len(generation.source_catalog.sources) != 1:
        raise PublicationError("composite_catalog_mismatch", "/published_workflow")
    template = _plain(generation.node_templates[0])
    extension = template["schema"]["x-unilabos-workflow-contract"]
    try:
        workflow_io = validate_workflow_graph_io(snapshot)
    except WorkflowIOValidationError as error:
        raise PublicationError(error.code, error.path) from error
    return PublicationProjection(
        source=generation.source_catalog.sources[0],
        template=template,
        handles=tuple(_plain(item) for item in generation.handle_templates),
        inputs=workflow_io.input_contract.to_dict()["parameters"],
        outputs=workflow_io.output_contract.to_dict()["outputs"],
        contract_digest=str(extension["contract_digest"]),
    )


def stored_template(template: Mapping[str, Any]) -> dict[str, Any]:
    """模板的存储/读取形状：schema 以 JSON 文本保存，与 ``/graph`` hydration 一致。"""

    result = _plain(template)
    schema = result.get("schema")
    if schema is not None and not isinstance(schema, str):
        result["schema"] = encode_json(schema, sort_keys=True).decode("utf-8")
    return result


def authoring_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """展开用的子工作流视图：去掉嵌套组合调用已展开的后代节点与相关连线。

    冻结快照里嵌套调用节点和它展开出的叶子同时存在；展开器会按调用节点的
    固定发布修订重新展开，保留叶子会得到两份副本。
    """

    templates = {
        str(item.get("uuid")): item for item in snapshot.get("node_templates", [])
    }
    nodes = {str(item["uuid"]): item for item in snapshot.get("nodes", [])}
    invocations = {
        identity for identity, node in nodes.items() if is_invocation(node, templates)
    }

    def nested(node: Mapping[str, Any]) -> bool:
        seen: set[str] = set()
        parent = node.get("parent_uuid")
        while parent is not None and str(parent) in nodes and str(parent) not in seen:
            identity = str(parent)
            if identity in invocations:
                return True
            seen.add(identity)
            parent = nodes[identity].get("parent_uuid")
        return False

    kept = [node for node in snapshot.get("nodes", []) if not nested(node)]
    kept_ids = {str(node["uuid"]) for node in kept}
    edges = [
        edge
        for edge in snapshot.get("edges", [])
        if str(edge.get("source_node_uuid")) in kept_ids
        and str(edge.get("target_node_uuid")) in kept_ids
    ]
    return {**_plain(snapshot), "nodes": _plain(kept), "edges": _plain(edges)}


def is_invocation(
    node: Mapping[str, Any],
    templates: Mapping[str, Mapping[str, Any]],
) -> bool:
    template = templates.get(str(node.get("workflow_node_template_uuid")))
    if template is not None and template.get("node_type") == "workflow":
        return True
    return isinstance(_unilab(node).get("composite"), Mapping)


def nested_pins(view: Mapping[str, Any]) -> list[tuple[str, int]]:
    """视图中直接组合调用固定的 (子工作流 uuid, 发布修订)。"""

    templates = {str(item.get("uuid")): item for item in view.get("node_templates", [])}
    pins: list[tuple[str, int]] = []
    for node in view.get("nodes", []):
        if not is_invocation(node, templates):
            continue
        composite = _unilab(node).get("composite")
        if not isinstance(composite, Mapping):
            raise PublicationError("composite_catalog_mismatch", "/child/nodes/composite")
        child = composite.get("child_workflow_uuid")
        revision = composite.get("child_workflow_revision")
        if not isinstance(child, str) or isinstance(revision, bool) or not isinstance(
            revision, int
        ):
            raise PublicationError("composite_catalog_mismatch", "/child/nodes/composite")
        pins.append((child, revision))
    return pins


def base_template_uuids(views: Iterable[Mapping[str, Any]]) -> set[str]:
    """视图中非组合节点引用的普通模板 uuid（展开时要从当前目录读取）。"""

    result: set[str] = set()
    for view in views:
        templates = {
            str(item.get("uuid")): item for item in view.get("node_templates", [])
        }
        for node in view.get("nodes", []):
            identity = node.get("workflow_node_template_uuid")
            if identity is not None and not is_invocation(node, templates):
                result.add(str(identity))
    return result


class FrozenSnapshotProvider:
    """按 workflow_uuid 返回本次装配选定的冻结快照。"""

    def __init__(self, snapshots: Mapping[str, Mapping[str, Any]]) -> None:
        self._snapshots = dict(snapshots)

    def get_published_workflow_snapshot(self, workflow_uuid: str) -> Mapping[str, Any]:
        try:
            return self._snapshots[workflow_uuid]
        except KeyError:
            raise LookupError(workflow_uuid) from None


class PublicationResolver:
    """把合成 (module, symbol) 解析为发布来源身份。"""

    def __init__(self, sources: Iterable[PublishedWorkflowSource]) -> None:
        self._by_import = {(item.module, item.symbol): item for item in sources}

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
        try:
            return self._by_import[(module, symbol)]
        except KeyError:
            raise LookupError(f"{module}:{symbol}") from None


def merged_invocation_entities(
    expansion: CompositeExpansion,
    *,
    name: str,
    pose: Mapping[str, Any] | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """把展开结果整理为要并入父图的节点、连线与要落库的已发布模板。

    未提供实参的子工作流输入不在父图里存在：去掉内部节点上指向它的 input_bindings，
    否则父图校验会把它当成父工作流参数。
    """

    assert expansion.invocation_node is not None
    invocation = _plain(expansion.invocation_node)
    invocation["name"] = name
    invocation["pose"] = _plain(pose) if pose else invocation.get("pose") or {}
    nodes = [invocation, *(_plain(item) for item in expansion.nodes)]
    templates = [_plain(item) for item in expansion.node_templates]
    handles = [_plain(item) for item in expansion.handle_templates]
    handles_by_uuid = {str(item["uuid"]): item for item in handles}
    by_uuid = {str(item["uuid"]): item for item in nodes}
    for node in nodes:
        composite = _unilab(node).get("composite")
        if not isinstance(composite, Mapping):
            continue
        provided = set((node.get("param") or {}).keys())
        for boundary_uuid, targets in (composite.get("target_mappings") or {}).items():
            boundary = handles_by_uuid.get(str(boundary_uuid))
            if boundary is None or boundary.get("handle_key") in provided:
                continue
            for target in targets:
                inner = by_uuid.get(str(target.get("workflow_node_uuid")))
                bindings = _unilab(inner).get("input_bindings") if inner else None
                if isinstance(bindings, dict):
                    bindings.pop(str(target.get("target_handle_uuid")), None)
    published = {
        str(item["uuid"]) for item in templates if item.get("node_type") == "workflow"
    }
    return (
        nodes,
        [_plain(item) for item in expansion.edges],
        [item for item in templates if str(item["uuid"]) in published],
        [
            item
            for item in handles
            if str(item.get("workflow_node_template_uuid")) in published
        ],
    )


def publication_summary(
    row: Mapping[str, Any],
    projection: PublicationProjection,
) -> dict[str, Any]:
    """公共 DTO：不含冻结快照正文，附带投影出的节点模板供前端调色板展示。"""

    snapshot_workflow = row["snapshot"]["workflow"]
    result = {
        "uuid": row["uuid"],
        "workflow_uuid": row["workflow_uuid"],
        "revision": row["revision"],
        "name": row["name"],
        "source_hash": row["source_hash"],
        "contract_digest": row["contract_digest"],
        "create_time": row["create_time"],
        "inputs": _plain(projection.inputs),
        "outputs": _plain(projection.outputs),
        "node_template": stored_template(projection.template),
        "handle_templates": [_plain(item) for item in projection.handles],
    }
    if snapshot_workflow.get("description") is not None:
        result["description"] = snapshot_workflow["description"]
    return result


def _semantic(entity: Mapping[str, Any], model: Any) -> dict[str, Any]:
    return model.model_validate(
        {field: entity[field] for field in model.model_fields if field in entity}
    ).model_dump()


def _without_volatile(entity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _plain(value)
        for key, value in entity.items()
        if key not in _VOLATILE_FIELDS
    }


def _unilab(entity: Mapping[str, Any] | None) -> Mapping[str, Any]:
    meta_data = entity.get("meta_data") if isinstance(entity, Mapping) else None
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab if isinstance(unilab, Mapping) else {}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "PUBLICATION_MODULE",
    "PUBLICATION_TEMPLATE_AUTHORITY",
    "FrozenSnapshotProvider",
    "PublicationError",
    "PublicationProjection",
    "PublicationResolver",
    "authoring_view",
    "base_template_uuids",
    "freeze_publication_snapshot",
    "host_node_resource_template",
    "is_invocation",
    "merged_invocation_entities",
    "nested_pins",
    "project_publication",
    "publication_source_hash",
    "publication_summary",
    "publication_symbol",
    "stored_template",
]
