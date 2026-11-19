"""``graph create`` 骨架生成器：设备节点、Site 展开与过滤规则。"""

from __future__ import annotations

import pytest

from unilabos.registry.graph_scaffold import build_graph_skeleton


class _StubRegistry:
    def __init__(self, device_type_registry):
        self.device_type_registry = device_type_registry


SITE_DEFINITION = {
    "index": 1,
    "label": "A1",
    "pose": {
        "position": {"x": 60.0, "y": 40.0, "z": 0.0},
        "size": {"width": 100.0, "height": 100.0, "depth": 20.0},
    },
    "allowed_resource_categories": ["demo_sample"],
    "parent_link": "A1",
    "description": "样品架 A1 位",
    "meta_data": {"row": "A", "column": 1},
}


@pytest.fixture()
def registry():
    return _StubRegistry(
        {
            "host_node": {"display_name": "Host"},
            "pump_demo": {"display_name": "示例泵", "available_sites": []},
            "rack_demo": {
                "display_name": "",
                "available_sites": [SITE_DEFINITION],
            },
        }
    )


def test_skeleton_generates_device_nodes_and_expands_sites(registry) -> None:
    payload = build_graph_skeleton(registry)

    assert payload["links"] == []
    ids = [node["id"] for node in payload["nodes"]]
    assert ids == ["pump_demo", "rack_demo"]  # host_node 被排除，按 id 排序

    pump = payload["nodes"][0]
    assert pump["class"] == "pump_demo"
    assert pump["template_name"] == "pump_demo"
    assert pump["name"] == "示例泵"
    assert pump["type"] == "device"
    assert pump["parent"] is None
    assert "children" not in pump  # 层级只由 parent 表达
    assert "sites" not in pump

    rack = payload["nodes"][1]
    assert rack["name"] == "rack_demo"  # display_name 为空时回退 id
    assert rack["sites_initialized"] is True
    assert rack["template_name"] == "rack_demo"
    (site,) = rack["sites"]
    assert site["label"] == "A1"
    assert site["material_uuid"] == rack["uuid"]
    assert site["template_name"] == "rack_demo"
    assert site["occupied_material_uuid"] is None
    assert site["uuid"]
    assert site["pose"]["position"] == {"x": 60.0, "y": 40.0, "z": 0.0}


def test_skeleton_include_filter(registry) -> None:
    payload = build_graph_skeleton(registry, include=["pump_demo"])
    assert [node["id"] for node in payload["nodes"]] == ["pump_demo"]


def test_skeleton_rejects_unknown_include(registry) -> None:
    with pytest.raises(ValueError, match="不存在这些设备"):
        build_graph_skeleton(registry, include=["nope"])


def test_skeleton_requires_at_least_one_device() -> None:
    with pytest.raises(ValueError, match="没有可生成节点"):
        build_graph_skeleton(_StubRegistry({"host_node": {}}))
