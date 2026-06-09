"""
AI4C robot arm device.

This device owns robot-arm semantics while delegating all PLC I/O to the
AI4C_plc device. PLC status is mirrored through ROS topics, and strong
reads/writes use AI4C_plc's built-in driver command action.
"""

import json
import threading
import time
from enum import Enum
from typing import Any, Optional

from rclpy.action import ActionClient
from std_msgs.msg import Bool, String
from unilabos_msgs.action import StrSingleInput

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action
from unilabos.resources.resource_tracker import JSON_UNILABOS_PARAM, PARAM_SAMPLE_UUIDS
from unilabos.utils.decorator import subscribe
from unilabos.utils.log import logger


class RoboticArmTargetPosition(int, Enum):
    SOLID_WEIGHING_STACK = 1
    SOLID_WEIGHING = 2
    PIPETTING_STATION = 3
    MAGNETIC_STIRRER = 4
    HPLC_STATION = 5
    PLATE_LOADING_RACK = 6
    PLATE_UNLOADING_RACK = 7


class RoboticArmAction(int, Enum):
    PICK = 1
    PLACE = 2
    ON_POWDER_HEAD = 3
    OFF_POWDER_HEAD = 4


MIN_RACK_POSITION = 1
MAX_RACK_POSITION = 8


@device(
    id="AI4C_robot_arm",
    display_name="AI4C 机械臂",
    category=["robotic_arm"],
    description="AI4C 机械臂搬运设备，通过 AI4C_plc 转发 PLC 读写",
    icon="AI4C.webp",
)
class AI4CRobotArmDevice:
    def __init__(
        self,
        plc_device_id: str = "AI4C_plc",
        plc_action_timeout: float = 10.0,
        state_max_age: float = 2.0,
        *args,
        **kwargs,
    ):
        self.plc_device_id = plc_device_id
        self.plc_action_timeout = plc_action_timeout
        self.state_max_age = state_max_age
        self._ros_node = None
        self._plc_command_client: Optional[ActionClient] = None
        self._plc_state: dict[str, Any] = {}
        self._plc_state_ts: dict[str, float] = {}
        self._state_lock = threading.RLock()

    @not_action
    def post_init(self, ros_node) -> None:
        """Create the cross-device action client after the ROS node is ready."""
        self._ros_node = ros_node
        self._plc_command_client = ActionClient(
            ros_node,
            StrSingleInput,
            f"/devices/{self.plc_device_id}/_execute_driver_command",
            callback_group=ros_node.callback_group,
        )

    @not_action
    def _set_plc_state(self, key: str, value: Any) -> None:
        with self._state_lock:
            self._plc_state[key] = value
            self._plc_state_ts[key] = time.time()

    @not_action
    def _get_plc_state(self, key: str, max_age: Optional[float] = None) -> Any:
        max_age = self.state_max_age if max_age is None else max_age
        with self._state_lock:
            if key not in self._plc_state:
                raise RuntimeError(f"PLC 状态尚未收到: {key}")
            age = time.time() - self._plc_state_ts[key]
            if age > max_age:
                raise RuntimeError(f"PLC 状态已过期: {key}, age={age:.2f}s")
            return self._plc_state[key]

    @not_action
    @subscribe("/devices/AI4C_plc/robotic_arm_idle", msg_type=Bool)
    def on_robotic_arm_idle(self, msg: Bool) -> None:
        self._set_plc_state("robotic_arm_idle", bool(msg.data))

    @not_action
    @subscribe("/devices/AI4C_plc/robotic_arm_action_complete", msg_type=Bool)
    def on_robotic_arm_action_complete(self, msg: Bool) -> None:
        self._set_plc_state("robotic_arm_action_complete", bool(msg.data))

    @not_action
    @subscribe("/devices/AI4C_plc/loading_rack_occupied", msg_type=String)
    def on_loading_rack_occupied(self, msg: String) -> None:
        self._set_plc_state("loading_rack_occupied", json.loads(msg.data))

    @not_action
    @subscribe("/devices/AI4C_plc/pipetting_station_occupied", msg_type=Bool)
    def on_pipetting_station_occupied(self, msg: Bool) -> None:
        self._set_plc_state("pipetting_station_occupied", bool(msg.data))

    @not_action
    @subscribe("/devices/AI4C_plc/magnetic_stirrer_occupied", msg_type=Bool)
    def on_magnetic_stirrer_occupied(self, msg: Bool) -> None:
        self._set_plc_state("magnetic_stirrer_occupied", bool(msg.data))

    @not_action
    @subscribe("/devices/AI4C_plc/hplc_workstation_occupied", msg_type=Bool)
    def on_hplc_workstation_occupied(self, msg: Bool) -> None:
        self._set_plc_state("hplc_workstation_occupied", bool(msg.data))

    @not_action
    def _wait_future(self, future, timeout: float, description: str):
        done = threading.Event()
        future.add_done_callback(lambda _future: done.set())
        if not done.wait(timeout):
            raise TimeoutError(f"{description} 超时 ({timeout}s)")
        return future.result()

    @not_action
    def _call_plc_command(self, function_name: str, function_args: dict[str, Any]) -> Any:
        if self._plc_command_client is None:
            raise RuntimeError("AI4C_plc action client 尚未初始化")

        if not self._plc_command_client.wait_for_server(timeout_sec=self.plc_action_timeout):
            raise TimeoutError(f"等待 AI4C_plc 命令服务超时: {self.plc_device_id}")

        command = {
            "function_name": function_name,
            "function_args": function_args,
            JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
        }
        goal = StrSingleInput.Goal()
        goal.string = json.dumps(command, ensure_ascii=False)

        goal_handle = self._wait_future(
            self._plc_command_client.send_goal_async(goal),
            self.plc_action_timeout,
            f"发送 PLC 命令 {function_name}",
        )
        if not goal_handle.accepted:
            raise RuntimeError(f"AI4C_plc 拒绝执行命令: {function_name}")

        result_wrapper = self._wait_future(
            goal_handle.get_result_async(),
            self.plc_action_timeout,
            f"等待 PLC 命令 {function_name} 返回",
        )
        result = result_wrapper.result
        result_info = json.loads(result.return_info or "{}")
        if not result.success or not result_info.get("suc", False):
            raise RuntimeError(result_info.get("error") or f"AI4C_plc 命令失败: {function_name}")
        return result_info.get("return_value")

    @not_action
    def _read_plc_variable(self, node_name: str, use_cache: bool = True) -> Any:
        return self._call_plc_command(
            "read_variable",
            {
                "node_name": node_name,
                "use_cache": use_cache,
            },
        )

    @not_action
    def _write_plc_variable(self, node_name: str, value: Any) -> None:
        self._call_plc_command(
            "write_variable",
            {
                "node_name": node_name,
                "value": value,
            },
        )

    @not_action
    def _read_bool_with_topic_fallback(self, state_key: str, node_name: str) -> bool:
        try:
            return bool(self._get_plc_state(state_key))
        except RuntimeError:
            logger.warning(f"订阅状态不可用，改为强制读取 PLC 节点: {node_name}")
            return bool(self._read_plc_variable(node_name, use_cache=False))

    @not_action
    def _wait_plc_bool(
        self,
        node_name: str,
        expected: bool,
        timeout: float = 300.0,
        interval: float = 0.2,
        description: str = None,
    ) -> bool:
        desc = description or node_name
        logger.info(f"等待 {desc} 变为 {expected}...")
        start = time.time()
        while True:
            if bool(self._read_plc_variable(node_name, use_cache=False)) is expected:
                logger.info(f"✓ {desc} 已变为 {expected}")
                return True
            if time.time() - start >= timeout:
                logger.error(f"✗ 等待 {desc} 超时 ({timeout}s)")
                return False
            time.sleep(interval)

    @not_action
    def is_robotic_arm_idle(self) -> bool:
        return self._read_bool_with_topic_fallback("robotic_arm_idle", "Robotic_Arm_Idle")

    @not_action
    def is_loading_rack_position_occupied(self, position: int) -> bool:
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error(f"上料架位置错误，必须在范围[{MIN_RACK_POSITION}, {MAX_RACK_POSITION}]内")
            return False

        try:
            rack_state = self._get_plc_state("loading_rack_occupied")
            return bool(rack_state.get(str(position), False))
        except RuntimeError:
            node_name = f"Well_Plate_Loading_Rack_InPut[{position - 1}]"
            logger.warning(f"上料架订阅状态不可用，改为强制读取 PLC 节点: {node_name}")
            return bool(self._read_plc_variable(node_name, use_cache=False))

    @not_action
    def is_unloading_rack_position_occupied(self, position: int) -> bool:
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error(f"下料架位置错误，必须在范围[{MIN_RACK_POSITION}, {MAX_RACK_POSITION}]内")
            return False

        node_name = f"Well_Plate_Unloading_Rack_InPut[{position - 1}]"
        return bool(self._read_plc_variable(node_name, use_cache=False))

    @not_action
    def is_pipetting_station_occupied(self) -> bool:
        return self._read_bool_with_topic_fallback("pipetting_station_occupied", "Pipetting_Station_Occupied")

    @not_action
    def is_magnetic_stirrer_occupied(self) -> bool:
        return self._read_bool_with_topic_fallback("magnetic_stirrer_occupied", "Magnetic_Stirrer_Occupied")

    @not_action
    def is_hplc_workstation_occupied(self) -> bool:
        return self._read_bool_with_topic_fallback("hplc_workstation_occupied", "HPLC_Pool_Occupied")

    @not_action
    def _run_robot_arm_action(
        self,
        target_position: RoboticArmTargetPosition,
        pick_place_code: int,
        arm_action: RoboticArmAction,
        description: str,
        success_message: str,
        reset_description: str = None,
    ) -> dict:
        failure_message = (
            f"{success_message[:-2]}失败" if success_message.endswith("完成") else f"{success_message}失败"
        )
        self._write_plc_variable("Robotic_Arm_Target_Position_Code", target_position.value)
        self._write_plc_variable("Robotic_Arm_Target_Pick_Place_Code", pick_place_code)
        self._write_plc_variable("Robotic_Arm_Action_Code", arm_action.value)
        self._write_plc_variable("Robotic_Arm_Action_Trigger", True)

        if self._wait_plc_bool(
            "Robotic_Arm_Action_Complete",
            True,
            description=description,
        ):
            self._write_plc_variable("Robotic_Arm_Action_Trigger", False)
            if self._wait_plc_bool(
                "Robotic_Arm_Action_Complete",
                False,
                description=reset_description or description,
            ):
                logger.info(success_message)
                return {
                    "success": True,
                    "message": success_message,
                }

            logger.error(failure_message)
            return {
                "success": False,
                "message": f"{failure_message}，完成复位超时",
            }

        logger.error(failure_message)
        return {
            "success": False,
            "message": f"{failure_message}，机械臂动作未完成",
        }

    @action(
        auto_prefix=True,
        description="步骤2：从上料架抓取孔板",
        handles=[
            ActionInputHandle(
                key="loading_rack_position",
                data_type="ai4c_loading_rack_position",
                label="上料架位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="孔板所在上料架位置，范围 1-8",
            )
        ],
    )
    def pick_well_plate_from_loading_rack(self, position: int = 1) -> dict:
        logger.info("从上料架取孔板...")
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error(f"上料架位置错误，必须在范围[{MIN_RACK_POSITION}, {MAX_RACK_POSITION}]内")
            return {
                "success": False,
                "message": "上料架位置错误",
            }

        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_loading_rack_position_occupied(position):
            logger.error(f"上料架位置{position}没有孔板")
            return {
                "success": False,
                "message": f"上料架位置{position}没有孔板",
            }

        logger.info(f"从上料架位置{position}抓取孔板...")
        return self._run_robot_arm_action(
            RoboticArmTargetPosition.PLATE_LOADING_RACK,
            position,
            RoboticArmAction.PICK,
            description="从上料架抓取孔板完成",
            success_message="从上料架抓取孔板完成",
        )

    @action(auto_prefix=True, description="步骤14/20：将孔板放置到移液站")
    def place_well_plate_to_pipetting_station(self) -> dict:
        logger.info("将孔板放置到移液站...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if self.is_pipetting_station_occupied():
            logger.error("移液站位置已有孔板")
            return {
                "success": False,
                "message": "移液站位置已有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.PIPETTING_STATION,
            1,
            RoboticArmAction.PLACE,
            description="将孔板放置到移液站完成",
            success_message="将孔板放置到移液站完成",
        )

    @action(auto_prefix=True, description="步骤16/21：从移液站取回孔板")
    def pick_well_plate_from_pipetting_station(self) -> dict:
        logger.info("从移液站取孔板...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_pipetting_station_occupied():
            logger.error("移液站位置没有孔板")
            return {
                "success": False,
                "message": "移液站位置没有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.PIPETTING_STATION,
            1,
            RoboticArmAction.PICK,
            description="从移液站取孔板完成",
            success_message="从移液站取孔板完成",
        )

    @action(auto_prefix=True, description="步骤17：将孔板放置到磁搅")
    def place_well_plate_to_magnetic_stirrer(self) -> dict:
        logger.info("将孔板放置到磁搅...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if self.is_magnetic_stirrer_occupied():
            logger.error("磁搅位置已有孔板")
            return {
                "success": False,
                "message": "磁搅位置已有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.MAGNETIC_STIRRER,
            1,
            RoboticArmAction.PLACE,
            description="将孔板放置到磁搅完成",
            success_message="将孔板放置到磁搅完成",
        )

    @action(auto_prefix=True, description="步骤19：从磁搅取回孔板")
    def pick_well_plate_from_magnetic_stirrer(self) -> dict:
        logger.info("从磁搅取孔板...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_magnetic_stirrer_occupied():
            logger.error("磁搅位置没有孔板")
            return {
                "success": False,
                "message": "磁搅位置没有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.MAGNETIC_STIRRER,
            1,
            RoboticArmAction.PICK,
            description="从磁搅取孔板完成",
            success_message="从磁搅取孔板完成",
        )

    @action(auto_prefix=True, description="步骤22：将孔板放置到 HPLC 站")
    def place_well_plate_to_hplc_station(self) -> dict:
        logger.info("将孔板放置到 HPLC 站...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if self.is_hplc_workstation_occupied():
            logger.error("HPLC 站位置已有孔板")
            return {
                "success": False,
                "message": "HPLC 站位置已有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.HPLC_STATION,
            1,
            RoboticArmAction.PLACE,
            description="将孔板放置到 HPLC 站完成",
            success_message="将孔板放置到 HPLC 站完成",
        )

    @action(auto_prefix=True, description="步骤24：从 HPLC 站取回孔板")
    def pick_well_plate_from_hplc_station(self) -> dict:
        logger.info("从 HPLC 站取孔板...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_hplc_workstation_occupied():
            logger.error("HPLC 站位置没有孔板")
            return {
                "success": False,
                "message": "HPLC 站位置没有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.HPLC_STATION,
            1,
            RoboticArmAction.PICK,
            description="从 HPLC 站取孔板完成",
            success_message="从 HPLC 站取孔板完成",
        )

    @action(
        auto_prefix=True,
        description="步骤25：将孔板放置到下料架",
        handles=[
            ActionInputHandle(
                key="unloading_rack_position",
                data_type="ai4c_unloading_rack_position",
                label="下料架位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="孔板放置的下料架位置，范围 1-8",
            )
        ],
    )
    def place_well_plate_to_unloading_rack(self, position: int = 1) -> dict:
        logger.info("将孔板放置到下料架...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error("下料架位置超出范围")
            return {
                "success": False,
                "message": "下料架位置超出范围",
            }

        if self.is_unloading_rack_position_occupied(position):
            logger.error("下料架位置已有孔板")
            return {
                "success": False,
                "message": "下料架位置已有孔板",
            }

        return self._run_robot_arm_action(
            RoboticArmTargetPosition.PLATE_UNLOADING_RACK,
            position,
            RoboticArmAction.PLACE,
            description="将孔板放置到下料架完成",
            success_message="将孔板放置到下料架完成",
        )
