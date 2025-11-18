import json
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from scripts.workflow import build_protocol_graph, draw_protocol_graph, draw_protocol_graph_with_ports


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _normalize_steps(data):
    normalized = []
    for step in data:
        action = step.get("action") or step.get("operation")
        if not action:
            continue
        raw_params = step.get("parameters") or step.get("action_args") or {}
        params = dict(raw_params)

        if "source" in raw_params and "sources" not in raw_params:
            params["sources"] = raw_params["source"]
        if "target" in raw_params and "targets" not in raw_params:
            params["targets"] = raw_params["target"]

        description = step.get("description") or step.get("purpose")
        step_dict = {"action": action, "parameters": params}
        if description:
            step_dict["description"] = description
        normalized.append(step_dict)
    return normalized


def _normalize_labware(data):
    labware = {}
    for item in data:
        reagent_name = item.get("reagent_name")
        key = reagent_name or item.get("material_name") or item.get("name")
        if not key:
            continue
        key = str(key)
        idx = 1
        original_key = key
        while key in labware:
            idx += 1
            key = f"{original_key}_{idx}"

        labware[key] = {
            "slot": item.get("positions") or item.get("slot"),
            "labware": item.get("material_name") or item.get("labware"),
            "well": item.get("well", []),
            "type": item.get("type", "reagent"),
            "role": item.get("role", ""),
            "name": key,
        }
    return labware


@pytest.mark.parametrize("protocol_name", [
    "example_bio",
    # "bioyond_materials_liquidhandling_1",
    "example_prcxi",
])
def test_build_protocol_graph(protocol_name):
    data_path = Path(__file__).with_name(f"{protocol_name}.json")
    with data_path.open("r", encoding="utf-8") as fp:
        d = json.load(fp)

    if "workflow" in d and "reagent" in d:
        protocol_steps = d["workflow"]
        labware_info = d["reagent"]
    elif "steps_info" in d and "labware_info" in d:
        protocol_steps = _normalize_steps(d["steps_info"])
        labware_info = _normalize_labware(d["labware_info"])
    else:
        raise ValueError("Unsupported protocol format")

    graph = build_protocol_graph(
        labware_info=labware_info,
        protocol_steps=protocol_steps,
        workstation_name="PRCXi",
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = data_path.with_name(f"{protocol_name}_graph_{timestamp}.png")
    draw_protocol_graph_with_ports(graph, str(output_path))
    print(graph)