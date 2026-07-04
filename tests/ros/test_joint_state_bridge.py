"""
P1 关节数据 & 资源跟随桥接测试 — 全面覆盖 HostNode 关节回调 + resource_pose 回调的边缘 case。

不依赖 ROS2 运行时，通过 mock 模拟 msg 和 bridge。

测试分组:
  E1: JointRepublisher JSON 输出格式 (已修复 str→json.dumps)
  E2: 关节状态回调 — 从 /joint_states (JointState msg) 直接读取 name/position
  E3: 资源跟随 (resource_pose) — 夹爪抓取/释放/多资源
  E4: 联合流程 — 关节 + 资源一并通过 bridge 发送
  E5: Bridge 调用验证
  E6: 同类型设备多实例 — 重复关节名场景
  E7: 吞吐优化 — 死区过滤、抑频、增量 resource_poses
"""

import json
import time
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace
from typing import Dict, Optional


# ==================== 辅助: 模拟 JointState msg ====================


def _make_joint_state_msg(names: list, positions: list, velocities=None, efforts=None):
    """构造模拟的 sensor_msgs/JointState 消息（不依赖 ROS2）"""
    msg = SimpleNamespace()
    msg.name = names
    msg.position = positions
    msg.velocity = velocities or [0.0] * len(names)
    msg.effort = efforts or [0.0] * len(names)
    return msg


def _make_string_msg(data: str):
    """构造模拟的 std_msgs/String 消息"""
    msg = SimpleNamespace()
    msg.data = data
    return msg


# ==================== 辅助: 提取 HostNode 核心逻辑用于隔离测试 ====================


class JointBridgeSimulator:
    """
    模拟 HostNode 的关节桥接核心逻辑（提取自 host_node.py），
    不依赖 ROS2 Node、subscription 等基础设施。

    包含吞吐优化逻辑:
    - 死区过滤 (dead band): 关节变化 < 阈值时不发送
    - 抑频 (throttle): 限制最大发送频率
    - 增量 resource_poses: 仅在变化时附带
    """

    JOINT_DEAD_BAND: float = 1e-4
    JOINT_MIN_INTERVAL: float = 0.05  # 秒

    def __init__(self, device_uuid_map: Dict[str, str],
                 dead_band: Optional[float] = None,
                 min_interval: Optional[float] = None):
        self.device_uuid_map = device_uuid_map
        self._device_ids_sorted = sorted(device_uuid_map.keys(), key=len, reverse=True)
        self._resource_poses: Dict[str, str] = {}
        self._resource_poses_dirty: bool = False
        self._last_joint_values: Dict[str, float] = {}
        self._last_send_time: float = -float("inf")  # 确保首条消息总是通过
        # 允许测试覆盖优化参数
        if dead_band is not None:
            self.JOINT_DEAD_BAND = dead_band
        if min_interval is not None:
            self.JOINT_MIN_INTERVAL = min_interval

    def resource_pose_callback(self, msg) -> None:
        """模拟 HostNode._resource_pose_callback（含变化检测）"""
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or not data:
            return
        has_change = False
        for k, v in data.items():
            if self._resource_poses.get(k) != v:
                has_change = True
                break
        if has_change:
            self._resource_poses.update(data)
            self._resource_poses_dirty = True

    def joint_state_callback(self, msg, now: Optional[float] = None) -> dict:
        """
        模拟 HostNode._joint_state_callback 核心逻辑（含优化）。
        now 参数允许测试控制时间。
        返回 {device_id: {"node_uuid": ..., "joint_states": {...}, "resource_poses": {...}}}。
        返回 {} 表示被优化过滤。
        """
        names = list(msg.name)
        positions = list(msg.position)
        if not names or len(names) != len(positions):
            return {}

        if now is None:
            now = time.time()
        resource_dirty = self._resource_poses_dirty

        # 抑频检查
        if not resource_dirty and (now - self._last_send_time) < self.JOINT_MIN_INTERVAL:
            return {}

        # 死区过滤
        has_significant_change = False
        for name, pos in zip(names, positions):
            last_val = self._last_joint_values.get(name)
            if last_val is None or abs(float(pos) - last_val) >= self.JOINT_DEAD_BAND:
                has_significant_change = True
                break

        if not has_significant_change and not resource_dirty:
            return {}

        # 更新状态
        for name, pos in zip(names, positions):
            self._last_joint_values[name] = float(pos)
        self._last_send_time = now

        # 按设备 ID 分组关节数据
        device_joints: Dict[str, Dict[str, float]] = {}
        for name, pos in zip(names, positions):
            matched_device = None
            for device_id in self._device_ids_sorted:
                if name.startswith(device_id + "_"):
                    matched_device = device_id
                    break
            if matched_device:
                if matched_device not in device_joints:
                    device_joints[matched_device] = {}
                device_joints[matched_device][name] = float(pos)
            elif len(self.device_uuid_map) == 1:
                fallback_id = self._device_ids_sorted[0]
                if fallback_id not in device_joints:
                    device_joints[fallback_id] = {}
                device_joints[fallback_id][name] = float(pos)

        # 构建设备级 resource_poses（仅 dirty 时附带）
        device_resource_poses: Dict[str, Dict[str, str]] = {}
        if resource_dirty:
            for resource_id, link_name in self._resource_poses.items():
                matched_device = None
                for device_id in self._device_ids_sorted:
                    if link_name.startswith(device_id + "_"):
                        matched_device = device_id
                        break
                if matched_device:
                    if matched_device not in device_resource_poses:
                        device_resource_poses[matched_device] = {}
                    device_resource_poses[matched_device][resource_id] = link_name
                elif len(self.device_uuid_map) == 1:
                    fallback_id = self._device_ids_sorted[0]
                    if fallback_id not in device_resource_poses:
                        device_resource_poses[fallback_id] = {}
                    device_resource_poses[fallback_id][resource_id] = link_name
            self._resource_poses_dirty = False

        result = {}
        for device_id, joint_states in device_joints.items():
            node_uuid = self.device_uuid_map.get(device_id)
            if not node_uuid:
                continue
            result[device_id] = {
                "node_uuid": node_uuid,
                "joint_states": joint_states,
                "resource_poses": device_resource_poses.get(device_id, {}),
            }
        return result


