"""Resolve YAML ``$ref`` pointers in external registry files (Plan 09 Task 2).

Supports ``relative/path.yaml#/json/pointer`` references so multiple device
variants can share one action/status contract. Detects reference cycles.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


class YamlRefError(ValueError):
    pass


class YamlRefCycleError(YamlRefError):
    pass


def resolve_yaml_refs(data: Any, base_file: Path | str) -> Any:
    return _resolve_node(deepcopy(data), Path(base_file).resolve(), seen=set())


def _resolve_node(node: Any, base_file: Path, seen: set[str]) -> Any:
    if isinstance(node, dict):
        if set(node.keys()) == {"$ref"}:
            ref = str(node["$ref"])
            path_part, _pointer = _split_ref(ref)
            # Same-document refs (e.g. JSON-Schema `#/$defs/Foo` inside init_param_schema)
            # are NOT external-registry contract refs: leave them intact for the schema
            # consumer. Only cross-file refs (with a file path part) are expanded here.
            if not path_part:
                return node
            return _resolve_ref(ref, base_file, seen)
        return {key: _resolve_node(value, base_file, seen) for key, value in node.items()}

    if isinstance(node, list):
        return [_resolve_node(item, base_file, seen) for item in node]

    return node


def _resolve_ref(ref: str, base_file: Path, seen: set[str]) -> Any:
    path_part, pointer = _split_ref(ref)
    ref_file = (base_file.parent / path_part).resolve()
    cycle_key = f"{ref_file}#{pointer}"
    if cycle_key in seen:
        raise YamlRefCycleError(f"YAML $ref cycle detected: {cycle_key}")

    seen.add(cycle_key)
    try:
        with ref_file.open(encoding="utf-8") as file:
            ref_data = yaml.safe_load(file) or {}
        target = _select_json_pointer(ref_data, pointer)
        return _resolve_node(deepcopy(target), ref_file, seen)
    finally:
        seen.remove(cycle_key)


def _split_ref(ref: str) -> tuple[str, str]:
    if "#" not in ref:
        return ref, ""
    path_part, pointer = ref.split("#", 1)
    return path_part, pointer


def _select_json_pointer(data: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return data
    if not pointer.startswith("/"):
        raise YamlRefError(f"Invalid JSON pointer: {pointer}")

    current = data
    for raw_part in pointer.strip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise YamlRefError(f"Cannot select '{part}' from non-container value")
    return current
