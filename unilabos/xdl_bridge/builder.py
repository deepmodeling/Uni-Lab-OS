from __future__ import annotations

from copy import deepcopy
import json
import uuid
from typing import Any

from .models import CanonicalProcedure
from .profile import StationProfile


class BridgeValidationError(ValueError):
    pass


_VESSEL_KEYS = {
    "vessel",
    "from_vessel",
    "to_vessel",
    "separation_vessel",
    "filtrate_vessel",
    "waste_phase_to_vessel",
    "product_vessel",
    "waste_vessel",
}


def _normalize_scalar(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    if key == "repeats":
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _edge(source: str, target: str, source_handle: str, target_handle: str) -> dict[str, str]:
    return {
        "source": source,
        "target": target,
        "source_node_uuid": source,
        "target_node_uuid": target,
        "source_handle_key": source_handle,
        "source_handle_io": "source",
        "target_handle_key": target_handle,
        "target_handle_io": "target",
    }


def build_workflow(
    procedure: CanonicalProcedure, profile: StationProfile, *, name: str
) -> dict[str, Any]:
    bindings = {
        component["id"]: profile.bind_component(
            component["id"], component.get("type", "")
        )
        for component in procedure.components
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    latest_output: dict[str, tuple[str, str]] = {}
    previous_node: str | None = None
    for step in procedure.steps:
        operation = profile.operation(step.operation)
        node_id = str(uuid.uuid4())
        parameters = deepcopy(step.parameters)
        for old, new in operation.get("parameter_aliases", {}).items():
            if old in parameters and new not in parameters:
                parameters[new] = parameters.pop(old)
        for key, value in operation.get("defaults", {}).items():
            parameters.setdefault(key, value)
        parameters.update(operation.get("overrides", {}))
        for key, value in tuple(parameters.items()):
            if key in _VESSEL_KEYS and isinstance(value, str):
                try:
                    parameters[key] = value if value in profile.graph_nodes else bindings[value]
                except KeyError as exc:
                    raise BridgeValidationError(
                        f"{step.source_path}: unbound vessel {value!r}"
                    ) from exc
            else:
                parameters[key] = _normalize_scalar(key, value)
        nodes.append(
            {
                "uuid": node_id,
                "name": f"Step {step.sequence}",
                "type": "ILab",
                "lab_node_type": "ILab",
                "template_name": operation["template"],
                "resource_name": profile.resource_name,
                "device_name": profile.workstation_id,
                "description": f"{step.operation} operation",
                "footer": f"{operation['template']}-{profile.resource_name}",
                "param": parameters,
            }
        )
        for parameter, handle in operation.get("inputs", {}).items():
            resource_id = parameters.get(parameter)
            if isinstance(resource_id, str) and resource_id in latest_output:
                source, source_handle = latest_output[resource_id]
                edges.append(_edge(source, node_id, source_handle, handle))
        if previous_node is not None:
            edges.append(_edge(previous_node, node_id, "ready", "ready"))
        for parameter, handle in operation.get("outputs", {}).items():
            resource_id = parameters.get(parameter)
            if isinstance(resource_id, str):
                latest_output[resource_id] = (node_id, handle)
        previous_node = node_id
    return {
        "workflow_uuid": str(uuid.uuid4()),
        "workflow_name": name,
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "edges": edges,
        "links": edges,
    }


def validate_workflow(payload: dict[str, Any], profile: StationProfile) -> None:
    serialized = json.dumps(payload)
    for value in ("PRCXI", "liquid_handler.prcxi", "[WARN:", "device."):
        if value in serialized:
            raise BridgeValidationError(f"Forbidden workflow value: {value}")
    for node in payload.get("nodes", []):
        if node.get("resource_name") != profile.resource_name:
            raise BridgeValidationError("Unexpected workflow resource")
        if node.get("device_name") != profile.workstation_id:
            raise BridgeValidationError("Unexpected workflow device")
        for key in _VESSEL_KEYS:
            if key in node.get("param", {}) and node["param"][key] not in profile.graph_nodes:
                raise BridgeValidationError(f"Unbound resource {node['name']}.{key}")