# 功能测试中禁用优化（dead_band=0, min_interval=0），确保逻辑正确性
def _make_sim(device_uuid_map: Dict[str, str]) -> JointBridgeSimulator:
    """创建禁用吞吐优化的模拟器（用于功能正确性测试）"""
    return JointBridgeSimulator(device_uuid_map, dead_band=0.0, min_interval=0.0)


# ==================== E1: JointRepublisher JSON 输出 ====================


class TestJointRepublisherFormat:
    """验证 JointRepublisher 输出标准 JSON（双引号）而非 Python repr（单引号）"""

    def test_output_is_valid_json(self):
        """str() 产生单引号，json.dumps() 产生双引号"""
        joint_dict = {
            "name": ["joint1", "joint2"],
            "position": [0.1, 0.2],
            "velocity": [0.0, 0.0],
            "effort": [0.0, 0.0],
        }
        result = json.dumps(joint_dict)
        parsed = json.loads(result)
        assert parsed["name"] == ["joint1", "joint2"]
        assert parsed["position"] == [0.1, 0.2]
        assert "'" not in result

    def test_str_produces_invalid_json(self):
        """对比: str() 不是合法 JSON"""
        joint_dict = {"name": ["joint1"], "position": [0.1]}
        result = str(joint_dict)
        with pytest.raises(json.JSONDecodeError):
            json.loads(result)


# ==================== E2: 关节状态回调（JointState msg 直接读取）====================


