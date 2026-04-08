"""
批量转运编译器测试

覆盖：单物料退化、刚好一批、多批次、空操作、AGV 配置发现、children dict 状态。
"""

import pytest
import networkx as nx

from unilabos.compile.batch_transfer_protocol import generate_batch_transfer_protocol
from unilabos.compile.agv_transfer_protocol import generate_agv_transfer_protocol
from unilabos.compile._agv_utils import find_agv_config, get_agv_capacity, split_batches


# ============ 构建测试用设备图 ============

def _make_graph(capacity_x=2, capacity_y=1, capacity_z=1):
    """构建包含 AGV 节点的测试设备图"""
    G = nx.DiGraph()

    # AGV 节点
    G.add_node("AGV", **{
        "type": "device",
        "class_": "agv_transport_station",
        "config": {
            "protocol_type": ["AGVTransferProtocol", "BatchTransferProtocol"],
            "device_roles": {
                "navigator": "zhixing_agv",
                "arm": "zhixing_ur_arm"
            },
            "route_table": {
                "StationA->StationB": {
                    "nav_command": '{"target": "LM1"}',
                    "arm_pick": '{"task_name": "pick.urp"}',
                    "arm_place": '{"task_name": "place.urp"}'
                },
                "AGV->StationA": {
                    "nav_command": '{"target": "LM1"}',
                    "arm_pick": '{"task_name": "pick.urp"}',
                    "arm_place": '{"task_name": "place.urp"}'
                },
                "StationA->StationA": {
                    "nav_command": '{"target": "LM1"}',
                    "arm_pick": '{"task_name": "pick.urp"}',
                    "arm_place": '{"task_name": "place.urp"}'
                },
            }
        }
    })

    # AGV 子设备
    G.add_node("zhixing_agv", type="device", class_="zhixing_agv")
    G.add_node("zhixing_ur_arm", type="device", class_="zhixing_ur_arm")
    G.add_edge("AGV", "zhixing_agv")
    G.add_edge("AGV", "zhixing_ur_arm")

    # AGV Warehouse 子资源
    G.add_node("agv_platform", **{
        "type": "warehouse",
        "config": {
            "name": "agv_platform",
            "num_items_x": capacity_x,
            "num_items_y": capacity_y,
            "num_items_z": capacity_z,
        }
    })
    G.add_edge("AGV", "agv_platform")

    # 来源/目标工站
    G.add_node("StationA", type="device", class_="workstation")
    G.add_node("StationB", type="device", class_="workstation")

    return G


def _make_repos(items_count=2):
    """构建测试用的 from_repo 和 to_repo dict"""
    children = {}
    for i in range(items_count):
        pos = f"A{i + 1:02d}"
        children[pos] = {
            "id": f"resource_{i + 1}",
            "name": f"R{i + 1}",
            "parent": "StationA",
            "type": "resource",
        }

    from_repo = {
        "StationA": {
            "id": "StationA",
            "name": "StationA",
            "children": children,
        }
    }
    to_repo = {
        "StationB": {
            "id": "StationB",
            "name": "StationB",
            "children": {},
        }
    }
    return from_repo, to_repo


def _make_items(count=2):
    """构建 transfer_resources / from_positions / to_positions"""
    resources = [
        {
            "id": f"resource_{i + 1}",
            "name": f"R{i + 1}",
            "sample_id": f"uuid-{i + 1}",
            "parent": "StationA",
            "type": "resource",
        }
        for i in range(count)
    ]
    from_positions = [f"A{i + 1:02d}" for i in range(count)]
    to_positions = [f"A{i + 1:02d}" for i in range(count)]
    return resources, from_positions, to_positions


# ============ _agv_utils 测试 ============

