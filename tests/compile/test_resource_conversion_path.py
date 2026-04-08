"""
ROS Goal → Resource 转换 → 编译器路径的集成测试

覆盖：
1. Resource.msg 新字段(uuid, klass, extra)的往返转换
2. dict → ROS Resource → dict 往返无损
3. ResourceTreeSet → get_plr_nested_dict 保留 children 结构
4. resource_helper 兼容 dict / ResourceDictInstance
5. vessel_parser.get_vessel 兼容 ResourceDictInstance
"""

import json
import pytest

# 不依赖 ROS 的测试 —— 直接测试 resource 处理路径
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.compile.utils.resource_helper import (
    ensure_resource_instance,
    resource_to_dict,
    get_resource_id,
    get_resource_data,
    get_resource_display_info,
    get_resource_liquid_volume,
)
from unilabos.compile.utils.vessel_parser import get_vessel


# ============ 构建测试数据 ============


def _make_resource_dict(
    id="reactor_01",
    uuid="uuid-reactor-01",
    name="reactor_01",
    klass="virtual_stirrer",
    type_="device",
    parent=None,
    parent_uuid=None,
    data=None,
    config=None,
    extra=None,
):
    return {
        "id": id,
        "uuid": uuid,
        "name": name,
        "class": klass,
        "type": type_,
        "parent": parent,
        "parent_uuid": parent_uuid or "",
        "description": "",
        "config": config or {},
        "data": data or {},
        "extra": extra or {},
        "position": {"x": 1.0, "y": 2.0, "z": 3.0},
    }


def _make_resource_instance(id="reactor_01", **kwargs):
    d = _make_resource_dict(id=id, **kwargs)
    return ResourceDictInstance.get_resource_instance_from_dict(d)


def _make_tree_with_children():
    """构建 StationA -> [R1, R2] 的资源树"""
    raw_data = [
        _make_resource_dict(
            id="StationA",
            uuid="uuid-station-a",
            name="StationA",
            klass="workstation",
            type_="device",
        ),
        _make_resource_dict(
            id="R1",
            uuid="uuid-r1",
            name="R1",
            klass="",
            type_="resource",
            parent="StationA",
            parent_uuid="uuid-station-a",
            data={"liquid": [{"liquid_type": "water", "volume": 10.0}]},
        ),
        _make_resource_dict(
            id="R2",
            uuid="uuid-r2",
            name="R2",
            klass="",
            type_="resource",
            parent="StationA",
            parent_uuid="uuid-station-a",
            data={"liquid": [{"liquid_type": "ethanol", "volume": 5.0}]},
        ),
    ]
    tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
    return tree_set


# ============ resource_helper 测试 ============


class TestResourceHelper:
    """测试 resource_helper 对 dict / ResourceDictInstance 的兼容性"""

    def test_ensure_resource_instance_from_dict(self):
        d = _make_resource_dict()
        inst = ensure_resource_instance(d)
        assert isinstance(inst, ResourceDictInstance)
        assert inst.res_content.id == "reactor_01"
        assert inst.res_content.uuid == "uuid-reactor-01"

    def test_ensure_resource_instance_passthrough(self):
        inst = _make_resource_instance()
        result = ensure_resource_instance(inst)
        assert result is inst  # 同一个对象，不复制

    def test_ensure_resource_instance_none(self):
        assert ensure_resource_instance(None) is None

    def test_get_resource_id_from_dict(self):
        d = _make_resource_dict(id="my_device")
        assert get_resource_id(d) == "my_device"

    def test_get_resource_id_from_instance(self):
        inst = _make_resource_instance(id="my_device")
        assert get_resource_id(inst) == "my_device"

    def test_get_resource_id_from_string(self):
        assert get_resource_id("my_device") == "my_device"

    def test_get_resource_id_from_wrapped_dict(self):
        """兼容 {station_id: {...}} 格式"""
        d = {"StationA": {"id": "StationA", "name": "StationA"}}
        assert get_resource_id(d) == "StationA"

    def test_get_resource_data_from_dict(self):
        d = _make_resource_dict(data={"temperature": 25.0})
        assert get_resource_data(d) == {"temperature": 25.0}

    def test_get_resource_data_from_instance(self):
        inst = _make_resource_instance(data={"temperature": 25.0})
        data = get_resource_data(inst)
        assert data["temperature"] == 25.0

    def test_get_resource_display_info_from_dict(self):
        d = _make_resource_dict(id="reactor_01", name="Reactor #1")
        info = get_resource_display_info(d)
        assert "reactor_01" in info
        assert "Reactor #1" in info

    def test_get_resource_display_info_from_instance(self):
        inst = _make_resource_instance(id="reactor_01", name="Reactor #1")
        info = get_resource_display_info(inst)
        assert "reactor_01" in info

    def test_get_resource_display_info_from_string(self):
        assert get_resource_display_info("reactor_01") == "reactor_01"

    def test_get_resource_liquid_volume(self):
        d = _make_resource_dict(data={"liquid": [{"liquid_type": "water", "volume": 15.5}]})
        assert get_resource_liquid_volume(d) == pytest.approx(15.5)

    def test_resource_to_dict_from_instance(self):
        inst = _make_resource_instance(id="reactor_01", klass="virtual_stirrer")
        d = resource_to_dict(inst)
        assert isinstance(d, dict)
        assert d["id"] == "reactor_01"
        assert d["class"] == "virtual_stirrer"

    def test_resource_to_dict_passthrough(self):
        d = _make_resource_dict()
        result = resource_to_dict(d)
        assert result is d  # 同一个 dict