class TestJointStateCallback:
    """测试从 JointState msg 直接读取 name/position 的分组逻辑"""

    def test_single_device_simple(self):
        """单设备，关节名有设备前缀"""
        sim = _make_sim({"panda": "uuid-panda"})
        msg = _make_joint_state_msg(
            ["panda_joint1", "panda_joint2"], [0.5, 1.0]
        )
        result = sim.joint_state_callback(msg)
        assert "panda" in result
        assert result["panda"]["joint_states"]["panda_joint1"] == 0.5
        assert result["panda"]["joint_states"]["panda_joint2"] == 1.0

    def test_single_device_no_prefix_fallback(self):
        """单设备，关节名无设备前缀 → 应归入唯一设备"""
        sim = _make_sim({"robot1": "uuid-r1"})
        msg = _make_joint_state_msg(["joint_a", "joint_b"], [1.0, 2.0])
        result = sim.joint_state_callback(msg)
        assert "robot1" in result
        assert result["robot1"]["joint_states"]["joint_a"] == 1.0
        assert result["robot1"]["joint_states"]["joint_b"] == 2.0

    def test_multi_device_distinct_prefixes(self):
        """多设备，不同前缀，正确分组"""
        sim = _make_sim({"arm1": "uuid-arm1", "arm2": "uuid-arm2"})
        msg = _make_joint_state_msg(
            ["arm1_j1", "arm1_j2", "arm2_j1", "arm2_j2"],
            [0.1, 0.2, 0.3, 0.4],
        )
        result = sim.joint_state_callback(msg)
        assert result["arm1"]["joint_states"]["arm1_j1"] == 0.1
        assert result["arm1"]["joint_states"]["arm1_j2"] == 0.2
        assert result["arm2"]["joint_states"]["arm2_j1"] == 0.3
        assert result["arm2"]["joint_states"]["arm2_j2"] == 0.4

    def test_ambiguous_prefix_longest_wins(self):
        """前缀歧义: arm 和 arm_left — 最长前缀优先"""
        sim = _make_sim({"arm": "uuid-arm", "arm_left": "uuid-arm-left"})
        msg = _make_joint_state_msg(
            ["arm_j1", "arm_left_j1", "arm_left_j2"],
            [0.1, 0.2, 0.3],
        )
        result = sim.joint_state_callback(msg)
        assert result["arm"]["joint_states"]["arm_j1"] == 0.1
        assert result["arm_left"]["joint_states"]["arm_left_j1"] == 0.2
        assert result["arm_left"]["joint_states"]["arm_left_j2"] == 0.3

    def test_multi_device_unmatched_joints_dropped(self):
        """多设备时，无法匹配前缀的关节应被丢弃（不 fallback）"""
        sim = _make_sim({"arm1": "uuid-arm1", "arm2": "uuid-arm2"})
        msg = _make_joint_state_msg(
            ["arm1_j1", "unknown_j1"],
            [0.1, 0.9],
        )
        result = sim.joint_state_callback(msg)
        assert result["arm1"]["joint_states"]["arm1_j1"] == 0.1
        for device_id, data in result.items():
            assert "unknown_j1" not in data["joint_states"]

    def test_empty_names(self):
        """空 name 列表"""
        sim = _make_sim({"dev": "uuid-dev"})
        msg = _make_joint_state_msg([], [])
        result = sim.joint_state_callback(msg)
        assert result == {}

    def test_mismatched_lengths(self):
        """name 和 position 长度不一致"""
        sim = _make_sim({"dev": "uuid-dev"})
        msg = _make_joint_state_msg(["j1", "j2"], [0.1])
        result = sim.joint_state_callback(msg)
        assert result == {}

    def test_no_devices(self):
        """无设备 UUID 映射"""
        sim = _make_sim({})
        msg = _make_joint_state_msg(["j1"], [0.1])
        result = sim.joint_state_callback(msg)
        assert result == {}

    def test_numeric_prefix_device_ids(self):
        """数字化设备 ID (如 deck1, deck12) — deck12_slot1 不应匹配 deck1"""
        sim = _make_sim({"deck1": "uuid-d1", "deck12": "uuid-d12"})
        msg = _make_joint_state_msg(
            ["deck1_slot1", "deck12_slot1"],
            [1.0, 2.0],
        )
        result = sim.joint_state_callback(msg)
        assert result["deck1"]["joint_states"]["deck1_slot1"] == 1.0
        assert result["deck12"]["joint_states"]["deck12_slot1"] == 2.0

    def test_position_float_conversion(self):
        """position 值应强制转为 float（即使输入为 int）"""
        sim = _make_sim({"arm": "uuid-arm"})
        msg = _make_joint_state_msg(["arm_j1"], [1])
        result = sim.joint_state_callback(msg)
        assert result["arm"]["joint_states"]["arm_j1"] == 1.0
        assert isinstance(result["arm"]["joint_states"]["arm_j1"], float)

    def test_node_uuid_in_result(self):
        """结果中应携带正确的 node_uuid"""
        sim = _make_sim({"panda": "uuid-panda-123"})
        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        result = sim.joint_state_callback(msg)
        assert result["panda"]["node_uuid"] == "uuid-panda-123"

    def test_device_with_no_uuid_skipped(self):
        """device_uuid_map 中存在映射但值为空 → 跳过"""
        sim = _make_sim({"arm": ""})
        msg = _make_joint_state_msg(["arm_j1"], [0.5])
        result = sim.joint_state_callback(msg)
        assert result == {}

    def test_many_joints_single_device(self):
        """单设备大量关节（如 7-DOF arm）"""
        sim = _make_sim({"panda": "uuid-panda"})
        names = [f"panda_joint{i}" for i in range(1, 8)]
        positions = [float(i) * 0.1 for i in range(1, 8)]
        msg = _make_joint_state_msg(names, positions)
        result = sim.joint_state_callback(msg)
        assert len(result["panda"]["joint_states"]) == 7
        assert result["panda"]["joint_states"]["panda_joint7"] == pytest.approx(0.7)

    def test_duplicate_joint_names_last_wins(self):
        """同类型设备多个实例时，如果关节名完全重复（bug 场景），后出现的值覆盖前者"""
        sim = _make_sim({"dev": "uuid-dev"})
        msg = _make_joint_state_msg(["dev_j1", "dev_j1"], [1.0, 2.0])
        result = sim.joint_state_callback(msg)
        assert result["dev"]["joint_states"]["dev_j1"] == 2.0

    def test_negative_positions(self):
        """关节角度为负数"""
        sim = _make_sim({"arm": "uuid-arm"})
        msg = _make_joint_state_msg(["arm_j1", "arm_j2"], [-1.57, -3.14])
        result = sim.joint_state_callback(msg)
        assert result["arm"]["joint_states"]["arm_j1"] == pytest.approx(-1.57)
        assert result["arm"]["joint_states"]["arm_j2"] == pytest.approx(-3.14)