class TestAGVUtils:
    def test_find_agv_config(self):
        G = _make_graph()
        cfg = find_agv_config(G)
        assert cfg["agv_id"] == "AGV"
        assert cfg["device_roles"]["navigator"] == "zhixing_agv"
        assert cfg["device_roles"]["arm"] == "zhixing_ur_arm"
        assert "StationA->StationB" in cfg["route_table"]

    def test_find_agv_config_by_id(self):
        G = _make_graph()
        cfg = find_agv_config(G, agv_id="AGV")
        assert cfg["agv_id"] == "AGV"

    def test_find_agv_config_not_found(self):
        G = nx.DiGraph()
        G.add_node("SomeDevice", type="device", class_="pump")
        with pytest.raises(ValueError, match="未找到 AGV"):
            find_agv_config(G)

    def test_get_agv_capacity(self):
        G = _make_graph(capacity_x=2, capacity_y=1, capacity_z=1)
        assert get_agv_capacity(G, "AGV") == 2

    def test_get_agv_capacity_multi_layer(self):
        G = _make_graph(capacity_x=1, capacity_y=2, capacity_z=3)
        assert get_agv_capacity(G, "AGV") == 6

    def test_split_batches_exact(self):
        assert split_batches([1, 2], 2) == [[1, 2]]

    def test_split_batches_overflow(self):
        assert split_batches([1, 2, 3], 2) == [[1, 2], [3]]

    def test_split_batches_single(self):
        assert split_batches([1], 4) == [[1]]

    def test_split_batches_zero_capacity(self):
        with pytest.raises(ValueError):
            split_batches([1], 0)


# ============ 批量转运编译器测试 ============

