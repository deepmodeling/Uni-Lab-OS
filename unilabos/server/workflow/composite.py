"""CompositeWorkflowInvocation 的发布合同公共门面。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from unilabos.server.workflow.catalog import PublishedWorkflowSource
from unilabos.server.workflow.handle_projection import (
    resource_slot_schema,
    structural_ready_handle,
    workflow_handle_type,
)
from unilabos.server.workflow.json_codec import encode_json
from unilabos.server.workflow.models import validate_uuid
from unilabos.server.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PublishedWorkflowContractError(ValueError):
    """已发布工作流合同不能从权威快照安全投影。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowContract:
    template: dict[str, Any]
    handles: tuple[dict[str, Any], ...]


def project_published_workflow_contract(
    *,
    source: PublishedWorkflowSource,
    applied_snapshot: Mapping[str, Any],
    host_node_resource_template: Mapping[str, Any] | None,
) -> PublishedWorkflowContract | None:
    """把同修订已应用工作流投影为封闭目录合同。"""

    if not isinstance(source, PublishedWorkflowSource):
        raise TypeError("source 必须是 PublishedWorkflowSource")
    if not isinstance(applied_snapshot, Mapping):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow",
        )
    workflow = applied_snapshot.get("workflow")
    applied_source = applied_snapshot.get("applied_source")
    if not isinstance(workflow, Mapping):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/workflow",
        )
    workflow_uuid = _uuid(
        workflow.get("uuid"),
        "/published_workflow/workflow/uuid",
    )
    if workflow_uuid != source.workflow_uuid:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/source/workflow_uuid",
        )
    revision = workflow.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/workflow/revision",
        )
    if not isinstance(applied_source, Mapping):
        return None
    if applied_source.get("workflow_revision") != revision:
        return None
    source_hash = _digest(
        applied_source.get("source_hash"),
        "/published_workflow/applied_source/source_hash",
    )
    host = _host_summary(host_node_resource_template)
    graph = {
        "workflow": _plain(workflow),
        "nodes": _array(applied_snapshot.get("nodes"), "/published_workflow/nodes"),
        "edges": _array(applied_snapshot.get("edges"), "/published_workflow/edges"),
        "node_templates": _array(
            applied_snapshot.get("node_templates"),
            "/published_workflow/node_templates",
        ),
        "handle_templates": _array(
            applied_snapshot.get("handle_templates"),
            "/published_workflow/handle_templates",
        ),
    }
    try:
        workflow_io = validate_workflow_graph_io(graph)
    except (WorkflowIOValidationError, KeyError, TypeError, ValueError):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/io_contract",
        ) from None
    inputs = workflow_io.input_contract.to_dict()["parameters"]
    outputs = workflow_io.output_contract.to_dict()["outputs"]
    transparent = _composition_mode(workflow)
    contract_digest = _contract_digest(
        inputs=inputs,
        outputs=outputs,
        composition_allow_transparent=transparent,
    )
    template_uuid = str(
        uuid5(UUID(host["uuid"]), f"unilabos:published-workflow:v1:{workflow_uuid}")
    )
    schema = _workflow_schema(
        inputs=inputs,
        outputs=outputs,
        workflow_uuid=workflow_uuid,
        workflow_revision=revision,
        applied_source_hash=source_hash,
        contract_digest=contract_digest,
        composition_allow_transparent=transparent,
    )
    handles = tuple(
        [
            _value_handle(
                item,
                io_type="target",
                template_uuid=template_uuid,
            )
            for item in inputs
        ]
        + [
            _value_handle(
                item,
                io_type="source",
                template_uuid=template_uuid,
            )
            for item in outputs
        ]
        + [
            _ready_handle("target", template_uuid=template_uuid),
            _ready_handle("source", template_uuid=template_uuid),
        ]
    )
    return PublishedWorkflowContract(
        template={
            "uuid": template_uuid,
            "resource_template_uuid": host["uuid"],
            "name": f"workflow:{workflow_uuid}",
            "display_name": str(workflow.get("name") or source.symbol),
            "description": str(workflow.get("description") or ""),
            "class": f"{source.module}:{source.symbol}",
            "type": "workflow",
            "node_type": "workflow",
            "goal": {str(item["name"]): str(item["name"]) for item in inputs},
            "goal_default": {
                str(item["name"]): _plain(item["default"])
                for item in inputs
                if "default" in item
            },
            "feedback": {},
            "result": {str(item["name"]): str(item["name"]) for item in outputs},
            "schema": schema,
            "meta_data": {
                "resource_template": host,
                "unilab": {
                    "framework_owner_only": True,
                    "workflow_source": {
                        "kind": "package",
                        "definition_fqid": source.definition_fqid,
                        "module": source.module,
                        "symbol": source.symbol,
                        "package_catalog_digest": source.package_catalog_digest,
                        "definition_content_hash": source.definition_content_hash,
                    },
                },
            },
        },
        handles=handles,
    )