# ==================== E3: 资源跟随 (resource_pose) ====================


class TestResourcePoseCallback:
    """测试 resource_pose 回调 — 夹爪抓取/释放/多资源"""

    def test_single_resource_attach(self):
        """单个资源挂载到夹爪 link"""
        sim = _make_sim({"panda": "uuid-panda"})
        msg = _make_string_msg(json.dumps({"plate_1": "panda_gripper_link"}))
        sim.resource_pose_callback(msg)
        assert sim._resource_poses == {"plate_1": "panda_gripper_link"}
        assert sim._resource_poses_dirty is True

    def test_multiple_resource_attach(self):
        """多个资源同时挂载到不同 link"""
        sim = _make_sim({"panda": "uuid-panda"})
        msg = _make_string_msg(json.dumps({
            "plate_1": "panda_gripper_link",
            "tip_rack": "panda_deck_link",
        }))
        sim.resource_pose_callback(msg)
        assert sim._resource_poses["plate_1"] == "panda_gripper_link"
        assert sim._resource_poses["tip_rack"] == "panda_deck_link"

    def test_incremental_update(self):
        """增量更新：新消息合并到已有状态"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_deck_link"})))
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_2": "panda_gripper_link"})))
        assert len(sim._resource_poses) == 2
        assert sim._resource_poses["plate_1"] == "panda_deck_link"
        assert sim._resource_poses["plate_2"] == "panda_gripper_link"

    def test_resource_reattach(self):
        """资源从 deck 移动到 gripper（抓取操作）"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_deck_link"})))
        assert sim._resource_poses["plate_1"] == "panda_deck_link"
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_gripper_link"})))
        assert sim._resource_poses["plate_1"] == "panda_gripper_link"

    def test_resource_release_back_to_world(self):
        """释放资源回到 world"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_gripper_link"})))
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "world"})))
        assert sim._resource_poses["plate_1"] == "world"

    def test_empty_dict_heartbeat_no_dirty(self):
        """空 dict（心跳包）不标记 dirty"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_link"})))
        sim._resource_poses_dirty = False  # 重置
        sim.resource_pose_callback(_make_string_msg(json.dumps({})))
        assert sim._resource_poses_dirty is False  # 空 dict 不应标记 dirty

    def test_same_value_no_dirty(self):
        """重复发送相同值不应标记 dirty"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_link"})))
        sim._resource_poses_dirty = False
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_link"})))
        assert sim._resource_poses_dirty is False

    def test_invalid_json_ignored(self):
        """非法 JSON 消息不影响状态"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate_1": "panda_link"})))
        sim.resource_pose_callback(_make_string_msg("not valid json {{{"))
        assert sim._resource_poses["plate_1"] == "panda_link"

    def test_non_dict_json_ignored(self):
        """JSON 但不是 dict（如 list）应被忽略"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps(["not", "a", "dict"])))
        assert sim._resource_poses == {}

    def test_python_repr_ignored(self):
        """Python repr 格式（单引号）应被忽略"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg("{'plate_1': 'panda_link'}"))
        assert sim._resource_poses == {}

    def test_multi_device_resource_attach(self):
        """多设备场景：不同设备的 link 挂载不同资源"""
        sim = _make_sim({"arm1": "uuid-arm1", "arm2": "uuid-arm2"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "arm1_gripper_link",
            "plate_B": "arm2_gripper_link",
        })))
        assert sim._resource_poses["plate_A"] == "arm1_gripper_link"
        assert sim._resource_poses["plate_B"] == "arm2_gripper_link"


# ==================== E4: 联合流程 — 关节 + 资源一并通过 bridge ====================


