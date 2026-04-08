"""
全链路集成测试：ROS Goal 转换 → ResourceTreeSet → get_plr_nested_dict → 编译器 → 动作列表

模拟 workstation.py 中的完整路径：
1. host 返回 raw_data（模拟 resource_get 响应）
2. ResourceTreeSet.from_raw_dict_list(raw_data) 构建资源树
3. tree.root_node.get_plr_nested_dict() 生成嵌套 dict
4. protocol_kwargs 传给编译器
5. 编译器返回 action_list，验证结构和关键字段
"""

import copy
import json
import pytest
import networkx as nx

from unilabos.resources.resource_tracker import (
    ResourceDictInstance,
    ResourceTreeSet,
)
from unilabos.compile.utils.resource_helper import (
    ensure_resource_instance,
    resource_to_dict,
    get_resource_id,
    get_resource_data,
)
from unilabos.compile.utils.vessel_parser import get_vessel

# ============ 构建模拟设备图 ============

def _build_test_graph():
    """构建一个包含常用设备节点的测试图"""
    G = nx.DiGraph()

    # 容器
    G.add_node("reactor_01", **{
        "id": "reactor_01",
        "name": "reactor_01",
        "type": "device",
        "class": "virtual_stirrer",
        "data": {},
        "config": {},
    })

    # 搅拌设备
    G.add_node("stirrer_1", **{
        "id": "stirrer_1",
        "name": "stirrer_1",
        "type": "device",
        "class": "virtual_stirrer",
        "data": {},
        "config": {},
    })
    G.add_edge("stirrer_1", "reactor_01")

    # 加热设备
    G.add_node("heatchill_1", **{
        "id": "heatchill_1",
        "name": "heatchill_1",
        "type": "device",
        "class": "virtual_heatchill",
        "data": {},
        "config": {},
    })
    G.add_edge("heatchill_1", "reactor_01")

    # 试剂容器（液体）
    G.add_node("flask_water", **{
        "id": "flask_water",
        "name": "flask_water",
        "type": "container",
        "class": "",
        "data": {"reagent_name": "water", "liquid": [{"liquid_type": "water", "volume": 500.0}]},
        "config": {"reagent": "water"},
    })

    # 固体加样器
    G.add_node("solid_dispenser_1", **{
        "id": "solid_dispenser_1",
        "name": "solid_dispenser_1",
        "type": "device",
        "class": "solid_dispenser",
        "data": {},
        "config": {},
    })

    # 泵
    G.add_node("pump_1", **{
        "id": "pump_1",
        "name": "pump_1",
        "type": "device",
        "class": "virtual_pump",
        "data": {},
        "config": {},
    })
    G.add_edge("flask_water", "pump_1")
    G.add_edge("pump_1", "reactor_01")

    return G


# ============ 构建模拟 host 返回数据 ============

