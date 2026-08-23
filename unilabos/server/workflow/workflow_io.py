"""已发布工作流父子输入输出合同的只读校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from unilabos.server.workflow.handle_projection import resource_slot_schema
from unilabos.server.workflow.models import validate_uuid
from unilabos.server.workflow.value_schema import (
    WorkflowValueSchemaError,
    normalize_value_schema,
    schema_is_assignable,
    validate_value,
)


class WorkflowIOValidationError(ValueError):
    """工作流公共输入输出边界无法安全投影。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkflowContract:
    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.value)


@dataclass(frozen=True, slots=True)
class WorkflowGraphIO:
    input_contract: WorkflowContract
    output_contract: WorkflowContract
    input_bindings: Mapping[str, tuple[Mapping[str, str], ...]]
    output_bindings: Mapping[str, Mapping[str, str]]


def validate_workflow_graph_io(graph: Mapping[str, Any]) -> WorkflowGraphIO:
    """校验工作流合同、节点输入绑定和输出绑定的一致性。"""

    try:
        workflow = _mapping(graph.get("workflow"), "/workflow")
        nodes = _sequence(graph.get("nodes"), "/nodes")
        templates = _sequence(graph.get("node_templates"), "/node_templates")
        handles = _sequence(graph.get("handle_templates"), "/handle_templates")
        validate_uuid(workflow.get("uuid"))
        node_by_uuid = {
            validate_uuid(item.get("uuid")): item for item in map(_mapping, nodes)
        }
        template_by_uuid = {
            validate_uuid(item.get("uuid")): item
            for item in map(_mapping, templates)
        }
        handle_by_uuid = {
            validate_uuid(item.get("uuid")): item
            for item in map(_mapping, handles)
        }
    except (TypeError, ValueError):
        raise WorkflowIOValidationError(
            "workflow_io_invalid",
            "/graph",
        ) from None
    if (
        len(node_by_uuid) != len(nodes)
        or len(template_by_uuid) != len(templates)
        or len(handle_by_uuid) != len(handles)
    ):
        raise WorkflowIOValidationError("workflow_io_invalid", "/graph")

    unilab = _unilab(workflow)
    input_contract = _input_contract(unilab.get("input_contract"))
    output_contract = _output_contract(unilab.get("output_contract"))
    input_descriptors = {
        item["name"]: item for item in input_contract["parameters"]
    }
    output_descriptors = {
        item["name"]: item for item in output_contract["outputs"]
    }

    input_bindings: dict[str, list[dict[str, str]]] = {
        name: [] for name in input_descriptors
    }
    for node_uuid, node in node_by_uuid.items():
        template_uuid = node.get("workflow_node_template_uuid")
        if template_uuid is None:
            continue
        try:
            template_uuid = validate_uuid(template_uuid)
        except (TypeError, ValueError):
            raise WorkflowIOValidationError(
                "workflow_io_invalid",
                f"/nodes/{node_uuid}/workflow_node_template_uuid",
            ) from None
        if template_uuid not in template_by_uuid:
            raise WorkflowIOValidationError(
                "workflow_io_invalid",
                f"/nodes/{node_uuid}/workflow_node_template_uuid",
            )
        bindings = _unilab(node).get("input_bindings", {})
        if not isinstance(bindings, Mapping):
            raise WorkflowIOValidationError(
                "workflow_io_invalid",
                f"/nodes/{node_uuid}/input_bindings",
            )
        for raw_handle_uuid, raw_binding in bindings.items():
            try:
                handle_uuid = validate_uuid(raw_handle_uuid)
            except (TypeError, ValueError):
                raise WorkflowIOValidationError(
                    "workflow_io_invalid",
                    f"/nodes/{node_uuid}/input_bindings",
                ) from None
            handle = handle_by_uuid.get(handle_uuid)
            if (
                handle is None
                or handle.get("workflow_node_template_uuid") != template_uuid
                or handle.get("io_type") != "target"
                or handle.get("handle_key") == "ready"
            ):
                raise WorkflowIOValidationError(
                    "workflow_io_invalid",
                    f"/nodes/{node_uuid}/input_bindings/{handle_uuid}",
                )
            binding = _mapping(
                raw_binding,
                f"/nodes/{node_uuid}/input_bindings/{handle_uuid}",
            )
            if set(binding) != {"parameter"} or not isinstance(
                binding.get("parameter"), str
            ):
                raise WorkflowIOValidationError(
                    "workflow_io_invalid",
                    f"/nodes/{node_uuid}/input_bindings/{handle_uuid}",
                )
            parameter = binding["parameter"]
            descriptor = input_descriptors.get(parameter)
            handle_schema = _handle_schema(handle)
            if descriptor is None or not schema_is_assignable(
                descriptor["schema"],
                handle_schema,
            ):
                raise WorkflowIOValidationError(
                    "workflow_io_invalid",
                    f"/nodes/{node_uuid}/input_bindings/{handle_uuid}",
                )
            input_bindings[parameter].append(
                {
                    "workflow_node_uuid": node_uuid,
                    "target_handle_uuid": handle_uuid,
                }
            )

    raw_outputs = unilab.get("output_bindings", {})
    if not isinstance(raw_outputs, Mapping) or set(raw_outputs) != set(
        output_descriptors
    ):
        raise WorkflowIOValidationError(
            "workflow_io_invalid",
            "/workflow/meta_data/unilab/output_bindings",
        )
    output_bindings: dict[str, dict[str, str]] = {}
    for name, descriptor in output_descriptors.items():
        path = f"/workflow/meta_data/unilab/output_bindings/{name}"
        binding = _mapping(raw_outputs[name], path)
        kind = binding.get("kind")
        if kind == "workflow_input":
            if set(binding) != {"kind", "parameter"}:
                raise WorkflowIOValidationError("workflow_io_invalid", path)
            parameter = binding.get("parameter")
            source = input_descriptors.get(parameter)
            if source is None or not schema_is_assignable(
                source["schema"],
                descriptor["schema"],
            ):
                raise WorkflowIOValidationError("workflow_io_invalid", path)
            output_bindings[name] = {
                "kind": "workflow_input",
                "parameter": str(parameter),
            }
            continue
        if kind != "node_output" or set(binding) != {
            "kind",
            "workflow_node_uuid",
            "source_handle_uuid",
        }:
            raise WorkflowIOValidationError("workflow_io_invalid", path)
        try:
            node_uuid = validate_uuid(binding["workflow_node_uuid"])
            handle_uuid = validate_uuid(binding["source_handle_uuid"])
        except (KeyError, TypeError, ValueError):
            raise WorkflowIOValidationError("workflow_io_invalid", path) from None
        node = node_by_uuid.get(node_uuid)
        handle = handle_by_uuid.get(handle_uuid)
        if (
            node is None
            or handle is None
            or handle.get("io_type") != "source"
            or handle.get("handle_key") == "ready"
            or handle.get("workflow_node_template_uuid")
            != node.get("workflow_node_template_uuid")
            or not schema_is_assignable(_handle_schema(handle), descriptor["schema"])
        ):
            raise WorkflowIOValidationError("workflow_io_invalid", path)
        output_bindings[name] = {
            "kind": "node_output",
            "workflow_node_uuid": node_uuid,
            "source_handle_uuid": handle_uuid,
        }

    _validate_resource_slot_output_authority(
        input_descriptors=input_descriptors,
        output_descriptors=output_descriptors,
        output_bindings=output_bindings,
    )

    return WorkflowGraphIO(
        input_contract=WorkflowContract(input_contract),
        output_contract=WorkflowContract(output_contract),
        input_bindings={
            name: tuple(values) for name, values in input_bindings.items()
        },
        output_bindings=output_bindings,
    )