class TestJointWithResourcePoses:
    """测试关节状态回调时，resource_poses 被正确按设备分组并包含在结果中"""

    def test_single_device_joint_with_resource(self):
        """单设备：关节更新时携带已挂载的资源"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_gripper_link",
        })))
        msg = _make_joint_state_msg(["panda_j1", "panda_j2"], [0.5, 1.0])
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"] == {"plate_1": "panda_gripper_link"}

    def test_single_device_no_resource(self):
        """单设备：无资源挂载时 resource_poses 为空 dict"""
        sim = _make_sim({"panda": "uuid-panda"})
        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"] == {}

    def test_multi_device_resource_routing(self):
        """多设备：资源按 link 前缀路由到正确设备"""
        sim = _make_sim({"arm1": "uuid-arm1", "arm2": "uuid-arm2"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "arm1_gripper_link",
            "plate_B": "arm2_gripper_link",
            "tube_1": "arm1_tool_link",
        })))
        msg = _make_joint_state_msg(
            ["arm1_j1", "arm2_j1"],
            [0.1, 0.2],
        )
        result = sim.joint_state_callback(msg)
        assert result["arm1"]["resource_poses"] == {
            "plate_A": "arm1_gripper_link",
            "tube_1": "arm1_tool_link",
        }
        assert result["arm2"]["resource_poses"] == {"plate_B": "arm2_gripper_link"}

    def test_resource_on_world_frame_not_routed(self):
        """资源挂在 world frame（已释放）— 多设备时无法匹配任何设备前缀"""
        sim = _make_sim({"arm1": "uuid-arm1", "arm2": "uuid-arm2"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "world",
        })))
        msg = _make_joint_state_msg(["arm1_j1"], [0.1])
        result = sim.joint_state_callback(msg)
        assert result["arm1"]["resource_poses"] == {}

    def test_resource_world_frame_single_device_fallback(self):
        """单设备时 world frame 的资源走 fallback"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "world",
        })))
        msg = _make_joint_state_msg(["panda_j1"], [0.1])
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"] == {"plate_A": "world"}

    def test_grab_and_move_sequence(self):
        """完整夹取序列: 资源在 deck → gripper 抓取 → arm 移动 → 放下"""
        sim = _make_sim({"panda": "uuid-panda"})

        # 初始: plate 在 deck
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_deck_third_link",
        })))

        msg = _make_joint_state_msg(
            ["panda_j1", "panda_j2", "panda_j3"],
            [0.0, -0.5, 1.0],
        )
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"]["plate_1"] == "panda_deck_third_link"

        # 抓取: plate 从 deck → gripper
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_gripper_link",
        })))

        msg = _make_joint_state_msg(
            ["panda_j1", "panda_j2", "panda_j3"],
            [1.57, 0.0, -0.5],
        )
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"]["plate_1"] == "panda_gripper_link"
        assert result["panda"]["joint_states"]["panda_j1"] == pytest.approx(1.57)

        # 放下: plate 从 gripper → 目标 deck
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_deck_first_link",
        })))

        msg = _make_joint_state_msg(
            ["panda_j1", "panda_j2", "panda_j3"],
            [0.0, 0.0, 0.0],
        )
        result = sim.joint_state_callback(msg)
        assert result["panda"]["resource_poses"]["plate_1"] == "panda_deck_first_link"

    def test_simultaneous_grab_multiple_resources(self):
        """同时持有多个资源（如双夹爪）"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_left_gripper",
            "plate_2": "panda_right_gripper",
            "tip_rack": "panda_deck_link",
        })))
        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        result = sim.joint_state_callback(msg)
        assert len(result["panda"]["resource_poses"]) == 3

    def test_resource_with_ambiguous_link_prefix(self):
        """link 前缀歧义: arm_left_gripper 应匹配 arm_left 而非 arm"""
        sim = _make_sim({"arm": "uuid-arm", "arm_left": "uuid-arm-left"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "arm_gripper_link",
            "plate_B": "arm_left_gripper_link",
        })))
        msg = _make_joint_state_msg(
            ["arm_j1", "arm_left_j1"],
            [0.1, 0.2],
        )
        result = sim.joint_state_callback(msg)
        assert result["arm"]["resource_poses"] == {"plate_A": "arm_gripper_link"}
        assert result["arm_left"]["resource_poses"] == {"plate_B": "arm_left_gripper_link"}


# ==================== E5: Bridge 调用验证 ====================


class TestBridgeCalls:
    """验证完整桥接流: callback → bridge.publish_joint_state 调用"""

    def test_bridge_called_per_device(self):
        """每个设备调用一次 publish_joint_state"""
        device_uuid_map = {"arm1": "uuid-111", "arm2": "uuid-222"}
        sim = _make_sim(device_uuid_map)
        bridge = MagicMock()
        bridge.publish_joint_state = MagicMock()

        msg = _make_joint_state_msg(
            ["arm1_j1", "arm2_j1"],
            [1.0, 2.0],
        )
        result = sim.joint_state_callback(msg)

        for device_id, data in result.items():
            bridge.publish_joint_state(
                data["node_uuid"], data["joint_states"], data["resource_poses"]
            )

        assert bridge.publish_joint_state.call_count == 2
        call_uuids = {c[0][0] for c in bridge.publish_joint_state.call_args_list}
        assert call_uuids == {"uuid-111", "uuid-222"}

    def test_bridge_called_with_resource_poses(self):
        """bridge 调用时携带 resource_poses"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_1": "panda_gripper_link",
        })))

        bridge = MagicMock()
        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        result = sim.joint_state_callback(msg)

        for device_id, data in result.items():
            bridge.publish_joint_state(
                data["node_uuid"], data["joint_states"], data["resource_poses"]
            )

        bridge.publish_joint_state.assert_called_once_with(
            "uuid-panda",
            {"panda_j1": 0.5},
            {"plate_1": "panda_gripper_link"},
        )

    def test_bridge_no_call_for_empty_joints(self):
        """无关节数据时不调用 bridge"""
        sim = _make_sim({"panda": "uuid-panda"})
        bridge = MagicMock()

        msg = _make_joint_state_msg([], [])
        result = sim.joint_state_callback(msg)

        for device_id, data in result.items():
            bridge.publish_joint_state(
                data["node_uuid"], data["joint_states"], data["resource_poses"]
            )

        bridge.publish_joint_state.assert_not_called()

    def test_bridge_resource_poses_empty_when_no_resources(self):
        """无资源挂载时，resource_poses 参数为空 dict"""
        sim = _make_sim({"panda": "uuid-panda"})
        bridge = MagicMock()

        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        result = sim.joint_state_callback(msg)

        for device_id, data in result.items():
            bridge.publish_joint_state(
                data["node_uuid"], data["joint_states"], data["resource_poses"]
            )

        bridge.publish_joint_state.assert_called_once_with(
            "uuid-panda",
            {"panda_j1": 0.5},
            {},
        )

    def test_multi_bridge_all_called(self):
        """多个 bridge 都应被调用"""
        sim = _make_sim({"arm": "uuid-arm"})
        bridges = [MagicMock(), MagicMock()]

        msg = _make_joint_state_msg(["arm_j1"], [0.5])
        result = sim.joint_state_callback(msg)

        for device_id, data in result.items():
            for bridge in bridges:
                bridge.publish_joint_state(
                    data["node_uuid"], data["joint_states"], data["resource_poses"]
                )

        for bridge in bridges:
            bridge.publish_joint_state.assert_called_once()


