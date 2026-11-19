"""旧版 JSON 工作流转换：动作节点与设液节点的 ``device_name`` 跟随调用方给的工作站实例名。"""

from __future__ import annotations

from unilabos.server.services.runtime.workflow.common import DEVICE_NAME_DEFAULT, build_protocol_graph, host_device_name
from unilabos.server.services.runtime.workflow.convert_from_json import convert_from_json

PROTOCOL = {
    "labware": [{"name": "source", "slot": "1", "type": "vendor_plate"}],
    "reagent": {
        "water": {"slot": 1, "well": ["A1"]},
        "mix": {"slot": 2, "well": ["B1"]},
    },
    "workflow": [
        {"action": "transfer_liquid", "action_args": {"sources": "water", "targets": "mix", "asp_vol": 50, "dis_vol": 50}},
    ],
}


def _device_names(graph) -> dict:
    names: dict = {}
    for node in graph.nodes.values():
        if node.get("template_name"):
            names.setdefault(node["template_name"], set()).add(node.get("device_name"))
    return names


def test_workstation_name_is_the_device_of_action_and_set_liquid_nodes() -> None:
    graph = convert_from_json(
        PROTOCOL,
        workstation_name="PRCXI",
        action_resource_mapping={"transfer_liquid": "liquid_handler.prcxi"},
        validate=False,
    )
    names = _device_names(graph)
    assert names["transfer_liquid"] == {"PRCXI"}
    assert names["set_liquid_from_plate"] == {"PRCXI"}
    # 建板节点仍在本机 host 服务设备上执行
    assert names["create_resource"] == {host_device_name()}


def test_explicit_device_name_overrides_the_workstation_name() -> None:
    graph = convert_from_json(PROTOCOL, workstation_name="station", device_name="lh_1", validate=False)
    assert _device_names(graph)["transfer_liquid"] == {"lh_1"}


def test_defaults_are_unchanged() -> None:
    names = _device_names(convert_from_json(PROTOCOL, validate=False))
    assert names["transfer_liquid"] == {DEVICE_NAME_DEFAULT}
    assert names["set_liquid_from_plate"] == {DEVICE_NAME_DEFAULT}

    steps = [{"action": "transfer_liquid", "parameters": {}}]
    direct = build_protocol_graph({}, steps, workstation_name="workstation")
    assert _device_names(direct)["transfer_liquid"] == {DEVICE_NAME_DEFAULT}
    direct = build_protocol_graph({}, steps, workstation_name="workstation", device_name="lh_2")
    assert _device_names(direct)["transfer_liquid"] == {"lh_2"}