class TestBatchTransferProtocol:
    def test_empty_items(self):
        """空物料列表返回空 steps"""
        G = _make_graph()
        from_repo, to_repo = _make_repos(0)
        steps = generate_batch_transfer_protocol(G, from_repo, to_repo, [], [], [])
        assert steps == []

    def test_single_item(self):
        """单物料转运（BatchTransfer 退化为单物料）"""
        G = _make_graph(capacity_x=2)
        from_repo, to_repo = _make_repos(1)
        resources, from_pos, to_pos = _make_items(1)
        steps = generate_batch_transfer_protocol(G, from_repo, to_repo, resources, from_pos, to_pos)

        # 应该有: nav到来源 + 1个pick + nav到目标 + 1个place = 4 steps
        assert len(steps) == 4
        assert steps[0]["action_name"] == "send_nav_task"
        assert steps[1]["action_name"] == "move_pos_task"
        assert steps[1]["_transfer_meta"]["phase"] == "pick"
        assert steps[2]["action_name"] == "send_nav_task"
        assert steps[3]["action_name"] == "move_pos_task"
        assert steps[3]["_transfer_meta"]["phase"] == "place"

    def test_exact_capacity(self):
        """物料数 = AGV 容量，刚好一批"""
        G = _make_graph(capacity_x=2)
        from_repo, to_repo = _make_repos(2)
        resources, from_pos, to_pos = _make_items(2)
        steps = generate_batch_transfer_protocol(G, from_repo, to_repo, resources, from_pos, to_pos)

        # nav + 2 pick + nav + 2 place = 6 steps
        assert len(steps) == 6
        pick_steps = [s for s in steps if s.get("_transfer_meta", {}).get("phase") == "pick"]
        place_steps = [s for s in steps if s.get("_transfer_meta", {}).get("phase") == "place"]
        assert len(pick_steps) == 2
        assert len(place_steps) == 2

    def test_multi_batch(self):
        """物料数 > AGV 容量，自动分批"""
        G = _make_graph(capacity_x=2)
        from_repo, to_repo = _make_repos(3)
        resources, from_pos, to_pos = _make_items(3)
        steps = generate_batch_transfer_protocol(G, from_repo, to_repo, resources, from_pos, to_pos)

        # 批次1: nav + 2 pick + nav + 2 place + nav(返回) = 7
        # 批次2: nav + 1 pick + nav + 1 place = 4
        # 总计 11 steps
        assert len(steps) == 11

        nav_steps = [s for s in steps if s["action_name"] == "send_nav_task"]
        # 批次1: 2 nav(去来源+去目标) + 1 nav(返回) + 批次2: 2 nav = 5 nav
        assert len(nav_steps) == 5

    def test_children_dict_updated(self):
        """compile 阶段三方 children dict 状态正确"""
        G = _make_graph(capacity_x=2)
        from_repo, to_repo = _make_repos(2)
        resources, from_pos, to_pos = _make_items(2)

        assert "A01" in from_repo["StationA"]["children"]
        assert "A02" in from_repo["StationA"]["children"]
        assert len(to_repo["StationB"]["children"]) == 0

        generate_batch_transfer_protocol(G, from_repo, to_repo, resources, from_pos, to_pos)

        # compile 后 from_repo 的 children 应该被 pop 掉
        assert "A01" not in from_repo["StationA"]["children"]
        assert "A02" not in from_repo["StationA"]["children"]
        # to_repo 应该有新物料
        assert "A01" in to_repo["StationB"]["children"]
        assert "A02" in to_repo["StationB"]["children"]
        assert to_repo["StationB"]["children"]["A01"]["id"] == "resource_1"

    def test_device_ids_from_config(self):
        """设备 ID 全部从配置读取，不硬编码"""
        G = _make_graph()
        from_repo, to_repo = _make_repos(1)
        resources, from_pos, to_pos = _make_items(1)
        steps = generate_batch_transfer_protocol(G, from_repo, to_repo, resources, from_pos, to_pos)

        device_ids = {s["device_id"] for s in steps}
        assert "zhixing_agv" in device_ids
        assert "zhixing_ur_arm" in device_ids

    def test_route_not_found(self):
        """路由表中无对应路线时报错"""
        G = _make_graph()
        from_repo = {"Unknown": {"id": "Unknown", "children": {"A01": {"id": "R1", "parent": "Unknown"}}}}
        to_repo = {"Other": {"id": "Other", "children": {}}}
        resources = [{"id": "R1", "name": "R1"}]
        with pytest.raises(KeyError, match="路由表"):
            generate_batch_transfer_protocol(G, from_repo, to_repo, resources, ["A01"], ["B01"])

    def test_length_mismatch(self):
        """三个数组长度不一致时报错"""
        G = _make_graph()
        from_repo, to_repo = _make_repos(2)
        resources = [{"id": "R1"}]
        with pytest.raises(ValueError, match="长度不一致"):
            generate_batch_transfer_protocol(G, from_repo, to_repo, resources, ["A01", "A02"], ["B01"])


# ============ 改造后的 AGV 单物料编译器测试 ============

class TestAGVTransferProtocol:
    def test_single_transfer_from_config(self):
        """改造后的单物料编译器从 G 读取配置"""
        G = _make_graph()
        from_repo = {"StationA": {"id": "StationA", "children": {"A01": {"id": "R1", "parent": "StationA"}}}}
        to_repo = {"StationB": {"id": "StationB", "children": {}}}
        steps = generate_agv_transfer_protocol(G, from_repo, "A01", to_repo, "B01")

        assert len(steps) == 2
        assert steps[0]["device_id"] == "zhixing_agv"
        assert steps[0]["action_name"] == "send_nav_task"
        assert steps[1]["device_id"] == "zhixing_ur_arm"
        assert steps[1]["action_name"] == "move_pos_task"

    def test_children_updated(self):
        """单物料编译后 children dict 正确更新"""
        G = _make_graph()
        from_repo = {"StationA": {"id": "StationA", "children": {"A01": {"id": "R1", "parent": "StationA"}}}}
        to_repo = {"StationB": {"id": "StationB", "children": {}}}
        generate_agv_transfer_protocol(G, from_repo, "A01", to_repo, "B01")

        assert "A01" not in from_repo["StationA"]["children"]
        assert "B01" in to_repo["StationB"]["children"]
        assert to_repo["StationB"]["children"]["B01"]["parent"] == "StationB"