def _input_contract(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"version": 1, "parameters": []}
    contract = _mapping(raw, "/input_contract")
    if contract.get("version") != 1 or set(contract) != {"version", "parameters"}:
        raise WorkflowIOValidationError("workflow_io_invalid", "/input_contract")
    parameters = _sequence(contract.get("parameters"), "/input_contract/parameters")
    normalized = [
        _descriptor(item, output=False, path=f"/input_contract/parameters/{index}")
        for index, item in enumerate(parameters)
    ]
    _unique_names(normalized, "/input_contract/parameters")
    return {"version": 1, "parameters": normalized}


def _output_contract(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"version": 1, "outputs": []}
    contract = _mapping(raw, "/output_contract")
    if contract.get("version") != 1 or set(contract) != {"version", "outputs"}:
        raise WorkflowIOValidationError("workflow_io_invalid", "/output_contract")
    outputs = _sequence(contract.get("outputs"), "/output_contract/outputs")
    normalized = [
        _descriptor(item, output=True, path=f"/output_contract/outputs/{index}")
        for index, item in enumerate(outputs)
    ]
    _unique_names(normalized, "/output_contract/outputs")
    return {"version": 1, "outputs": normalized}


def _descriptor(raw: Any, *, output: bool, path: str) -> dict[str, Any]:
    descriptor = _mapping(raw, path)
    allowed = {"name", "schema", "title", "description"}
    allowed |= {"implicit"} if output else {"required", "default"}
    if set(descriptor) - allowed:
        raise WorkflowIOValidationError("workflow_io_invalid", path)
    name = descriptor.get("name")
    if not isinstance(name, str) or not name.isidentifier():
        raise WorkflowIOValidationError("workflow_io_invalid", f"{path}/name")
    try:
        schema = normalize_value_schema(descriptor.get("schema"), path=f"{path}/schema")
    except WorkflowValueSchemaError as error:
        raise WorkflowIOValidationError(error.code, error.path) from error
    result: dict[str, Any] = {"name": name, "schema": schema}
    for field in ("title", "description"):
        value = descriptor.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise WorkflowIOValidationError("workflow_io_invalid", f"{path}/{field}")
            result[field] = value
    if output:
        implicit = descriptor.get("implicit", False)
        if not isinstance(implicit, bool):
            raise WorkflowIOValidationError("workflow_io_invalid", f"{path}/implicit")
        result["implicit"] = implicit
    else:
        required = descriptor.get("required", False)
        if not isinstance(required, bool):
            raise WorkflowIOValidationError("workflow_io_invalid", f"{path}/required")
        result["required"] = required
        if "default" in descriptor:
            try:
                result["default"] = validate_value(
                    schema,
                    descriptor["default"],
                    path=f"{path}/default",
                )
            except WorkflowValueSchemaError as error:
                raise WorkflowIOValidationError(error.code, error.path) from error
    return result


