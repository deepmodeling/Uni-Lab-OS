"""materials.from_str / to_str / parse_resource_slot 的序列化契约测试。

资源在跨进程边界统一走 JSON 字符串；这里锁定三种输入形态的自动识别
（单节点对象、扁平节点列表、dump() 分组形态）和 ResourceSlot 的剥离规则。
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pylabrobot.resources import Resource as PLRResource

from unilabos.resources import materials
from unilabos.resources.resource_tracker import ResourceTreeSet


def _node(**overrides):
    payload = {
        "id": "carrier",
        "uuid": str(uuid4()),
        "name": "carrier",
        "type": "carrier",
        "class": "",
        "config": {},
        "data": {},
        "extra": {},
        "pose": {"position": {"x": 1, "y": 2, "z": 3}},
    }
    payload.update(overrides)
    return payload


def test_from_str_single_node_object():
    node = _node()
    tree_set = materials.from_str(json.dumps(node))
    assert isinstance(tree_set, ResourceTreeSet)
    assert len(tree_set.trees) == 1
    assert tree_set.trees[0].root_node.res_content.uuid == node["uuid"]


def test_from_str_flat_node_list_builds_tree():
    root = _node(id="root", name="root")
    child = _node(id="child", name="child", parent_uuid=root["uuid"])
    tree_set = materials.from_str(json.dumps([root, child]))
    assert len(tree_set.trees) == 1
    root_node = tree_set.trees[0].root_node
    assert root_node.res_content.uuid == root["uuid"]
    assert [c.res_content.uuid for c in root_node.children] == [child["uuid"]]


def test_to_str_from_str_roundtrip_dump_form():
    root = _node(id="root", name="root")
    child = _node(id="child", name="child", parent_uuid=root["uuid"])
    tree_set = materials.from_str([root, child])

    payload = materials.to_str(tree_set)
    restored = materials.from_str(payload)

    assert json.loads(payload) == tree_set.dump()
    assert restored.dump() == tree_set.dump()


def test_from_str_rejects_invalid_payload():
    with pytest.raises(ValueError):
        materials.from_str("[]")
    with pytest.raises(ValueError):
        materials.from_str(json.dumps([1, 2]))
    with pytest.raises(ValueError):
        materials.from_str(json.dumps("just-a-string"))


def test_parse_resource_slot_passthrough_plr():
    plr = PLRResource(name="res", size_x=1, size_y=1, size_z=1)
    kind, payload = materials.parse_resource_slot(plr)
    assert kind == materials.SLOT_KIND_PLR
    assert payload is plr


def test_parse_resource_slot_reference_forms():
    ref = {"uuid": "u-1"}
    assert materials.parse_resource_slot(ref) == (
        materials.SLOT_KIND_REFERENCE,
        ref,
    )

    kind, payload = materials.parse_resource_slot(json.dumps({"id": "carrier"}))
    assert kind == materials.SLOT_KIND_REFERENCE
    assert payload == {"id": "carrier"}

    # 裸字符串按 uuid 引用处理
    kind, payload = materials.parse_resource_slot("bare-uuid-string")
    assert kind == materials.SLOT_KIND_REFERENCE
    assert payload == {"uuid": "bare-uuid-string"}


def test_parse_resource_slot_tree_forms():
    root = _node(id="root", name="root")
    child = _node(id="child", name="child", parent_uuid=root["uuid"])
    payload_str = json.dumps([root, child])

    kind, tree_set = materials.parse_resource_slot(json.loads(payload_str))
    assert kind == materials.SLOT_KIND_TREE
    assert isinstance(tree_set, ResourceTreeSet)
    assert len(tree_set.trees) == 1

    kind, tree_set_from_str = materials.parse_resource_slot(payload_str)
    assert kind == materials.SLOT_KIND_TREE
    assert tree_set_from_str.dump() == tree_set.dump()


def test_parse_resource_slot_rejects_invalid():
    with pytest.raises(ValueError):
        materials.parse_resource_slot("")
    with pytest.raises(ValueError):
        materials.parse_resource_slot({"foo": "bar"})
    with pytest.raises(TypeError):
        materials.parse_resource_slot(123)