def _make_raw_resource(
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
    """模拟 host 返回的单个资源 dict（与 resource_get 服务响应一致）"""
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
        "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    }


def _simulate_workstation_resource_enrichment(raw_data_list, field_type="unilabos_msgs/Resource"):
    """
    模拟 workstation.py 中 resource enrichment 的核心逻辑：
    raw_data → ResourceTreeSet.from_raw_dict_list → get_plr_nested_dict → protocol_kwargs[k]
    """
    tree_set = ResourceTreeSet.from_raw_dict_list(raw_data_list)

    if field_type == "unilabos_msgs/Resource":
        # 单个 Resource：取第一棵树的根节点
        root_instance = tree_set.trees[0].root_node if tree_set.trees else None
        return root_instance.get_plr_nested_dict() if root_instance else {}
    else:
        # sequence<Resource>：返回列表
        return [tree.root_node.get_plr_nested_dict() for tree in tree_set.trees]


# ============ 全链路测试：Stir 协议 ============

class TestStirProtocolFullChain:
    """Stir 协议全链路：host raw_data → enriched dict → compiler → action_list"""

    def test_stir_with_enriched_resource_dict(self):
        """单个 Resource 经过 enrichment 后传给 stir compiler"""
        from unilabos.compile.stir_protocol import generate_stir_protocol

        raw_data = [_make_raw_resource(
            id="reactor_01", uuid="uuid-reactor-01",
            klass="virtual_stirrer", type_="device",
        )]

        # 模拟 workstation enrichment
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        assert enriched_vessel["id"] == "reactor_01"
        assert enriched_vessel["uuid"] == "uuid-reactor-01"
        assert enriched_vessel["class"] == "virtual_stirrer"

        # 传给编译器
        G = _build_test_graph()
        actions = generate_stir_protocol(
            G=G,
            vessel=enriched_vessel,
            time="60",
            stir_speed=300.0,
        )

        assert isinstance(actions, list)
        assert len(actions) >= 1
        action = actions[0]
        assert action["device_id"] == "stirrer_1"
        assert action["action_name"] == "stir"
        assert "vessel" in action["action_kwargs"]
        assert action["action_kwargs"]["vessel"]["id"] == "reactor_01"

    def test_stir_with_resource_dict_instance(self):
        """直接用 ResourceDictInstance 传给 stir compiler（通过 get_plr_nested_dict 转换）"""
        from unilabos.compile.stir_protocol import generate_stir_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        inst = tree_set.trees[0].root_node

        # 通过 resource_to_dict 转换（resource_helper 兼容层）
        vessel_dict = resource_to_dict(inst)
        assert isinstance(vessel_dict, dict)
        assert vessel_dict["id"] == "reactor_01"

        G = _build_test_graph()
        actions = generate_stir_protocol(G=G, vessel=vessel_dict, time="30")

        assert len(actions) >= 1
        assert actions[0]["action_name"] == "stir"

    def test_stir_with_string_vessel(self):
        """兼容旧模式：直接传 vessel 字符串"""
        from unilabos.compile.stir_protocol import generate_stir_protocol

        G = _build_test_graph()
        actions = generate_stir_protocol(G=G, vessel="reactor_01", time="30")

        assert len(actions) >= 1
        assert actions[0]["device_id"] == "stirrer_1"
        assert actions[0]["action_kwargs"]["vessel"]["id"] == "reactor_01"


# ============ 全链路测试：HeatChill 协议 ============

class TestHeatChillProtocolFullChain:
    """HeatChill 协议全链路"""

    def test_heatchill_with_enriched_resource(self):
        from unilabos.compile.heatchill_protocol import generate_heat_chill_protocol

        raw_data = [_make_raw_resource(id="reactor_01", klass="virtual_stirrer")]
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_heat_chill_protocol(
            G=G,
            vessel=enriched_vessel,
            temp=80.0,
            time="300",
        )

        assert isinstance(actions, list)
        assert len(actions) >= 1
        action = actions[0]
        assert action["device_id"] == "heatchill_1"
        assert action["action_name"] == "heat_chill"
        assert action["action_kwargs"]["temp"] == 80.0

    def test_heatchill_start_with_enriched_resource(self):
        from unilabos.compile.heatchill_protocol import generate_heat_chill_start_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_heat_chill_start_protocol(
            G=G,
            vessel=enriched_vessel,
            temp=60.0,
        )

        assert len(actions) >= 1
        assert actions[0]["action_name"] == "heat_chill_start"
        assert actions[0]["action_kwargs"]["temp"] == 60.0

    def test_heatchill_stop_with_enriched_resource(self):
        from unilabos.compile.heatchill_protocol import generate_heat_chill_stop_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_heat_chill_stop_protocol(G=G, vessel=enriched_vessel)

        assert len(actions) >= 1
        assert actions[0]["action_name"] == "heat_chill_stop"


# ============ 全链路测试：Add 协议 ============

class TestAddProtocolFullChain:
    """Add 协议全链路：vessel enrichment + reagent 查找 + 泵传输"""

    def test_add_solid_with_enriched_resource(self):
        from unilabos.compile.add_protocol import generate_add_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_add_protocol(
            G=G,
            vessel=enriched_vessel,
            reagent="NaCl",
            mass="5 g",
        )

        assert isinstance(actions, list)
        assert len(actions) >= 1
        # 应该包含至少一个 add_solid 或 log_message 动作
        action_names = [a.get("action_name", "") for a in actions]
        assert any(name in ["add_solid", "log_message"] for name in action_names)

    def test_add_liquid_with_enriched_resource(self):
        from unilabos.compile.add_protocol import generate_add_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched_vessel = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_add_protocol(
            G=G,
            vessel=enriched_vessel,
            reagent="water",
            volume="10 mL",
        )

        assert isinstance(actions, list)
        assert len(actions) >= 1


# ============ 全链路测试：ResourceDictInstance 兼容层 ============

class TestResourceDictInstanceCompatibility:
    """验证编译器兼容层对 ResourceDictInstance 的处理"""

    def test_get_vessel_from_enriched_dict(self):
        """get_vessel 对 enriched dict 的处理"""
        raw_data = [_make_raw_resource(
            id="reactor_01",
            data={"temperature": 25.0, "liquid": [{"liquid_type": "water", "volume": 10.0}]},
        )]
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        vessel_id, vessel_data = get_vessel(enriched)
        assert vessel_id == "reactor_01"
        assert vessel_data["temperature"] == 25.0
        assert len(vessel_data["liquid"]) == 1

    def test_get_vessel_from_resource_instance(self):
        """get_vessel 直接对 ResourceDictInstance 的处理"""
        raw_data = [_make_raw_resource(
            id="reactor_01",
            data={"temperature": 25.0},
        )]
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        inst = tree_set.trees[0].root_node

        vessel_id, vessel_data = get_vessel(inst)
        assert vessel_id == "reactor_01"
        assert vessel_data["temperature"] == 25.0

    def test_ensure_resource_instance_round_trip(self):
        """ensure_resource_instance → resource_to_dict 无损往返"""
        raw_data = [_make_raw_resource(
            id="reactor_01", uuid="uuid-r01", klass="virtual_stirrer",
            data={"temp": 25.0},
        )]
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        # dict → ResourceDictInstance
        inst = ensure_resource_instance(enriched)
        assert isinstance(inst, ResourceDictInstance)
        assert inst.res_content.id == "reactor_01"
        assert inst.res_content.uuid == "uuid-r01"

        # ResourceDictInstance → dict
        d = resource_to_dict(inst)
        assert isinstance(d, dict)
        assert d["id"] == "reactor_01"
        assert d["uuid"] == "uuid-r01"
        assert d["class"] == "virtual_stirrer"


# ============ 全链路测试：带 children 的资源树 ============

class TestResourceTreeWithChildren:
    """测试带 children 结构的资源树通过编译器的路径"""

    def _make_tree_with_children(self):
        """构建 StationA -> [Flask1, Flask2] 的资源树"""
        return [
            _make_raw_resource(
                id="StationA", uuid="uuid-station-a",
                name="StationA", klass="workstation", type_="device",
            ),
            _make_raw_resource(
                id="Flask1", uuid="uuid-flask-1",
                name="Flask1", klass="", type_="resource",
                parent="StationA", parent_uuid="uuid-station-a",
                data={"liquid": [{"liquid_type": "water", "volume": 10.0}]},
            ),
            _make_raw_resource(
                id="Flask2", uuid="uuid-flask-2",
                name="Flask2", klass="", type_="resource",
                parent="StationA", parent_uuid="uuid-station-a",
                data={"liquid": [{"liquid_type": "ethanol", "volume": 5.0}]},
            ),
        ]

    def test_enrichment_preserves_children_structure(self):
        """验证 enrichment 后 children 为嵌套 dict"""
        raw_data = self._make_tree_with_children()
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        assert enriched["id"] == "StationA"
        assert "children" in enriched
        assert isinstance(enriched["children"], dict)
        assert "Flask1" in enriched["children"]
        assert "Flask2" in enriched["children"]

    def test_children_preserve_uuid_and_data(self):
        """验证 children 中的 uuid 和 data 被正确保留"""
        raw_data = self._make_tree_with_children()
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        flask1 = enriched["children"]["Flask1"]
        assert flask1["uuid"] == "uuid-flask-1"
        assert flask1["data"]["liquid"][0]["liquid_type"] == "water"
        assert flask1["data"]["liquid"][0]["volume"] == 10.0

        flask2 = enriched["children"]["Flask2"]
        assert flask2["uuid"] == "uuid-flask-2"
        assert flask2["data"]["liquid"][0]["liquid_type"] == "ethanol"

    def test_children_dict_can_be_popped(self):
        """模拟 batch_transfer_protocol 中 pop children 的操作"""
        raw_data = self._make_tree_with_children()
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        # batch_transfer_protocol 中会 pop children
        children = enriched["children"]
        popped = children.pop("Flask1")
        assert popped["id"] == "Flask1"
        assert "Flask1" not in enriched["children"]
        assert "Flask2" in enriched["children"]

    def test_children_dict_usable_as_from_repo(self):
        """模拟 batch_transfer_protocol 中 from_repo 参数"""
        raw_data = self._make_tree_with_children()
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        # 模拟编译器接收的 from_repo 格式
        from_repo = {"StationA": enriched}
        from_repo_ = list(from_repo.values())[0]

        assert from_repo_["id"] == "StationA"
        assert "Flask1" in from_repo_["children"]
        assert from_repo_["children"]["Flask1"]["uuid"] == "uuid-flask-1"

    def test_sequence_resource_enrichment(self):
        """sequence<Resource> 情况：多个独立资源树"""
        raw_data1 = [_make_raw_resource(id="R1", uuid="uuid-r1")]
        raw_data2 = [_make_raw_resource(id="R2", uuid="uuid-r2")]

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


# ============ 全链路测试：动作列表结构验证 ============

class TestActionListStructure:
    """验证编译器返回的 action_list 结构符合 workstation 预期"""

    def _validate_action(self, action):
        """验证单个 action dict 的结构"""
        if action.get("action_name") == "wait":
            # wait 伪动作不需要 device_id
            assert "action_kwargs" in action
            assert "time" in action["action_kwargs"]
            return

        if action.get("action_name") == "log_message":
            # log 伪动作
            assert "action_kwargs" in action
            return

        # 正常设备动作
        assert "device_id" in action, f"action 缺少 device_id: {action}"
        assert "action_name" in action, f"action 缺少 action_name: {action}"
        assert "action_kwargs" in action, f"action 缺少 action_kwargs: {action}"
        assert isinstance(action["action_kwargs"], dict)

    def test_stir_action_list_structure(self):
        from unilabos.compile.stir_protocol import generate_stir_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_stir_protocol(G=G, vessel=enriched, time="60")

        for action in actions:
            if isinstance(action, list):
                # 并行动作
                for sub_action in action:
                    self._validate_action(sub_action)
            else:
                self._validate_action(action)

    def test_heatchill_action_list_structure(self):
        from unilabos.compile.heatchill_protocol import generate_heat_chill_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_heat_chill_protocol(G=G, vessel=enriched, temp=80.0, time="60")

        for action in actions:
            if isinstance(action, list):
                for sub_action in action:
                    self._validate_action(sub_action)
            else:
                self._validate_action(action)

    def test_add_action_list_structure(self):
        from unilabos.compile.add_protocol import generate_add_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched = _simulate_workstation_resource_enrichment(raw_data)

        G = _build_test_graph()
        actions = generate_add_protocol(G=G, vessel=enriched, reagent="NaCl", mass="5 g")

        for action in actions:
            if isinstance(action, list):
                for sub_action in action:
                    self._validate_action(sub_action)
            else:
                self._validate_action(action)


# ============ 全链路测试：message_converter 到 enrichment ============

class TestMessageConverterToEnrichment:
    """模拟从 ROS 消息转换后的 dict 到 enrichment 的完整链路"""

    def test_ros_goal_conversion_simulation(self):
        """
        模拟 workstation.py 中的完整流程：
        1. ROS goal 中的 vessel 字段被 convert_from_ros_msg 转换为浅层 dict
        2. workstation 用 resource_id 请求 host 获取完整资源数据
        3. ResourceTreeSet.from_raw_dict_list 构建资源树
        4. get_plr_nested_dict 生成嵌套 dict 替换 protocol_kwargs[k]
        """
        # 步骤1: 模拟 convert_from_ros_msg 的输出（浅层 dict，只有 id 等基本字段）
        shallow_vessel = {
            "id": "reactor_01",
            "uuid": "uuid-reactor-01",
            "name": "reactor_01",
            "type": "device",
            "category": "virtual_stirrer",
            "children": [],
            "parent": "",
            "parent_uuid": "",
            "config": {},
            "data": {},
            "extra": {},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        }

        protocol_kwargs = {
            "vessel": shallow_vessel,
            "time": "300",
            "stir_speed": 300.0,
        }

        # 步骤2: 提取 resource_id
        resource_id = protocol_kwargs["vessel"]["id"]
        assert resource_id == "reactor_01"

        # 步骤3: 模拟 host 返回完整数据（带 children）
        host_response = [
            _make_raw_resource(
                id="reactor_01", uuid="uuid-reactor-01",
                klass="virtual_stirrer", type_="device",
                data={"temperature": 25.0, "pressure": 1.0},
                config={"max_temp": 300.0},
            ),
        ]

        # 步骤4: enrichment
        enriched = _simulate_workstation_resource_enrichment(host_response)
        protocol_kwargs["vessel"] = enriched

        # 验证 enrichment 后的 protocol_kwargs
        assert protocol_kwargs["vessel"]["id"] == "reactor_01"
        assert protocol_kwargs["vessel"]["uuid"] == "uuid-reactor-01"
        assert protocol_kwargs["vessel"]["class"] == "virtual_stirrer"
        assert protocol_kwargs["vessel"]["data"]["temperature"] == 25.0
        assert protocol_kwargs["vessel"]["config"]["max_temp"] == 300.0

        # 步骤5: 传给编译器
        from unilabos.compile.stir_protocol import generate_stir_protocol
        G = _build_test_graph()
        actions = generate_stir_protocol(G=G, **protocol_kwargs)

        assert len(actions) >= 1
        assert actions[0]["device_id"] == "stirrer_1"
        assert actions[0]["action_name"] == "stir"

    def test_ros_goal_with_children_enrichment(self):
        """ROS goal → enrichment 带 children 的场景（batch transfer）"""
        # 模拟 host 返回带 children 的数据
        host_response = [
            _make_raw_resource(
                id="StationA", uuid="uuid-sa", klass="workstation", type_="device",
                config={"num_items_x": 4, "num_items_y": 2},
            ),
            _make_raw_resource(
                id="Plate1", uuid="uuid-p1", type_="resource",
                parent="StationA", parent_uuid="uuid-sa",
                data={"sample": "sample_A"},
            ),
            _make_raw_resource(
                id="Plate2", uuid="uuid-p2", type_="resource",
                parent="StationA", parent_uuid="uuid-sa",
                data={"sample": "sample_B"},
            ),
        ]

        enriched = _simulate_workstation_resource_enrichment(host_response)

        assert enriched["id"] == "StationA"
        assert enriched["class"] == "workstation"
        assert len(enriched["children"]) == 2
        assert enriched["children"]["Plate1"]["data"]["sample"] == "sample_A"
        assert enriched["children"]["Plate2"]["uuid"] == "uuid-p2"

        # 模拟 batch_transfer 的 from_repo 格式
        from_repo = {"StationA": enriched}
        from_repo_ = list(from_repo.values())[0]
        assert "Plate1" in from_repo_["children"]
        assert from_repo_["children"]["Plate1"]["uuid"] == "uuid-p1"


# ============ 全链路测试：多协议连续调用 ============

class TestMultiProtocolChain:
    """模拟连续执行多个协议（如 add → stir → heatchill）"""

    def test_sequential_protocol_execution(self):
        """模拟典型合成路径：add → stir → heatchill"""
        from unilabos.compile.stir_protocol import generate_stir_protocol
        from unilabos.compile.heatchill_protocol import generate_heat_chill_protocol
        from unilabos.compile.add_protocol import generate_add_protocol

        raw_data = [_make_raw_resource(
            id="reactor_01", uuid="uuid-reactor-01",
            klass="virtual_stirrer", type_="device",
        )]
        enriched = _simulate_workstation_resource_enrichment(raw_data)
        G = _build_test_graph()

        # 每次调用用 enriched 的副本，避免编译器修改原数据
        all_actions = []

        # 步骤1: 添加试剂
        add_actions = generate_add_protocol(
            G=G, vessel=copy.deepcopy(enriched),
            reagent="NaCl", mass="5 g",
        )
        all_actions.extend(add_actions)

        # 步骤2: 搅拌
        stir_actions = generate_stir_protocol(
            G=G, vessel=copy.deepcopy(enriched),
            time="60", stir_speed=300.0,
        )
        all_actions.extend(stir_actions)

        # 步骤3: 加热
        heat_actions = generate_heat_chill_protocol(
            G=G, vessel=copy.deepcopy(enriched),
            temp=80.0, time="300",
        )
        all_actions.extend(heat_actions)

        # 验证总动作列表
        assert len(all_actions) >= 3
        # 每个协议至少产生一个核心动作
        action_names = [a.get("action_name", "") for a in all_actions if isinstance(a, dict)]
        assert "stir" in action_names
        assert "heat_chill" in action_names

    def test_enriched_resource_not_mutated(self):
        """验证编译器不应修改传入的 enriched dict（如果需要修改应 deepcopy）"""
        from unilabos.compile.stir_protocol import generate_stir_protocol

        raw_data = [_make_raw_resource(id="reactor_01")]
        enriched = _simulate_workstation_resource_enrichment(raw_data)
        original_id = enriched["id"]
        original_uuid = enriched["uuid"]

        G = _build_test_graph()
        generate_stir_protocol(G=G, vessel=enriched, time="60")

        # 验证 enriched dict 核心字段未被修改
        assert enriched["id"] == original_id
        assert enriched["uuid"] == original_uuid