def _workflow_schema(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    workflow_uuid: str,
    workflow_revision: int,
    applied_source_hash: str,
    contract_digest: str,
    composition_allow_transparent: bool,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _input_property_schema(item)
                    for item in inputs
                },
                "required": [
                    str(item["name"])
                    for item in inputs
                    if item.get("required") is True
                ],
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _plain(item["schema"]) for item in outputs
                },
                "required": [str(item["name"]) for item in outputs],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-workflow-contract": {
            "version": 1,
            "compatibility_version": 1,
            "workflow_uuid": workflow_uuid,
            "workflow_revision": workflow_revision,
            "applied_source_hash": applied_source_hash,
            "contract_digest": contract_digest,
            "composition_allow_transparent": composition_allow_transparent,
            "input_order": [str(item["name"]) for item in inputs],
            "output_order": [str(item["name"]) for item in outputs],
        },
    }


def _input_property_schema(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    schema = _plain(descriptor["schema"])
    if "default" in descriptor:
        schema["default"] = _plain(descriptor["default"])
    return schema


def _contract_digest(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    composition_allow_transparent: bool,
) -> str:
    payload = {
        "version": 1,
        "composition_allow_transparent": composition_allow_transparent,
        "inputs": [_semantic_descriptor(item) for item in inputs],
        "outputs": [_semantic_descriptor(item) for item in outputs],
    }
    return "sha256:" + hashlib.sha256(
        encode_json(payload, sort_keys=True)
    ).hexdigest()


def _semantic_descriptor(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _plain(value)
        for key, value in raw.items()
        if key not in {"title", "description"}
    }


def _value_handle(
    descriptor: Mapping[str, Any],
    *,
    io_type: str,
    template_uuid: str,
) -> dict[str, Any]:
    name = str(descriptor["name"])
    schema = _plain(descriptor["schema"])
    slot = resource_slot_schema(schema)
    allowlist = (
        _plain(slot.get("allowed_resource_template_uuids"))
        if slot is not None
        else None
    )
    implicit = bool(descriptor.get("implicit", False)) if io_type == "source" else False
    handle_uuid = str(uuid5(UUID(template_uuid), f"handle:{io_type}:{name}"))
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": name,
        "io_type": io_type,
        "display_name": str(descriptor.get("title") or name),
        "description": str(descriptor.get("description") or ""),
        "type": workflow_handle_type(schema),
        "required": bool(descriptor.get("required", False))
        if io_type == "target"
        else False,
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": name,
        "meta_data": {
            "unilab": {
                "value_schema": schema,
                "editor_control": "material_port" if slot is not None else "variable_selector",
                "allowed_resource_template_uuids": allowlist,
                "implicit_passthrough": implicit,
            }
        },
    }


def _ready_handle(io_type: str, *, template_uuid: str) -> dict[str, Any]:
    handle = structural_ready_handle(io_type)
    handle.update(
        {
            "uuid": str(uuid5(UUID(template_uuid), f"handle:{io_type}:ready")),
            "workflow_node_template_uuid": template_uuid,
        }
    )
    return handle


def _composition_mode(workflow: Mapping[str, Any]) -> bool:
    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    value = (
        unilab.get("composition_allow_transparent", False)
        if isinstance(unilab, Mapping)
        else False
    )
    if not isinstance(value, bool):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/composition_allow_transparent",
        )
    return value


def _host_summary(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "uuid",
        "name",
        "display_name",
    }:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/host_node/resource_template_uuid",
        )
    identity = _uuid(value.get("uuid"), "/host_node/resource_template_uuid")
    name = value.get("name")
    display_name = value.get("display_name")
    if not isinstance(name, str) or not name or not isinstance(display_name, str):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/host_node/resource_template",
        )
    return {"uuid": identity, "name": name, "display_name": display_name}


def _uuid(value: Any, path: str) -> str:
    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        ) from None
    if value != identity:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        )
    return identity


def _digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        )
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise PublishedWorkflowContractError("composite_catalog_mismatch", path)
    return _plain(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


from unilabos.server.workflow.composite_expansion import (  # noqa: E402
    CompositeAuthoring,
    CompositeExpansion,
    PublishedWorkflowResolver,
    PublishedWorkflowSnapshotProvider,
)

__all__ = [
    "CompositeAuthoring",
    "CompositeExpansion",
    "PublishedWorkflowContract",
    "PublishedWorkflowContractError",
    "PublishedWorkflowResolver",
    "PublishedWorkflowSnapshotProvider",
    "project_published_workflow_contract",
]
