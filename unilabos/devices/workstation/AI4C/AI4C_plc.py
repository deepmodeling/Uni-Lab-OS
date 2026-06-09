"""
AI4C PLC 设备驱动。

只负责 OPC UA/PLC 通讯、初始化、心跳和通用状态变量访问。
具体机械臂、固态称量、移液、磁搅、HPLC action 不放在这个设备里。
"""
import os
import threading
import time
from typing import Any, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.utils.log import logger
from unilabos.devices.workstation.AI4C.base_opcua_client import OpcUaClientWithSubscription


AI4C_PLC_STATUS_NODES = {
    "robotic_arm_idle": "Robotic_Arm_Idle",
    "robotic_arm_action_complete": "Robotic_Arm_Action_Complete",
    "solid_weighing_occupied": "Solid_Weighing_Occupied",
    "powder_in_solid_weighing_occupied": "Powder_In_Solid_Weighing_Occupied",
    "pipetting_station_occupied": "Pipetting_Station_Occupied",
    "magnetic_stirrer_occupied": "Magnetic_Stirrer_Occupied",
    "hplc_workstation_occupied": "HPLC_Pool_Occupied",
    "robotic_arm_current_step": "Robotic_Arm_Current_Step",
    "solid_weighing_current_step": "Solid_Weighing_Current_Step",
    "magnetic_stirrer_current_step": "Magnetic_Stirrer_Current_Step",
}