def _handle_schema(handle: Mapping[str, Any]) -> dict[str, Any]:
    unilab = _unilab(handle)
    raw = unilab.get("value_schema")
    if raw is None:
        kind = str(handle.get("type") or "object")
        aliases = {"ResourceSlot": {"$slot": "ResourceSlot"}, "default": {"type": "object"}}
        raw = aliases.get(kind, {"type": kind})
    try:
        return normalize_value_schema(raw)
    except WorkflowValueSchemaError as error:
        raise WorkflowIOValidationError(error.code, error.path) from error


def _validate_resource_slot_output_authority(
    *,
    input_descriptors: Mapping[str, Mapping[str, Any]],
    output_descriptors: Mapping[str, Mapping[str, Any]],
    output_bindings: Mapping[str, Mapping[str, str]],
) -> None:
    """保证物料输入不会在组合工作流公共边界中静默丢失。"""

    for name, input_descriptor in input_descriptors.items():
        if resource_slot_schema(input_descriptor["schema"]) is None:
            continue
        output_descriptor = output_descriptors.get(name)
        if (
            output_descriptor is None
            or resource_slot_schema(output_descriptor["schema"]) is None
            or not schema_is_assignable(
                input_descriptor["schema"],
                output_descriptor["schema"],
            )
        ):
            raise WorkflowIOValidationError(
                "workflow_io_invalid",
                f"/output_contract/outputs/{name}",
            )

    for name, output_descriptor in output_descriptors.items():
        if output_descriptor.get("implicit") is not True:
            continue
        input_descriptor = input_descriptors.get(name)
        binding = output_bindings.get(name)
        if (
            input_descriptor is None
            or resource_slot_schema(input_descriptor["schema"]) is None
            or resource_slot_schema(output_descriptor["schema"]) is None
            or not schema_is_assignable(
                input_descriptor["schema"],
                output_descriptor["schema"],
            )
            or not schema_is_assignable(
                output_descriptor["schema"],
                input_descriptor["schema"],
            )
            or binding != {"kind": "workflow_input", "parameter": name}
        ):
            raise WorkflowIOValidationError(
                "workflow_io_invalid",
                f"/workflow/meta_data/unilab/output_bindings/{name}",
            )


def _unilab(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    meta_data = entity.get("meta_data")
    if not isinstance(meta_data, Mapping):
        return {}
    unilab = meta_data.get("unilab")
    return unilab if isinstance(unilab, Mapping) else {}


def _mapping(value: Any, path: str = "/value") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowIOValidationError("workflow_io_invalid", path)
    return {str(key): _plain(item) for key, item in value.items()}


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise WorkflowIOValidationError("workflow_io_invalid", path)
    return [_plain(item) for item in value]


def _unique_names(items: Sequence[Mapping[str, Any]], path: str) -> None:
    names = [str(item["name"]) for item in items]
    if len(set(names)) != len(names):
        raise WorkflowIOValidationError("workflow_io_invalid", path)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "WorkflowContract",
    "WorkflowGraphIO",
    "WorkflowIOValidationError",
    "schema_is_assignable",
    "validate_workflow_graph_io",
]