# ============ vessel_parser 兼容性测试 ============


class TestVesselParser:
    """测试 vessel_parser.get_vessel 对 ResourceDictInstance 的兼容"""

    def test_get_vessel_from_dict(self):
        d = _make_resource_dict(id="reactor_01", data={"temperature": 25.0})
        vessel_id, vessel_data = get_vessel(d)
        assert vessel_id == "reactor_01"
        assert vessel_data["temperature"] == 25.0

    def test_get_vessel_from_string(self):
        vessel_id, vessel_data = get_vessel("reactor_01")
        assert vessel_id == "reactor_01"
        assert vessel_data == {}

    def test_get_vessel_from_resource_instance(self):
        inst = _make_resource_instance(id="reactor_01", data={"temperature": 25.0})
        vessel_id, vessel_data = get_vessel(inst)
        assert vessel_id == "reactor_01"
        assert vessel_data["temperature"] == 25.0

    def test_get_vessel_from_wrapped_dict(self):
        """兼容 {station_id: {id: ..., data: {...}}} 格式"""
        d = {"StationA": {"id": "StationA", "data": {"vol": 100}}}
        vessel_id, vessel_data = get_vessel(d)
        assert vessel_id == "StationA"


# ============ ResourceTreeSet → get_plr_nested_dict 测试 ============


class TestResourceTreeRoundTrip:
    """测试 ResourceTreeSet → get_plr_nested_dict 保留树结构和关键字段"""

    def test_tree_preserves_children(self):
        tree_set = _make_tree_with_children()
        assert len(tree_set.trees) == 1
        root = tree_set.trees[0].root_node
        assert root.res_content.id == "StationA"
        assert len(root.children) == 2

    def test_plr_nested_dict_has_children(self):
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        assert isinstance(nested, dict)
        assert "children" in nested
        assert isinstance(nested["children"], dict)
        assert "R1" in nested["children"]
        assert "R2" in nested["children"]

    def test_plr_nested_dict_preserves_uuid(self):
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        assert nested["uuid"] == "uuid-station-a"
        assert nested["children"]["R1"]["uuid"] == "uuid-r1"

    def test_plr_nested_dict_preserves_klass(self):
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        assert nested["class"] == "workstation"

    def test_plr_nested_dict_preserves_data(self):
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        r1_data = nested["children"]["R1"]["data"]
        assert "liquid" in r1_data
        assert r1_data["liquid"][0]["volume"] == 10.0

    def test_plr_nested_dict_usable_by_get_vessel(self):
        """get_plr_nested_dict 的结果可以直接传给 get_vessel"""
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        vessel_id, vessel_data = get_vessel(nested)
        assert vessel_id == "StationA"

    def test_dump_vs_plr_nested_dict(self):
        """dump() 是扁平化的，get_plr_nested_dict 保留树结构"""
        tree_set = _make_tree_with_children()
        # dump 返回扁平列表
        dumped = tree_set.dump()
        assert isinstance(dumped[0], list)
        assert len(dumped[0]) == 3  # StationA + R1 + R2，全部扁平

        # get_plr_nested_dict 保留嵌套
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()
        assert isinstance(nested["children"], dict)
        assert len(nested["children"]) == 2  # 嵌套的 children


# ============ 模拟 workstation 路径测试 ============


class TestWorkstationPath:
    """模拟 workstation.py 中的关键路径:
    raw_data → ResourceTreeSet.from_raw_dict_list → get_plr_nested_dict → compiler
    """

    def test_single_resource_path(self):
        """单个 Resource: 取第一棵树的根节点"""
        raw_data = [
            _make_resource_dict(id="reactor_01", uuid="uuid-r01", klass="virtual_stirrer"),
        ]
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        root = tree_set.trees[0].root_node
        result = root.get_plr_nested_dict()
        assert result["id"] == "reactor_01"
        assert result["uuid"] == "uuid-r01"
        assert result["class"] == "virtual_stirrer"

    def test_resource_with_children_path(self):
        """Resource 带 children: AGV/batch transfer 场景"""
        tree_set = _make_tree_with_children()
        root = tree_set.trees[0].root_node
        nested = root.get_plr_nested_dict()

        # 模拟编译器接收到的参数
        from_repo = {"StationA": nested}
        assert "A01" not in from_repo["StationA"]["children"]  # children 按 id 索引
        assert "R1" in from_repo["StationA"]["children"]
        assert from_repo["StationA"]["children"]["R1"]["uuid"] == "uuid-r1"

    def test_multiple_resource_path(self):
        """多个 Resource: 每棵树取根节点"""
        raw_data1 = [_make_resource_dict(id="R1", uuid="uuid-r1")]
        raw_data2 = [_make_resource_dict(id="R2", uuid="uuid-r2")]
        # 模拟 host 返回多棵树
        tree_set1 = ResourceTreeSet.from_raw_dict_list(raw_data1)
        tree_set2 = ResourceTreeSet.from_raw_dict_list(raw_data2)
        results = [
            tree.root_node.get_plr_nested_dict()
            for ts in [tree_set1, tree_set2]
            for tree in ts.trees
        ]
        assert len(results) == 2
        assert results[0]["id"] == "R1"
        assert results[1]["id"] == "R2"
