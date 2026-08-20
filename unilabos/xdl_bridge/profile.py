from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

import yaml

from .contracts import standard_operations


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class StationProfile:
    path: Path
    graph_path: Path
    registry_path: Path
    workstation_id: str
    resource_name: str
    mode: str
    hardware_ids: dict[str, str]
    hardware_types: dict[str, str]
    operations: dict[str, dict[str, Any]]
    graph_nodes: frozenset[str]

    def bind_component(self, component_id: str, component_type: str) -> str:
        node_id = self.hardware_ids.get(component_id) or self.hardware_types.get(
            component_type
        )
        if node_id is None:
            raise ProfileError(
                f"No station binding for component {component_id!r} ({component_type!r})"
            )
        if node_id not in self.graph_nodes:
            raise ProfileError(f"Bound station node does not exist: {node_id}")
        return node_id

    def operation(self, name: str) -> dict[str, Any]:
        try:
            return self.operations[name]
        except KeyError as exc:
            raise ProfileError(f"Unsupported XDL operation: {name}") from exc


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{name} must be a mapping")
    return value


def _handle_keys(action: dict[str, Any], io_type: str) -> set[str]:
    handles = _mapping(action.get("handles", {}), "registry handles")
    return {
        str(item["handler_key"])
        for item in handles.get(io_type, [])
        if isinstance(item, dict) and item.get("handler_key")
    }


def load_station_profile(path: str | Path) -> StationProfile:
    profile_path = Path(path).resolve()
    raw = _mapping(yaml.safe_load(profile_path.read_text(encoding="utf-8")), "profile")
    station = _mapping(raw.get("station"), "station")
    hardware = _mapping(raw.get("hardware"), "hardware")
    graph_path = (profile_path.parent / str(station["graph"])).resolve()
    registry_path = (profile_path.parent / str(station["registry"])).resolve()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    registry = _mapping(
        yaml.safe_load(registry_path.read_text(encoding="utf-8")), "registry"
    )
    graph_nodes = frozenset(str(node["id"]) for node in graph.get("nodes", []))
    workstation_id = str(station["workstation_id"])
    if workstation_id not in graph_nodes:
        raise ProfileError(f"Workstation node does not exist: {workstation_id}")
    mode = str(station.get("mode", "real"))
    if mode not in {"real", "virtual"}:
        raise ProfileError(f"Station mode must be real or virtual: {mode}")

    operations = standard_operations()
    for name, override in _mapping(raw.get("operation_overrides", {}), "operation_overrides").items():
        if name not in operations:
            raise ProfileError(f"Cannot override unknown operation: {name}")
        operations[name].update(_mapping(override, f"operation_overrides.{name}"))

    resource_name = str(station["resource_name"])
    resource = _mapping(registry.get(resource_name), f"registry.{resource_name}")
    actions = _mapping(
        _mapping(resource.get("class"), f"registry.{resource_name}.class").get(
            "action_value_mappings"
        ),
        "action_value_mappings",
    )
    for name, operation in operations.items():
        action = actions.get(operation["template"])
        if not isinstance(action, dict):
            continue
        for handle in operation.get("inputs", {}).values():
            if handle not in _handle_keys(action, "input"):
                raise ProfileError(f"{name}: missing input handle {handle}")
        for handle in operation.get("outputs", {}).values():
            if handle not in _handle_keys(action, "output"):
                raise ProfileError(f"{name}: missing output handle {handle}")

    return StationProfile(
        path=profile_path,
        graph_path=graph_path,
        registry_path=registry_path,
        workstation_id=workstation_id,
        resource_name=resource_name,
        mode=mode,
        hardware_ids={str(k): str(v) for k, v in _mapping(hardware.get("ids", {}), "hardware.ids").items()},
        hardware_types={str(k): str(v) for k, v in _mapping(hardware.get("types", {}), "hardware.types").items()},
        operations=operations,
        graph_nodes=graph_nodes,
    )