@device(
    id="AI4C_plc",
    display_name="AI4C PLC",
    category=["custom"],
    description="AI4C PLC/OPC UA 通讯设备，只暴露 PLC 初始化、心跳和通用状态变量",
    icon="AI4C.webp",
)
class AI4CPLCDevice(OpcUaClientWithSubscription):
    """
    AI4C PLC 设备。

    该设备只负责 OPC UA/PLC 通讯、初始化、心跳和状态变量访问。
    具体工艺动作应放在机械臂、固态称量、移液站、磁搅、HPLC 等设备中。
    """

    def __init__(
        self,
        url: str,
        csv_path: str = None,
        username: str = None,
        password: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0,
        subscription_interval: int = 500,
        *args,
        **kwargs,
    ):
        super().__init__(
            url=url,
            username=username,
            password=password,
            use_subscription=use_subscription,
            cache_timeout=cache_timeout,
            subscription_interval=subscription_interval,
            *args,
            **kwargs,
        )

        if csv_path:
            if not os.path.isabs(csv_path):
                current_dir = os.path.dirname(os.path.abspath(__file__))
                csv_path = os.path.join(current_dir, csv_path)
            self.load_nodes_from_csv(csv_path)

        self.m_initialized = False
        self.heartbeat_on = False
        self.m_robot_arm_current_step = None
        self.m_solid_weighing_current_step = None
        self.m_magnetic_stirrer_current_step = None

    @action(auto_prefix=True, description="启动 AI4C PLC 心跳")
    def start_heart_beat(self) -> dict:
        """启动 PLC 心跳。"""
        if self.heartbeat_on:
            return {
                "success": True,
                "message": "心跳已在运行",
            }

        logger.info("启动心跳")
        self.heartbeat_on = True
        timer = threading.Timer(1.0, self.trigger_heart_beat)
        timer.daemon = True
        timer.start()
        return {
            "success": True,
            "message": "心跳已启动",
        }

    @action(auto_prefix=True, description="停止 AI4C PLC 心跳")
    def stop_heart_beat(self) -> dict:
        """停止 PLC 心跳。"""
        logger.info("停止心跳")
        self.heartbeat_on = False
        self.set_node_value("Heart_Beat", False)
        return {
            "success": True,
            "message": "心跳已停止",
        }

    @not_action
    def trigger_heart_beat(self) -> None:
        """写入一次 PLC 心跳，并按需继续调度。"""
        if not self.heartbeat_on:
            return

        value = self.get_node_value("Heart_Beat")
        self.set_node_value("Heart_Beat", not value)

        if self.m_initialized:
            self._refresh_current_steps()

        timer = threading.Timer(1.0, self.trigger_heart_beat)
        timer.daemon = True
        timer.start()

    @not_action
    def read_variable(self, node_name: str, use_cache: bool = True) -> Any:
        """对外提供 PLC 变量读取函数，供其他设备实例调用。"""
        return self.get_node_value(node_name, use_cache=use_cache)

    @not_action
    def write_variable(self, node_name: str, value: Any) -> bool:
        """对外提供 PLC 变量写入函数，供其他设备实例调用。"""
        return bool(self.set_node_value(node_name, value))

    @not_action
    def get_variables(self, node_names: Optional[list[str]] = None, use_cache: bool = True) -> dict:
        """批量读取 PLC 变量；默认返回 CSV 中注册的全部 PLC 变量。"""
        names = node_names or list(self._variables_to_find)
        result = {}
        for name in names:
            node_name = AI4C_PLC_STATUS_NODES.get(name, name)
            result_key = name if node_names else self._reverse_mapping.get(name, name)
            try:
                result[result_key] = self.get_node_value(node_name, use_cache=use_cache)
            except Exception as exc:
                result[result_key] = {
                    "success": False,
                    "error": str(exc),
                }
        return result

    @action(auto_prefix=True, description="获取指定 PLC 变量的状态")
    def check_variable_status(self, variable_name: str) -> dict:
        """
        获取指定 PLC 变量的状态。

        Args:
            variable_name[变量名称]: CSV 文件中的变量名称（支持中文名或英文名）。
        """
        # 1. 尝试将英文名映射为注册的中文名
        real_name = self._name_mapping.get(variable_name, variable_name)

        # 2. 调用 get_variables 获取状态
        res = self.get_variables([real_name], use_cache=False)

        # 3. 确保返回的 key 尽量与前端传进来的 variable_name 一致
        final_res = {}
        for k, v in res.items():
            if k == real_name:
                final_res[variable_name] = v
            else:
                final_res[k] = v
        return final_res

    @not_action
    def _refresh_current_steps(self) -> None:
        """刷新并记录关键流程步变化。"""
        robot_arm_current_step = self.get_node_value("Robotic_Arm_Current_Step")
        if self.m_robot_arm_current_step != robot_arm_current_step:
            self.m_robot_arm_current_step = robot_arm_current_step
            logger.info(f"机械臂当前步骤更新: {self.m_robot_arm_current_step}")

        solid_weighing_current_step = self.get_node_value("Solid_Weighing_Current_Step")
        if self.m_solid_weighing_current_step != solid_weighing_current_step:
            self.m_solid_weighing_current_step = solid_weighing_current_step
            logger.info(f"固体称量当前步骤更新: {self.m_solid_weighing_current_step}")

        magnetic_stirrer_current_step = self.get_node_value("Magnetic_Stirrer_Current_Step")
        if self.m_magnetic_stirrer_current_step != magnetic_stirrer_current_step:
            self.m_magnetic_stirrer_current_step = magnetic_stirrer_current_step
            logger.info(f"磁搅当前步骤更新: {self.m_magnetic_stirrer_current_step}")

    @topic_config(period=1.0)
    def robotic_arm_idle(self) -> bool:
        return bool(self.read_variable("Robotic_Arm_Idle", use_cache=False))

    @topic_config(period=0.2)
    def robotic_arm_action_complete(self) -> bool:
        return bool(self.read_variable("Robotic_Arm_Action_Complete", use_cache=True))

    @topic_config(period=0.5)
    def loading_rack_occupied(self) -> dict:
        return {
            str(position): bool(
                self.read_variable(f"Well_Plate_Loading_Rack_InPut[{position - 1}]", use_cache=True)
            )
            for position in range(1, 9)
        }

    @topic_config(period=1.0)
    def solid_weighing_occupied(self) -> bool:
        return bool(self.read_variable("Solid_Weighing_Occupied", use_cache=False))

    @topic_config(period=1.0)
    def powder_in_solid_weighing_occupied(self) -> bool:
        return bool(self.read_variable("Powder_In_Solid_Weighing_Occupied", use_cache=False))

    @topic_config(period=1.0)
    def pipetting_station_occupied(self) -> bool:
        return bool(self.read_variable("Pipetting_Station_Occupied", use_cache=False))

    @topic_config(period=1.0)
    def magnetic_stirrer_occupied(self) -> bool:
        return bool(self.read_variable("Magnetic_Stirrer_Occupied", use_cache=False))

    @topic_config(period=1.0)
    def hplc_workstation_occupied(self) -> bool:
        return bool(self.read_variable("HPLC_Pool_Occupied", use_cache=False))

    @topic_config(period=1.0)
    def robotic_arm_current_step(self) -> int:
        return int(self.read_variable("Robotic_Arm_Current_Step", use_cache=False) or 0)

    @topic_config(period=1.0)
    def solid_weighing_current_step(self) -> int:
        return int(self.read_variable("Solid_Weighing_Current_Step", use_cache=False) or 0)

    @topic_config(period=1.0)
    def magnetic_stirrer_current_step(self) -> int:
        return int(self.read_variable("Magnetic_Stirrer_Current_Step", use_cache=False) or 0)

    def _wait_until_true(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 0.2,
        description: str = None,
    ) -> bool:
        """等待布尔节点变为 True。"""
        desc = description or node_name
        logger.info(f"等待 {desc} 变为 True...")
        start = time.time()
        while True:
            if self.get_node_value(node_name, use_cache=True):
                logger.info(f"✓ {desc} 已变为 True")
                return True
            if time.time() - start >= timeout:
                logger.error(f"✗ 等待 {desc} 超时（{timeout}秒）")
                return False
            time.sleep(interval)

    def _wait_until_false(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 0.2,
        description: str = None,
    ) -> bool:
        """等待布尔节点变为 False。"""
        desc = description or node_name
        logger.info(f"等待 {desc} 变为 False...")
        start = time.time()
        while True:
            if not self.get_node_value(node_name, use_cache=True):
                logger.info(f"✓ {desc} 已变为 False")
                return True
            if time.time() - start >= timeout:
                logger.error(f"✗ 等待 {desc} 超时（{timeout}秒）")
                return False
            time.sleep(interval)