# ==================== E6: 同类型设备多个实例 — 重复关节名场景 ====================


class TestDuplicateDeviceTypes:
    """
    多个同类型设备（如 2 个 OT-2 移液器），关节名格式为 {device_id}_{joint_name}。
    设备 ID 不同（如 ot2_left, ot2_right），但底层关节名相同（如 pipette_j1）。
    """

    def test_same_type_different_id(self):
        """同类型设备不同 ID"""
        sim = _make_sim({
            "ot2_left": "uuid-ot2-left",
            "ot2_right": "uuid-ot2-right",
        })
        msg = _make_joint_state_msg(
            ["ot2_left_pipette_j1", "ot2_left_pipette_j2",
             "ot2_right_pipette_j1", "ot2_right_pipette_j2"],
            [0.1, 0.2, 0.3, 0.4],
        )
        result = sim.joint_state_callback(msg)
        assert result["ot2_left"]["joint_states"]["ot2_left_pipette_j1"] == 0.1
        assert result["ot2_left"]["joint_states"]["ot2_left_pipette_j2"] == 0.2
        assert result["ot2_right"]["joint_states"]["ot2_right_pipette_j1"] == 0.3
        assert result["ot2_right"]["joint_states"]["ot2_right_pipette_j2"] == 0.4

    def test_same_type_with_resources_routed_correctly(self):
        """同类型设备各自抓取资源，按 link 前缀正确路由"""
        sim = _make_sim({
            "ot2_left": "uuid-ot2-left",
            "ot2_right": "uuid-ot2-right",
        })
        sim.resource_pose_callback(_make_string_msg(json.dumps({
            "plate_A": "ot2_left_gripper",
            "plate_B": "ot2_right_gripper",
        })))
        msg = _make_joint_state_msg(
            ["ot2_left_j1", "ot2_right_j1"],
            [0.5, 0.6],
        )
        result = sim.joint_state_callback(msg)
        assert result["ot2_left"]["resource_poses"] == {"plate_A": "ot2_left_gripper"}
        assert result["ot2_right"]["resource_poses"] == {"plate_B": "ot2_right_gripper"}

    def test_numbered_devices_no_confusion(self):
        """编号设备: robot1 不应匹配 robot10 的关节"""
        sim = _make_sim({
            "robot1": "uuid-r1",
            "robot10": "uuid-r10",
        })
        msg = _make_joint_state_msg(
            ["robot1_j1", "robot10_j1"],
            [1.0, 10.0],
        )
        result = sim.joint_state_callback(msg)
        assert result["robot1"]["joint_states"]["robot1_j1"] == 1.0
        assert result["robot10"]["joint_states"]["robot10_j1"] == 10.0

    def test_three_same_type_devices(self):
        """三个同类型设备"""
        sim = _make_sim({
            "pump_a": "uuid-pa",
            "pump_b": "uuid-pb",
            "pump_c": "uuid-pc",
        })
        msg = _make_joint_state_msg(
            ["pump_a_flow", "pump_b_flow", "pump_c_flow",
             "pump_a_pressure", "pump_b_pressure"],
            [1.0, 2.0, 3.0, 0.1, 0.2],
        )
        result = sim.joint_state_callback(msg)
        assert len(result["pump_a"]["joint_states"]) == 2
        assert len(result["pump_b"]["joint_states"]) == 2
        assert len(result["pump_c"]["joint_states"]) == 1


