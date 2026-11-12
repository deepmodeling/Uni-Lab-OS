"""图层级只由 ``parent`` 表达：``children`` 是派生字段，读取忽略、导出不写。"""

from __future__ import annotations

from uuid import uuid4

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials.graph import graph_resource_tree


def _node(node_id: str, parent: str | None = None, **overrides):
    payload = {
        "id": node_id,
        "uuid": str(uuid4()),
        "name": node_id,
        "type": "container",
        "class": "",
        "template_name": "RegularContainer",
        "config": {"type": "RegularContainer"},
        "data": {},
        "extra": {},
        "parent": parent,
    }
    payload.update(overrides)
    return payload


def test_graph_loader_builds_hierarchy_from_parent_only():
    tree = graph_resource_tree(
        [_node("root"), _node("child", parent="root"), _node("leaf", parent="child")]
    )

    (root,) = tree.root_nodes
    assert [child.res_content.id for child in root.children] == ["child"]
    assert [leaf.res_content.id for leaf in root.children[0].children] == ["leaf"]
    assert all("children" not in dumped for dumped in tree.dump()[0])


def test_graph_loader_ignores_legacy_children_lists():
    """旧图 children 列表与 parent 不一致时以 parent 为准，且 children 不会落进 config。"""
    tree = graph_resource_tree(
        [
            _node("root", children=["stranger"]),
            _node("child", parent="root", children=[]),
            _node("stranger", children=[]),
        ]
    )

    roots = {node.res_content.id: node for node in tree.root_nodes}
    assert set(roots) == {"root", "stranger"}
    assert [child.res_content.id for child in roots["root"].children] == ["child"]
    assert all("children" not in node.res_content.config for node in tree.all_nodes)


def test_flat_dump_roundtrips_hierarchy_via_parent_uuid():
    """扁平 dump 只靠 parent_uuid 表达层级，重新加载后树结构不变。"""
    root = _node("root", type="deck")
    plate_a = _node("a", parent="root", type="plate")
    well = _node("a1", parent="a", type="well")
    plate_b = _node("b", parent="root", type="plate")
    tree = ResourceTreeSet.from_raw_dict_list([root, plate_a, well, plate_b])

    (flat,) = tree.dump()
    assert [node["id"] for node in flat] == ["root", "a", "a1", "b"]
    assert all("children" not in node for node in flat)
    assert {node["id"]: node["parent_uuid"] for node in flat} == {
        "root": None,
        "a": root["uuid"],
        "a1": plate_a["uuid"],
        "b": root["uuid"],
    }

    rebuilt = ResourceTreeSet.load([flat])
    (rebuilt_root,) = rebuilt.root_nodes
    assert [child.res_content.id for child in rebuilt_root.children] == ["a", "b"]
    assert [leaf.res_content.id for leaf in rebuilt_root.children[0].children] == ["a1"]