# ==================== E7: 吞吐优化测试 ====================


class TestThroughputOptimizations:
    """测试死区过滤、抑频、增量 resource_poses 等优化行为"""

    # --- 死区过滤 (Dead Band) ---

    def test_dead_band_filters_tiny_change(self):
        """关节变化小于死区阈值 → 被过滤"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.0)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        result1 = sim.joint_state_callback(msg1, now=0.0)
        assert "arm" in result1

        # 微小变化 (0.001 < 0.01 死区)
        msg2 = _make_joint_state_msg(["arm_j1"], [1.001])
        result2 = sim.joint_state_callback(msg2, now=1.0)
        assert result2 == {}

    def test_dead_band_passes_significant_change(self):
        """关节变化大于死区阈值 → 通过"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.0)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        sim.joint_state_callback(msg1, now=0.0)

        msg2 = _make_joint_state_msg(["arm_j1"], [1.05])
        result2 = sim.joint_state_callback(msg2, now=1.0)
        assert "arm" in result2
        assert result2["arm"]["joint_states"]["arm_j1"] == pytest.approx(1.05)

    def test_dead_band_first_message_always_passes(self):
        """首次消息总是通过（无历史值）"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=1000.0, min_interval=0.0)
        msg = _make_joint_state_msg(["arm_j1"], [0.001])
        result = sim.joint_state_callback(msg, now=0.0)
        assert "arm" in result

    def test_dead_band_any_joint_change_triggers(self):
        """多关节中只要有一个超过死区就全部发送"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.0)
        msg1 = _make_joint_state_msg(["arm_j1", "arm_j2"], [1.0, 2.0])
        sim.joint_state_callback(msg1, now=0.0)

        # j1 微变化，j2 大变化
        msg2 = _make_joint_state_msg(["arm_j1", "arm_j2"], [1.001, 2.5])
        result2 = sim.joint_state_callback(msg2, now=1.0)
        assert "arm" in result2
        # 两个关节的值都应包含在结果中
        assert result2["arm"]["joint_states"]["arm_j1"] == pytest.approx(1.001)
        assert result2["arm"]["joint_states"]["arm_j2"] == pytest.approx(2.5)

    # --- 抑频 (Throttle) ---

    def test_throttle_filters_rapid_messages(self):
        """发送间隔内的消息被过滤"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.0, min_interval=0.1)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        result1 = sim.joint_state_callback(msg1, now=0.0)
        assert "arm" in result1

        # 0.05s < 0.1s 间隔
        msg2 = _make_joint_state_msg(["arm_j1"], [2.0])
        result2 = sim.joint_state_callback(msg2, now=0.05)
        assert result2 == {}

    def test_throttle_passes_after_interval(self):
        """超过发送间隔后消息通过"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.0, min_interval=0.1)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        sim.joint_state_callback(msg1, now=0.0)

        msg2 = _make_joint_state_msg(["arm_j1"], [2.0])
        result2 = sim.joint_state_callback(msg2, now=0.15)
        assert "arm" in result2

    def test_throttle_bypassed_by_resource_change(self):
        """resource_pose 变化时忽略抑频限制"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.0, min_interval=1.0)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        sim.joint_state_callback(msg1, now=0.0)

        # 资源变化 → 强制发送
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate": "arm_gripper"})))
        msg2 = _make_joint_state_msg(["arm_j1"], [1.0])
        result2 = sim.joint_state_callback(msg2, now=0.01)  # 远小于 1.0 间隔
        assert "arm" in result2
        assert result2["arm"]["resource_poses"] == {"plate": "arm_gripper"}

    # --- 增量 resource_poses ---

    def test_resource_poses_only_sent_when_dirty(self):
        """resource_poses 仅在 dirty 时附带，否则为空"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate": "panda_gripper"})))

        # 第一次发送：dirty → 携带 resource_poses
        msg1 = _make_joint_state_msg(["panda_j1"], [0.5])
        result1 = sim.joint_state_callback(msg1)
        assert result1["panda"]["resource_poses"] == {"plate": "panda_gripper"}

        # dirty 已清除
        assert sim._resource_poses_dirty is False

        # 第二次发送：not dirty → resource_poses 为空
        msg2 = _make_joint_state_msg(["panda_j1"], [1.0])
        result2 = sim.joint_state_callback(msg2)
        assert result2["panda"]["resource_poses"] == {}

    def test_resource_change_resets_dirty_after_send(self):
        """dirty 在发送后被重置，再次 resource_pose 变化后重新标记"""
        sim = _make_sim({"panda": "uuid-panda"})
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate": "panda_deck"})))

        msg = _make_joint_state_msg(["panda_j1"], [0.5])
        sim.joint_state_callback(msg)
        assert sim._resource_poses_dirty is False

        # 再次资源变化
        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate": "panda_gripper"})))
        assert sim._resource_poses_dirty is True

        msg2 = _make_joint_state_msg(["panda_j1"], [1.0])
        result2 = sim.joint_state_callback(msg2)
        assert result2["panda"]["resource_poses"] == {"plate": "panda_gripper"}

    # --- 组合场景 ---

    def test_dead_band_bypassed_by_resource_dirty(self):
        """关节无变化但 resource_pose 有变化 → 仍然发送"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.0)
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        sim.joint_state_callback(msg1, now=0.0)

        sim.resource_pose_callback(_make_string_msg(json.dumps({"plate": "arm_gripper"})))
        # 关节值完全不变
        msg2 = _make_joint_state_msg(["arm_j1"], [1.0])
        result2 = sim.joint_state_callback(msg2, now=1.0)
        assert "arm" in result2
        assert result2["arm"]["resource_poses"] == {"plate": "arm_gripper"}

    def test_high_frequency_stream_only_significant_pass(self):
        """模拟高频流: 只有显著变化的消息通过"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.0)
        t = 0.0
        passed_count = 0

        # 100 条消息，每条微小递增 0.001
        for i in range(100):
            t += 0.1
            val = 1.0 + i * 0.001
            msg = _make_joint_state_msg(["arm_j1"], [val])
            result = sim.joint_state_callback(msg, now=t)
            if result:
                passed_count += 1

        # 首次总通过 + 每 10 条左右（累计 0.01 变化）通过一次
        assert passed_count < 20  # 远少于 100
        assert passed_count >= 5  # 但不应为 0

    def test_throttle_and_dead_band_combined(self):
        """同时受抑频和死区影响"""
        sim = JointBridgeSimulator({"arm": "uuid-arm"}, dead_band=0.01, min_interval=0.5)

        # 首条通过
        msg1 = _make_joint_state_msg(["arm_j1"], [1.0])
        assert sim.joint_state_callback(msg1, now=0.0) != {}

        # 时间不够 + 变化不够 → 过滤
        msg2 = _make_joint_state_msg(["arm_j1"], [1.001])
        assert sim.joint_state_callback(msg2, now=0.1) == {}

        # 时间够但变化不够 → 过滤
        msg3 = _make_joint_state_msg(["arm_j1"], [1.002])
        assert sim.joint_state_callback(msg3, now=1.0) == {}

        # 时间够且变化够 → 通过
        msg4 = _make_joint_state_msg(["arm_j1"], [1.05])
        assert sim.joint_state_callback(msg4, now=1.5) != {}
