"""
AI4C 设备驱动
继承自 OPC UA 通讯基类，实现具体的设备动作函数
"""

import json
import time
import traceback
from typing import Optional
import os
import threading

# 导入日志类
from unilabos.utils.log import logger
import logging
from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action

# 导入通讯基类
from unilabos.devices.workstation.AI4C.base_opcua_client import OpcUaClientWithSubscription

# 定义机械臂目标位置的枚举
from enum import Enum
class RoboticArmTargetPosition(int, Enum):
    """
    机械臂目标位置的枚举
    """
    # 固态称量堆栈
    SOLID_WEIGHING_STACK = 1
    # 固体称量
    SOLID_WEIGHING = 2
    # 移液站
    PIPETTING_STATION = 3
    # 磁搅
    MAGNETIC_STIRRER = 4
    # HPLC工站
    HPLC_STATION = 5
    # 孔板上料架
    PLATE_LOADING_RACK = 6
    # 孔板下料架
    PLATE_UNLOADING_RACK = 7

class RoboticArmAction(int, Enum):
    """
    机械臂动作的枚举
    """
    # 抓取
    PICK = 1
    # 存放
    PLACE = 2
    # 上粉末头
    ON_POWDER_HEAD = 3
    # 下粉末头
    OFF_POWDER_HEAD = 4

# 最大和最小的料架位置
MIN_RACK_POSITION = 1
MAX_RACK_POSITION = 8

# 最大和最小的称量堆栈位置
MIN_SOLID_WEIGHING_STACK_POSITION = 1
MAX_SOLID_WEIGHING_STACK_POSITION = 25


# 定义 AI4C 设备通信类
# 包含一个固态称量、一个移液站、一个磁搅、一个 HPLC 工站
@device(
    id="AI4C_station",
    display_name="AI4C 工作站",
    category=["workstation"],
    description="AI4C 水合工作站，仅开放初始化、上料取板、固态称量开门、孔板放入固态称量四个步骤",
    icon="AI4C.webp",
)
class AI4CDevice(OpcUaClientWithSubscription):
    """
    AI4M 设备类
    继承自 OpcUaClientWithSubscription，实现具体的设备动作函数
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
        """
        初始化 AI4C 设备
        
        参数:
            url: OPC UA 服务器地址
            csv_path: 节点配置 CSV 文件路径
            username: OPC UA 用户名
            password: OPC UA 密码
            use_subscription: 是否启用订阅模式
            cache_timeout: 缓存超时时间（秒）
            subscription_interval: 订阅发布间隔（毫秒）
        """
        # 调用父类构造函数
        super().__init__(
            url=url,
            username=username,
            password=password,
            use_subscription=use_subscription,
            cache_timeout=cache_timeout,
            subscription_interval=subscription_interval,
            *args,
            **kwargs
        )

        # 如果提供了 CSV 路径，则直接加载节点
        if csv_path:
            self.load_nodes_from_csv(csv_path)

        self.m_initialized = False

    # 初始化工站
    @action(auto_prefix=True, description="步骤1：初始化 AI4C 工站")
    def init_workstation(self) -> dict:
        """
        初始化工作站函数：
        - 水合工站初始化PC
        - 等待水合工站初始化完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("停止机械臂触发...")
        self.set_node_value("Robotic_Arm_Action_Trigger", False)

        logger.info("水合工站初始化...")
        self.set_node_value("Hydration_Workstation_PC_Initialization", False)
        time.sleep(1.0)
        self.set_node_value("Hydration_Workstation_PC_Initialization", True)
        time.sleep(1.0)
        if not self._wait_until_false('Hydration_Workstation_Initialization_Complete', description="水合工站初始化完成"):
            logger.error("水合工站初始化失败")
            return {
                "success": False,
                "message": "水合工站初始化失败",
            }
        if self._wait_until_true('Hydration_Workstation_Initialization_Complete', description="水合工站初始化完成"):
            logger.info("水合工站初始化完成")

            self.m_robot_arm_current_step = self.get_node_value("Robotic_Arm_Current_Step")
            logger.info(f"机械臂当前步骤: {self.m_robot_arm_current_step}")
            self.m_solid_weighing_current_step = self.get_node_value("Solid_Weighing_Current_Step")
            logger.info(f"固体称量当前步骤: {self.m_solid_weighing_current_step}")
            self.m_magnetic_stirrer_current_step = self.get_node_value("Magnetic_Stirrer_Current_Step")
            logger.info(f"磁搅当前步骤: {self.m_magnetic_stirrer_current_step}")

            self.m_initialized = True

            self.set_node_value("Hydration_Workstation_PC_Initialization", False) # 初始化完成，复位
            return {
                "success": True,
                "message": "水合工站初始化完成",
            }
        else:
            logger.error("水合工站初始化失败")
            self.m_initialized = False
            return {
                "success": False,
                "message": "水合工站初始化失败",
            }

        
    @not_action
    def is_robotic_arm_initialization_complete(self)-> bool:
        """
        检查机械臂是否初始化完成

        Returns:
            bool: 如果机械臂初始化完成，返回True，否则返回False
        """
        return self.get_node_value("Robotic_Arm_Initialization_Complete")
    
    @not_action
    def is_solid_weighing_initialization_complete(self)-> bool:
        """
        检查固体称量是否初始化完成

        Returns:
            bool: 如果固体称量初始化完成，返回True，否则返回False
        """
        return self.get_node_value("Solid_Weighing_Initialization_Complete")
    
    @not_action
    def is_pipetting_station_initialization_complete(self)-> bool:
        """
        检查移液站是否初始化完成

        Returns:
            bool: 如果移液站初始化完成，返回True，否则返回False
        """
        return self.get_node_value("Pipetting_Station_Initialization_Complete")
    
    @not_action
    def is_magnetic_stirrer_initialization_complete(self)-> bool:
        """
        检查磁搅是否初始化完成

        Returns:
            bool: 如果磁搅初始化完成，返回True，否则返回False
        """
        return self.get_node_value("Magnetic_Stirrer_Initialization_Complete")
    
    @not_action
    def is_hplc_workstation_initialization_complete(self)-> bool:
        """
        检查HPLC工站是否初始化完成

        Returns:
            bool: 如果HPLC工站初始化完成，返回True，否则返回False
        """
        return self.get_node_value("HPLC_Workstation_Initialization_Complete")

    @not_action
    def is_robotic_arm_idle(self) -> bool:
        """
        检查机械臂是否空闲

        Returns:
            bool: 如果机械臂空闲，返回True，否则返回False
        """
        return self.get_node_value("Robotic_Arm_Idle")

    @not_action
    def is_solid_weighing_occupied(self) -> bool:
        """
        检查固体称量是否占位

        Returns:
            bool: 如果固体称量占位，返回True，否则返回False
        """
        return self.get_node_value("Solid_Weighing_Occupied")
    
    @not_action
    def is_powder_position_in_solid_weighing_occupied(self) -> bool:
        """
        检查粉末头是否在固体称量中占位
        
        Returns:
            bool: 如果粉末头在固体称量中占位，返回True，否则返回False
        """
        return self.get_node_value("Powder_In_Solid_Weighing_Occupied")
    
    @not_action
    def is_pipetting_station_occupied(self) -> bool:
        """
        检查移液站是否占位

        Returns:
            bool: 如果移液站占位，返回True，否则返回False
        """
        return self.get_node_value("Pipetting_Station_Occupied")

    @not_action
    def is_magnetic_stirrer_occupied(self) -> bool:
        """
        检查磁搅是否占位

        Returns:
            bool: 如果磁搅占位，返回True，否则返回False
        """
        return self.get_node_value("Magnetic_Stirrer_Occupied")
    
    @not_action
    def is_hplc_workstation_occupied(self) -> bool:
        """
        检查HPLC工站是否占位

        Returns:
            bool: 如果HPLC工站占位，返回True，否则返回False
        """
        return self.get_node_value("HPLC_Pool_Occupied")
    
    @not_action
    def is_loading_rack_position_occupied(self, position: int) -> bool:
        """
        检查上料架位置是否占位

        Args:
            position (int): 上料架位置，范围[1, 8]

        Returns:
            bool: 如果上料架位置占位，返回True，否则返回False
        """
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error(f"上料架位置错误，必须在范围[{MIN_RACK_POSITION}, {MAX_RACK_POSITION}]内")
            return False
        
        position_index = position - 1
        nodeId = f"Well_Plate_Loading_Rack_InPut[{position_index}]"
        return self.get_node_value(nodeId)
    
    @not_action
    def is_unloading_rack_position_occupied(self, position: int) -> bool:
        """
        检查下料架位置是否占位

        Args:
            position (int): 下料架位置，范围[1, 8]

        Returns:
            bool: 如果下料架位置占位，返回True，否则返回False
        """
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            logger.error(f"下料架位置错误，必须在范围[{MIN_RACK_POSITION}, {MAX_RACK_POSITION}]内")
            return False
        
        position_index = position - 1
        nodeId = f"Well_Plate_Unloading_Rack_InPut[{position_index}]"
        return self.get_node_value(nodeId)

    @not_action
    def is_solid_weighing_stack_position_occupied(self, position: int) -> bool:
        """
        检查固体称量堆栈位置是否占位

        Args:
            position (int): 固体称量堆栈位置，范围[1, 25]

        Returns:
            bool: 如果固体称量堆栈位置占位，返回True，否则返回False
        """
        if position < MIN_SOLID_WEIGHING_STACK_POSITION or position > MAX_SOLID_WEIGHING_STACK_POSITION:
            logger.error(f"固体称量堆栈位置错误，必须在范围[{MIN_SOLID_WEIGHING_STACK_POSITION}, {MAX_SOLID_WEIGHING_STACK_POSITION}]内")
            return False

        position_index = position - 1
        nodeId = f"Powder_Cylinder_InPut[{position_index}]"
        return self.get_node_value(nodeId)

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
        """
        从上料架抓取孔板：
        - 检查机械臂是否空闲
        - 检测上料架位置position处是否有孔板
        - 设置从上料架位置position处抓取孔板
        - 等待从上料架抓取孔板完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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
            logger.error("上料架位置{}没有孔板".format(position))
            return {
                "success": False,
                "message": "上料架位置{}没有孔板".format(position),
            }

        logger.info("从上料架位置{}抓取孔板...".format(position))
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.PLATE_LOADING_RACK) # 设置机械臂目标位置为上料架
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", position) # 设置上料架位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为抓取
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从上料架抓取孔板完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从上料架抓取孔板完成"): # 等待完成状态复位
                logger.info("从上料架抓取孔板完成")
                return {
                    "success": True,
                    "message": "从上料架抓取孔板完成",
                }
            else:
                logger.error("从上料架抓取孔板失败")
                return {
                    "success": False,
                    "message": "从上料架抓取孔板失败，完成复位超时",
                }
        else:
            logger.error("从上料架抓取孔板失败")
            return {
                "success": False,
                "message": "从上料架抓取孔板失败，机械臂动作未完成",
            }
        
    @action(auto_prefix=True, description="步骤4：将孔板放置到固态称量")
    def place_well_plate_to_solid_weighing(self) -> dict:
        """
        将孔板放置到称重区：
        - 检查机械臂是否空闲
        - 检查称重区是否占位
        - 设置将孔板放置到称重区
        - 等待将孔板放置到称重区完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("将孔板放置到称重区...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if self.is_solid_weighing_occupied():
            logger.error("固态称重已占位")
            return {
                "success": False,
                "message": "固态称重已占位",
            }
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING) # 设置机械臂目标位置为称重区
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置称重区位置，1为默认位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为放置
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将孔板放置到固态称重完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将孔板放置到固态称重完成"): # 等待完成状态复位
                logger.info("将孔板放置到固态称重完成")
                return {
                    "success": True,
                    "message": "将孔板放置到固态称重完成",
                }
            else:
                logger.error("将孔板放置到固态称重失败")
                return {
                    "success": False,
                    "message": "将孔板放置到固态称重失败，完成复位超时",
                }
        else:
            logger.error("将孔板放置到固态称重失败")
            return {
                "success": False,
                "message": "将孔板放置到固态称重失败，机械臂动作未完成",
            }
        
    @action(
        auto_prefix=True,
        description="步骤5：从固体称量堆栈抓取粉桶",
        handles=[
            ActionInputHandle(
                key="powder_stack_position",
                data_type="ai4c_powder_stack_position",
                label="粉桶堆栈位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="粉桶所在堆栈位置，范围 1-25",
            )
        ],
    )
    def pick_powder_cylinder_from_stack(self, position: int = 6) -> dict:
        """
        从固体称量堆栈中取粉桶：
        - 检查机械臂是否空闲
        - 检查固体称量堆栈位置是否有粉桶
        - 设置从固体称量堆栈中取粉桶
        - 等待从固体称量堆栈中取粉桶完成
        - 返回成功

        Args:
            position (int): 粉桶位置，1-25

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("从固体称量堆栈中取粉桶...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if position < MIN_SOLID_WEIGHING_STACK_POSITION or position > MAX_SOLID_WEIGHING_STACK_POSITION:
            logger.error("粉桶位置不在有效范围内")
            return {
                "success": False,
                "message": "粉桶位置不在有效范围内",
            }

        if not self.is_solid_weighing_stack_position_occupied(position):
            logger.error("固体称量堆栈位置{}没有粉桶".format(position))
            return {
                "success": False,
                "message": "固体称量堆栈位置{}没有粉桶".format(position),
            }
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING_STACK) # 设置机械臂目标位置为固态称量堆栈
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", position) # 设置固态称量堆栈位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为抓取
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从固体称量堆栈中取粉桶完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从固体称量堆栈中取粉桶完成"): # 等待完成状态复位
                logger.info("从固体称量堆栈中取粉桶完成")
                return {
                    "success": True,
                    "message": "从固体称量堆栈中取粉桶完成",
                }
            else:
                logger.error("从固体称量堆栈中取粉桶失败")
                return {
                    "success": False,
                    "message": "从固体称量堆栈中取粉桶失败，完成复位超时",
                }
        else:
            logger.error("从固体称量堆栈中取粉桶失败")
            return {
                "success": False,
                "message": "从固体称量堆栈中取粉桶失败，机械臂动作未完成",
            }
        
    @action(auto_prefix=True, description="步骤6：将粉桶放置到固态称量")
    def place_powder_cylinder_to_solid_weighing(self) -> dict:
        """
        将粉桶放置到固态称量：
        - 检查机械臂是否空闲
        - 检查固态称量粉桶位置是否有粉桶
        - 设置将粉桶放置到固态称量
        - 等待将粉桶放置到固态称量完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("将粉桶放置到固态称量...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }
        
        if self.is_powder_position_in_solid_weighing_occupied():
            logger.error("固态称量粉桶位置已经有粉桶")
            return {
                "success": False,
                "message": "固态称量粉桶位置已经有粉桶",
            }
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING) # 设置机械臂目标位置为固态称量
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置固态称量位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.ON_POWDER_HEAD) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将粉桶放置到固态称量完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将粉桶放置到固态称量完成"): # 等待完成状态复位
                logger.info("将粉桶放置到固态称量完成")
                return {
                    "success": True,
                    "message": "将粉桶放置到固态称量完成",
                }
            else:
                logger.error("将粉桶放置到固态称量失败")
                return {
                    "success": False,
                    "message": "将粉桶放置到固态称量失败，完成复位超时",
                }
        else:
            logger.error("将粉桶放置到固态称量失败")
            return {
                "success": False,        
                "message": "将粉桶放置到固态称量失败，机械臂动作未完成",
            }

    @action(auto_prefix=True, description="步骤10：从固态称量取回粉桶")
    def pick_powder_cylinder_from_solid_weighing(self) -> dict:
        """
        从固态称量中取粉桶：
        - 检查机械臂是否空闲
        - 检查固态称量粉桶位置是否有粉桶
        - 设置从固态称量中取粉桶
        - 等待从固态称量中取粉桶完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("从固态称量中取粉桶...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_powder_position_in_solid_weighing_occupied():
            logger.error("固态称量粉桶位置没有粉桶")
            return {
                "success": False,
                "message": "固态称量粉桶位置没有粉桶",
            }
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING) # 设置机械臂目标位置为固态称量
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置固态称量位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.OFF_POWDER_HEAD) # 设置动作类型为下粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从固态称量中取粉桶完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从固态称量中取粉桶完成"): # 等待完成状态复位
                logger.info("从固态称量中取粉桶完成")
                return {
                    "success": True,
                    "message": "从固态称量中取粉桶完成",
                }
            else:
                logger.error("从固态称量中取粉桶失败")
                return {
                    "success": False,
                    "message": "从固态称量中取粉桶失败，完成复位超时",
                }
        else:
            logger.error("从固态称量中取粉桶失败")
            return {
                "success": False,
                "message": "从固态称量中取粉桶失败，机械臂动作未完成",
            }
        
    @action(
        auto_prefix=True,
        description="步骤11：将粉桶放回固态称量堆栈",
        handles=[
            ActionInputHandle(
                key="powder_stack_return_position",
                data_type="ai4c_powder_stack_position",
                label="粉桶放回堆栈位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="粉桶放回的堆栈位置，范围 1-25",
            )
        ],
    )
    def place_powder_cylinder_to_solid_weighing_stack(self, position: int = 6) -> dict:
        """
        将粉桶放置到固态称量堆栈：
        - 检查机械臂是否空闲
        - 检查固态称量堆栈位置是否有粉桶
        - 设置将粉桶放置到固态称量堆栈
        - 等待将粉桶放置到固态称量堆栈完成
        - 返回成功

        Args:
            position (int): 粉桶位置，1-25

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("将粉桶放置到固态称量堆栈...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }
        
        if position < MIN_SOLID_WEIGHING_STACK_POSITION or position > MAX_SOLID_WEIGHING_STACK_POSITION:
            logger.error(f"固态称量堆栈位置 {position} 超出范围")
            return {
                "success": False,
                "message": f"固态称量堆栈位置 {position} 超出范围",
            }
        
        if self.is_solid_weighing_stack_position_occupied(position):
            logger.error(f"固态称量堆栈位置 {position} 已有粉桶")
            return {
                "success": False,
                "message": f"固态称量堆栈位置 {position} 已有粉桶",
            }
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING_STACK) # 设置机械臂目标位置为固态称量堆栈
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", position) # 设置固态称量堆栈位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将粉桶放置到固态称量堆栈完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将粉桶放置到固态称量堆栈完成"): # 等待完成状态复位
                logger.info("将粉桶放置到固态称量堆栈完成")
                return {
                    "success": True,
                    "message": "将粉桶放置到固态称量堆栈完成",
                }
            else:
                logger.error("将粉桶放置到固态称量堆栈失败")
                return {
                    "success": False,
                    "message": "将粉桶放置到固态称量堆栈失败，完成复位超时",
                }
        else:
            logger.error("将粉桶放置到固态称量堆栈失败")
            return {
                "success": False,
                "message": "将粉桶放置到固态称量堆栈失败，机械臂动作未完成",
            }
    
    @action(auto_prefix=True, description="步骤12：从固态称量取回孔板")
    def pick_well_plate_from_solid_weighing(self) -> dict:
        """
        从固态称量中取孔板：
        - 检查机械臂是否空闲
        - 检查固态称量位置是否有孔板
        - 设置从固态称量中取孔板
        - 等待从固态称量中取孔板完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("从固态称量中取孔板...")
        if not self.is_robotic_arm_idle():
            logger.error("机械臂不在空闲状态")
            return {
                "success": False,
                "message": "机械臂不在空闲状态",
            }

        if not self.is_solid_weighing_occupied():
            logger.error("固态称量位置没有孔板")
            return {
                "success": False,
                "message": "固态称量位置没有孔板",
            }

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.SOLID_WEIGHING) # 设置机械臂目标位置为固态称量
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置固态称量位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从固态称量中取孔板完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从固态称量中取孔板完成"): # 等待完成状态复位
                logger.info("从固态称量中取孔板完成")
                return {
                    "success": True,
                    "message": "从固态称量中取孔板完成",
                }
            else:
                logger.error("从固态称量中取孔板失败")
                return {
                    "success": False,
                    "message": "从固态称量中取孔板失败，完成复位超时",
                }
        else:
            logger.error("从固态称量中取孔板失败")
            return {
                "success": False,
                "message": "从固态称量中取孔板失败，机械臂动作未完成",
            }
        
    @action(auto_prefix=True, description="步骤14/20：将孔板放置到移液站")
    def place_well_plate_to_pipetting_station(self) -> dict:
        """
        将孔板放置到移液站：
        - 检查机械臂是否空闲
        - 检查移液站位置是否有孔板
        - 设置将孔板放置到移液站
        - 等待将孔板放置到移液站完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.PIPETTING_STATION) # 设置机械臂目标位置为移液站
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置移液站位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将孔板放置到移液站完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将孔板放置到移液站完成"): # 等待完成状态复位
                logger.info("将孔板放置到移液站完成")
                return {
                    "success": True,
                    "message": "将孔板放置到移液站完成",
                }
            else:
                logger.error("将孔板放置到移液站失败")
                return {
                    "success": False,
                    "message": "将孔板放置到移液站失败，完成复位超时",
                }
        else:
            logger.error("将孔板放置到移液站失败")
            return {
                "success": False,
                "message": "将孔板放置到移液站失败，机械臂动作未完成",
            }

    @action(auto_prefix=True, description="步骤16/21：从移液站取回孔板")
    def pick_well_plate_from_pipetting_station(self) -> dict:
        """
        从移液站取孔板：
        - 检查机械臂是否空闲
        - 检查移液站位置是否有孔板
        - 设置从移液站取孔板
        - 等待从移液站取孔板完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.PIPETTING_STATION) # 设置机械臂目标位置为移液站
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置移液站位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从移液站取孔板完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从移液站取孔板完成"): # 等待完成状态复位
                logger.info("从移液站取孔板完成")
                return {
                    "success": True,
                    "message": "从移液站取孔板完成",
                }
            else:
                logger.error("从移液站取孔板失败")
                return {
                    "success": False,
                    "message": "从移液站取孔板失败，完成复位超时",
                }
        else:
            logger.error("从移液站取孔板失败")
            return {
                "success": False,
                "message": "从移液站取孔板失败，机械臂动作未完成",
            }
    
    @action(auto_prefix=True, description="步骤17：将孔板放置到磁搅")
    def place_well_plate_to_magnetic_stirrer(self) -> dict:
        """
        将孔板放置到磁搅：
        - 检查机械臂是否空闲
        - 检查磁搅位置是否有孔板
        - 设置将孔板放置到磁搅
        - 等待将孔板放置到磁搅完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.MAGNETIC_STIRRER) # 设置机械臂目标位置为磁搅
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置磁搅位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将孔板放置到磁搅完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将孔板放置到磁搅完成"): # 等待完成状态复位
                logger.info("将孔板放置到磁搅完成")
                return {
                    "success": True,
                    "message": "将孔板放置到磁搅完成",
                }
            else:
                logger.error("将孔板放置到磁搅失败")
                return {
                    "success": False,
                    "message": "将孔板放置到磁搅失败，完成复位超时",
                }
        else:
            logger.error("将孔板放置到磁搅失败")
            return {
                "success": False,
                "message": "将孔板放置到磁搅失败，机械臂动作未完成",
            }
        
    @action(auto_prefix=True, description="步骤19：从磁搅取回孔板")
    def pick_well_plate_from_magnetic_stirrer(self) -> dict:
        """
        从磁搅取孔板：
        - 检查机械臂是否空闲
        - 检查磁搅位置是否有孔板
        - 设置从磁搅取孔板
        - 等待从磁搅取孔板完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.MAGNETIC_STIRRER) # 设置机械臂目标位置为磁搅
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置磁搅位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从磁搅取孔板完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从磁搅取孔板完成"): # 等待完成状态复位
                logger.info("从磁搅取孔板完成")
                return {
                    "success": True,
                    "message": "从磁搅取孔板完成",
                }
            else:
                logger.error("从磁搅取孔板失败")
                return {
                    "success": False,
                    "message": "从磁搅取孔板失败，完成复位超时",
                }
        else:
            logger.error("从磁搅取孔板失败")
            return {
                "success": False,
                "message": "从磁搅取孔板失败，机械臂动作未完成",
            }
    
    @action(auto_prefix=True, description="步骤22：将孔板放置到 HPLC 站")
    def place_well_plate_to_hplc_station(self) -> dict:
        """
        将孔板放置到 HPLC 站：
        - 检查机械臂是否空闲
        - 检查 HPLC 站位置是否有孔板
        - 设置将孔板放置到 HPLC 站
        - 等待将孔板放置到 HPLC 站完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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

        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.HPLC_STATION) # 设置机械臂目标位置为 HPLC 站
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置 HPLC 站位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将孔板放置到 HPLC 站完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将孔板放置到 HPLC 站完成"): # 等待完成状态复位
                logger.info("将孔板放置到 HPLC 站完成")
                return {
                    "success": True,
                    "message": "将孔板放置到 HPLC 站完成",
                }
            else:
                logger.error("将孔板放置到 HPLC 站失败")
                return {
                    "success": False,
                    "message": "将孔板放置到 HPLC 站失败，完成复位超时",
                }
        else:
            logger.error("将孔板放置到 HPLC 站失败")
            return {
                "success": False,
                "message": "将孔板放置到 HPLC 站失败，机械臂动作未完成",
            }
    
    @action(auto_prefix=True, description="步骤24：从 HPLC 站取回孔板")
    def pick_well_plate_from_hplc_station(self) -> dict:
        """
        从 HPLC 站取孔板：
        - 检查机械臂是否空闲
        - 检查 HPLC 站位置是否有孔板
        - 设置从 HPLC 站取孔板
        - 等待从 HPLC 站取孔板完成
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
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
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.HPLC_STATION) # 设置机械臂目标位置为 HPLC 站
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", 1) # 设置 HPLC 站位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PICK) # 设置动作类型为上粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="从 HPLC 站取孔板完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="从 HPLC 站取孔板完成"): # 等待完成状态复位
                logger.info("从 HPLC 站取孔板完成")
                return {
                    "success": True,
                    "message": "从 HPLC 站取孔板完成",
                }
            else:
                logger.error("从 HPLC 站取孔板失败")
                return {
                    "success": False,
                    "message": "从 HPLC 站取孔板失败，完成复位超时",
                }
        else:
            logger.error("从 HPLC 站取孔板失败")
            return {
                "success": False,
                "message": "从 HPLC 站取孔板失败，机械臂动作未完成",
            }
        
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
        """
        将孔板放置到下料架：
        - 检查机械臂是否空闲
        - 检查下料架位置是否有孔板
        - 设置将孔板放置到下料架
        - 等待将孔板放置到下料架完成
        - 返回成功

        Args:
            position (int): 下料架位置

        Returns:
            dict: 包含 success 和 message
        """
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
        
        self.set_node_value("Robotic_Arm_Target_Position_Code", RoboticArmTargetPosition.PLATE_UNLOADING_RACK) # 设置机械臂目标位置为下料架
        self.set_node_value("Robotic_Arm_Target_Pick_Place_Code", position) # 设置下料架位置
        self.set_node_value("Robotic_Arm_Action_Code", RoboticArmAction.PLACE) # 设置动作类型为下粉末头
        self.set_node_value("Robotic_Arm_Action_Trigger", True) # 设置动作触发
        if self._wait_until_true("Robotic_Arm_Action_Complete", description="将孔板放置到下料架完成"):
            self.set_node_value("Robotic_Arm_Action_Trigger", False) # 复位动作触发
            if self._wait_until_false("Robotic_Arm_Action_Complete", description="将孔板放置到下料架完成"): # 等待完成状态复位
                logger.info("将孔板放置到下料架完成")
                return {
                    "success": True,
                    "message": "将孔板放置到下料架完成",
                }
            else:
                logger.error("将孔板放置到下料架失败")
                return {
                    "success": False,
                    "message": "将孔板放置到下料架失败，完成复位超时",
                }
        else:
            logger.error("将孔板放置到下料架失败")
            return {
                "success": False,
                "message": "将孔板放置到下料架失败，机械臂动作未完成",
            }

    @action(auto_prefix=True, description="步骤3/9：打开固态称量门")
    def open_solid_weighing_door(self) -> dict:
        """
        打开固态称重门：
        - 检查固态称重门是否关闭
        - 打开固态称重门
        - 等待固态称重门打开
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("打开固态称重门...")
        self.set_node_value("Solid_Weighing_Close_Door", False)
        self.set_node_value("Solid_Weighing_Open_Door", True) # 打开固态称重门
        if self._wait_until_true("Solid_Weighing_Open_Door_Complete", description="固态称重门打开完成"):
            return {
                "success": True,
                "message": "固态称重门打开完成",
            }
        else:
            logger.error("固态称重门打开失败")
            return {
                "success": False,
                "message": "固态称重门打开失败",
            }
        
    @action(auto_prefix=True, description="步骤7/13：关闭固态称量门")
    def close_solid_weighing_door(self) -> dict:
        """
        关闭固态称重门：
        - 检查固态称重门是否打开
        - 关闭固态称重门
        - 等待固态称重门关闭
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("关闭固态称重门...")
        self.set_node_value("Solid_Weighing_Open_Door", False)
        self.set_node_value("Solid_Weighing_Close_Door", True) # 关闭固态称重门
        if self._wait_until_true("Solid_Weighing_Close_Door_Complete", description="固态称重门关闭完成"):
            return {
                "success": True,
                "message": "固态称重门关闭完成",
            }
        else:
            logger.error("固态称重门关闭失败")
            return {
                "success": False,
                "message": "固态称重门关闭失败",
            }
        
    @action(
        auto_prefix=True,
        description="步骤8：触发固体称量",
        handles=[
            ActionInputHandle(
                key="solid_weighing_gram",
                data_type="ai4c_solid_weighing_gram",
                label="称重目标值",
                data_key="gram",
                data_source=DataSource.HANDLE,
                description="固体称量目标值",
            ),
            ActionInputHandle(
                key="solid_weighing_tolerance",
                data_type="ai4c_solid_weighing_tolerance",
                label="称重误差",
                data_key="tolerance",
                data_source=DataSource.HANDLE,
                description="固体称量允许误差",
            ),
            ActionInputHandle(
                key="solid_weighing_slot",
                data_type="ai4c_solid_weighing_slot",
                label="称量槽位",
                data_key="slot",
                data_source=DataSource.HANDLE,
                description="固体称量槽位",
            ),
        ],
    )
    def trigger_solid_weighing(self, gram: int = 10, tolerance: int = 1, slot: int = 1) -> dict:
        """
        触发固体称重：
        - 检查固态称重是否已占位
        - 检查固态称重粉桶位置已占位
        - 设置固体称重参数
        - 触发固体称重
        - 等待固体称重完成
        - 返回成功

        Args:
            gram (int): 称重目标值
            tolerance (int): 称重误差
            slot (int): 称重器位置

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("触发固体称重...")
        if not self.is_solid_weighing_occupied():
            logger.error("固态称重位置没有孔板")
            return {
                "success": False,
                "message": "固态称重位置没有孔板",
            }

        if not self.is_powder_position_in_solid_weighing_occupied():
            logger.error("固态称重位置没有粉桶")
            return {
                "success": False,
                "message": "固态称重位置没有粉桶",
            }

        self.set_node_value("Solid_Weighing_Weight_in_Grams", gram) # 设置称重目标值
        self.set_node_value("Solid_Weighing_Error", tolerance) # 设置称重误差
        self.set_node_value("Solid_Weighing_Slot_Position", slot) # 设置称重器穴位
        self.set_node_value("Solid_Weighing_Processing_Allowed", True) # 设置允许加工
        # 等待加工完成
        if self._wait_until_true("Solid_Weighing_Processing_Complete", description="固体称重完成"):
            self.set_node_value("Solid_Weighing_Processing_Allowed", False) # 复位允许加工
            if (self._wait_until_false("Solid_Weighing_Processing_Complete", description="固体称重完成")):
                logger.info("固体称重完成")
                return {
                    "success": True,
                    "message": "固体称重完成",
                }
            else:
                logger.error("固体称重失败")
                return {
                    "success": False,
                    "message": "固体称重失败，完成复位超时",
                }
        else:
            logger.error("固体称重失败")
            return {
                "success": False,
                "message": "固体称重失败，动作超时",
            }
        
    @action(
        auto_prefix=True,
        description="步骤18：触发磁力搅拌",
        handles=[
            ActionInputHandle(
                key="magnetic_stirrer_speed",
                data_type="ai4c_magnetic_stirrer_speed",
                label="搅拌速度",
                data_key="speed",
                data_source=DataSource.HANDLE,
                description="磁力搅拌速度",
            ),
            ActionInputHandle(
                key="magnetic_stirrer_temperature",
                data_type="ai4c_magnetic_stirrer_temperature",
                label="搅拌温度",
                data_key="temperature",
                data_source=DataSource.HANDLE,
                description="磁力搅拌温度",
            ),
            ActionInputHandle(
                key="magnetic_stirrer_minutes",
                data_type="ai4c_magnetic_stirrer_minutes",
                label="搅拌时间",
                data_key="mins",
                data_source=DataSource.HANDLE,
                description="磁力搅拌时间，单位分钟",
            ),
        ],
    )
    def trigger_magnetic_stirrer(self, speed: int = 100, temperature: int = 30, mins: int = 1) -> dict:
        """
        触发磁力搅拌：
        - 等待磁力搅拌请求加工信号
        - 检查磁力搅拌是否已占位
        - 设置搅拌参数
        - 触发搅拌参数已下发
        - 等待搅拌完成
        - 返回成功

        Args:
            speed (int): 搅拌速度
            temperature (int): 搅拌温度
            mins (int): 搅拌时间(分钟)

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("触发磁力搅拌...")
        if not self._wait_until_true("Magnetic_Stirrer_Processing_Request", description="等待磁力搅拌请求加工信号"):
            logger.error("等待磁力搅拌请求加工信号超时")
            return {
                "success": False,
                "message": "等待磁力搅拌请求加工信号超时",
            }

        if not self.is_magnetic_stirrer_occupied():
            logger.error("磁力搅拌位置没有孔板")
            return {
                "success": False,
                "message": "磁力搅拌位置没有孔板",
            }

        self.set_node_value("Magnetic_Stirrer_Speed_Parameter", speed) # 设置搅拌速度
        self.set_node_value("Magnetic_Stirrer_Temperature_Parameter", temperature) # 设置搅拌温度
        self.set_node_value("Magnetic_Stirrer_Time_Parameter", mins) # 设置搅拌时间
        self.set_node_value("Magnetic_Stirrer_Parameters_Sent", True) # 触发搅拌参数已下发
        # 等待参数已执行
        if self._wait_until_true("Magnetic_Stirrer_Parameters_Executed", description="等待搅拌参数已执行"):
            # 复位搅拌参数已下发
            self.set_node_value("Magnetic_Stirrer_Parameters_Sent", False) # 复位搅拌参数已下发
            logger.info("搅拌参数已执行")
        else:
            logger.error("搅拌参数执行失败")
            return {
                "success": False,
                "message": "搅拌参数执行失败",
            }
        
        # 等待加工完成
        if self._wait_until_true("Magnetic_Stirrer_Processing_Complete", description="等待搅拌完成", timeout=mins*60.0+100.0):
            logger.info("搅拌完成")
            return {
                "success": True,
                "message": "搅拌完成",
            }
        else:
            logger.error("搅拌失败")
            return {
                "success": False,
                "message": "搅拌失败，动作超时",
            }

    @action(
        auto_prefix=True,
        description="步骤15：触发移液",
        handles=[
            ActionInputHandle(
                key="pipetting_param",
                data_type="ai4c_pipetting_param",
                label="移液参数",
                data_key="param",
                data_source=DataSource.HANDLE,
                description="移液站参数",
            )
        ],
    )
    def trigger_pipetting(self, param: int) -> dict:
        """
        触发移液：
        - 等待移液请求加工信号
        - 检查移液位置是否已占位
        - 设置移液参数
        - 触发移液参数已下发
        - 等待移液完成
        - 返回成功

        Args:
            param (int): 移液参数

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("触发移液...")

        time.sleep(3)
        return {
            "success": True,
            "message": "移液完成",
        }

        '''
        if not self._wait_until_true("Pipetting_Station_Processing_Request", description="等待移液请求加工信号"):
            logger.error("等待移液请求加工信号超时")
            return {
                "success": False,
                "message": "等待移液请求加工信号超时",
            }

        if not self.is_pipetting_station_occupied():
            logger.error("移液位置没有孔板")
            return {
                "success": False,
                "message": "移液位置没有孔板",
            }
        
        self.set_node_value("Pipetting_Station_Parameter_Setting", param) # 设置移液参数
        self.set_node_value("Pipetting_Station_Parameters_Sent", True) # 触发移液参数已下发
        # 等待参数已执行
        if self._wait_until_true("Pipetting_Station_Parameters_Executed", description="等待移液参数已执行"):
            # 复位搅拌参数已下发
            self.set_node_value("Pipetting_Station_Parameters_Sent", False) # 复位搅拌参数已下发
            if self._wait_until_false("Pipetting_Station_Parameters_Executed", description="等待移液参数已执行复位"):
                logger.info("移液参数已执行")
            else:
                logger.error("移液参数执行失败")
                return {
                    "success": False,
                    "message": "移液参数执行失败，完成复位超时",
                }
        else:
            logger.error("移液参数执行失败")
            return {
                "success": False,
                "message": "移液参数执行失败",
            }

        # 等待加工完成
        if self._wait_until_true("Pipetting_Station_Processing_Complete", description="等待移液完成"):
            logger.info("移液完成")
            return {
                "success": True,
                "message": "移液完成",
            }
        else:
            logger.error("移液失败")
            return {
                "success": False,
                "message": "移液失败，动作超时",
            }
        '''
        
    @action(
        auto_prefix=True,
        description="步骤23：触发 HPLC",
        handles=[
            ActionInputHandle(
                key="hplc_param",
                data_type="ai4c_hplc_param",
                label="HPLC 参数",
                data_key="param",
                data_source=DataSource.HANDLE,
                description="HPLC 加工参数",
            )
        ],
    )
    def trigger_hplc(self, param: int = 1) -> dict:
        """
        触发 HPLC：
        - 等待 HPLC 请求加工信号
        - 检查 HPLC 位置是否已占位
        - 设置 HPLC 参数
        - 触发 HPLC 参数已下发
        - 等待 HPLC 完成
        - 返回成功

        Args:
        - param (int): HPLC 参数

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("触发 HPLC...")

        time.sleep(5)

        return {
            "success": True,
            "message": "HPLC 完成",
        }
    
        '''
        if not self._wait_until_true("HPLC_Processing_Request", description="等待 HPLC 请求加工信号"):
            logger.error("等待 HPLC 请求加工信号超时")
            return {
                "success": False,
                "message": "等待 HPLC 请求加工信号超时",
            }
        
        if not self.is_hplc_workstation_occupied():
            logger.error("HPLC 位置没有孔板")
            return {
                "success": False,
                "message": "HPLC 位置没有孔板",
            }

        # self.set_node_value("HPLC_Parameter_Setting", param) # 设置 HPLC 参数
        self.set_node_value("HPLC_Parameters_Sent", True) # 触发 HPLC 参数已下发
        # 等待参数已执行
        if self._wait_until_true("HPLC_Parameters_Executed", description="等待 HPLC 参数已执行"):
            # 复位 HPLC 参数已下发
            self.set_node_value("HPLC_Parameters_Sent", False) # 复位 HPLC 参数已下发
            if self._wait_until_false("HPLC_Parameters_Executed", description="等待 HPLC 参数已执行复位"):
                logger.info("HPLC 参数已执行")
            else:
                logger.error("HPLC 参数执行失败")
                return {
                    "success": False,
                    "message": "HPLC 参数执行失败，完成复位超时",
                }
        else:
            logger.error("HPLC 参数执行失败")
            return {
                "success": False,
                "message": "HPLC 参数执行失败",
            }
        
        # 等待加工完成
        if self._wait_until_true("HPLC_Processing_Complete", description="等待 HPLC 完成"):
            logger.info("HPLC 完成")
            return {
                "success": True,
                "message": "HPLC 完成",
            }
        else:
            logger.error("HPLC 失败")
            return {
                "success": False,
                "message": "HPLC 失败，动作超时",
            }
        '''
    
    @not_action
    def trigger_heart_beat(self) -> None:
        """
        写入心跳
        """
        if self.heartbeat_on:
            # logger.info("写心跳")
            value = self.get_node_value("Heart_Beat")
            self.set_node_value("Heart_Beat", not value)

            if self.m_initialized:
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

            # 再次启动定时器，形成循环
            timer = threading.Timer(1.0, self.trigger_heart_beat)
            timer.daemon = True
            timer.start()
    
    @not_action
    def start_heart_beat(self) -> None:
        """
        启动心跳
        """
        logger.info("启动心跳")
        timer = threading.Timer(1.0, self.trigger_heart_beat)
        timer.daemon = True
        timer.start()
        self.heartbeat_on = True
    
    @not_action
    def stop_heart_beat(self) -> None:
        """
        停止心跳
        """
        logger.info("停止心跳")
        self.set_node_value("Heart_Beat", False)
        self.heartbeat_on = False
        
    @not_action
    def trigger_all_process(self) -> dict:
        """
        触发所有加工，从1号上料架抓取孔板后完成整个流程，最后放置在1号下料架上，
        固态称重从1号和2号堆栈获取粉桶，完成后放置到1号和2号堆栈
        称量使用固定参数
        磁搅也使用固定参数

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("触发所有流程...")
        ret = self.pick_well_plate_from_loading_rack(1)
        if not ret["success"]:
            return ret
        
        ret = self.open_solid_weighing_door()
        if not ret["success"]:
            return ret

        ret = self.place_well_plate_to_solid_weighing()
        if not ret["success"]:
            return ret
        
        ret = self.pick_powder_cylinder_from_stack(6)
        if not ret["success"]:
            return ret

        ret = self.place_powder_cylinder_to_solid_weighing()
        if not ret["success"]:
            return ret
        
        ret = self.close_solid_weighing_door()
        if not ret["success"]:
            return ret

        ret = self.trigger_solid_weighing(10, 1, 1)
        if not ret["success"]:
            return ret
        
        ret = self.open_solid_weighing_door()
        if not ret["success"]:
            return ret

        ret = self.pick_powder_cylinder_from_solid_weighing()
        if not ret["success"]:
            return ret
        
        ret = self.place_powder_cylinder_to_solid_weighing_stack(6)
        if not ret["success"]:
            return ret
        
        #ret = self.pick_powder_cylinder_from_stack(7)
        #if not ret["success"]:
        #    return ret

        #ret = self.place_powder_cylinder_to_solid_weighing()
        #if not ret["success"]:
        #    return ret
        
        #ret = self.close_solid_weighing_door()
        #if not ret["success"]:
        #    return ret

        #ret = self.trigger_solid_weighing(10, 1, 2)
        #if not ret["success"]:
        #    return ret
        
        #ret = self.open_solid_weighing_door()
        #if not ret["success"]:
        #    return ret

        #ret = self.pick_powder_cylinder_from_solid_weighing()
        #if not ret["success"]:
        #    return ret
        
        #ret = self.place_powder_cylinder_to_solid_weighing_stack(7)
        #if not ret["success"]:
        #    return ret
        
        ret = self.pick_well_plate_from_solid_weighing()
        if not ret["success"]:
            return ret
        
        ret = self.close_solid_weighing_door()
        if not ret["success"]:
            return ret

        ret = self.place_well_plate_to_pipetting_station()
        if not ret["success"]:
            return ret

        ret = self.trigger_pipetting(1)
        if not ret["success"]:
            return ret
        
        ret = self.pick_well_plate_from_pipetting_station()
        if not ret["success"]:
            return ret

        ret = self.place_well_plate_to_magnetic_stirrer()
        if not ret["success"]:
            return ret

        ret = self.trigger_magnetic_stirrer(100, 30, 1)
        if not ret["success"]:
            return ret

        ret = self.pick_well_plate_from_magnetic_stirrer()
        if not ret["success"]:
            return ret
        
        ret = self.place_well_plate_to_pipetting_station()
        if not ret["success"]:
            return ret

        ret = self.pick_well_plate_from_pipetting_station()
        if not ret["success"]:
            return ret

        ret = self.place_well_plate_to_hplc_station()
        if not ret["success"]:
            return ret
        
        ret = self.trigger_hplc(1)
        if not ret["success"]:
            return ret
        
        ret = self.pick_well_plate_from_hplc_station()
        if not ret["success"]:
            return ret

        ret = self.place_well_plate_to_unloading_rack(1)
        if not ret["success"]:
            return ret

        return {
            "success": True,
            "message": "所有加工完成",
        }


    def _wait_until_true(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 0.2,
        description: str = None
    ) -> bool:
        """等待布尔节点变为 True（轮询时强制从 OPC UA 服务器读取，避免订阅缓存过期）"""
        desc = description or node_name
        logger.info(f"等待 {desc} 变为 True（轮询节点: {node_name}）...")
        
        start = time.time()
        while True:
            value = self.get_node_value(node_name, force_read=True)
            logger.debug(f"轮询节点 [{node_name}] = {value!r}，目标 True（{desc}）")
            if value:
                logger.info(f"✓ {desc} 已变为 True（节点 [{node_name}]）")
                return True
            
            if time.time() - start >= timeout:
                logger.error(f"✗ 等待 {desc} 超时（{timeout}秒，节点 [{node_name}] 仍为 {value!r}）")
                return False
            
            time.sleep(interval)
    
    def _wait_until_false(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 0.2,
        description: str = None
    ) -> bool:
        """等待布尔节点变为 False（轮询时强制从 OPC UA 服务器读取，避免订阅缓存过期）"""
        desc = description or node_name
        logger.info(f"等待 {desc} 变为 False（轮询节点: {node_name}）...")
        
        start = time.time()
        while True:
            value = self.get_node_value(node_name, force_read=True)
            logger.debug(f"轮询节点 [{node_name}] = {value!r}，目标 False（{desc}）")
            if not value:
                logger.info(f"✓ {desc} 已变为 False（节点 [{node_name}]）")
                return True
            
            if time.time() - start >= timeout:
                logger.error(f"✗ 等待 {desc} 超时（{timeout}秒，节点 [{node_name}] 仍为 {value!r}）")
                return False
            
            time.sleep(interval)
    
    def _wait_for_nodes(
        self,
        conditions: dict,  # {node_name: target_value, ...}
        timeout: float = 300.0,
        interval: float = 0.2
    ) -> bool:
        """等待多个节点同时满足条件"""
        start = time.time()
        while True:
            all_met = all(
                self.get_node_value(name, force_read=True) == target
                for name, target in conditions.items()
            )
            if all_met:
                return True
            
            if time.time() - start >= timeout:
                return False
            
            time.sleep(interval)


if __name__ == '__main__':
    # 调试用法
    A4 = AI4CDevice(
        url="opc.tcp://jdht1471820.bohrium.tech:50003",
        csv_path=os.path.dirname(os.path.abspath(__file__)) + "/ai4c_sim_updated.csv"
    )

    # 启动心跳
    # A4.start_heart_beat()

    logger.setLevel(logging.INFO)

    time.sleep(3)

    # 初始化工作站
    A4.init_workstation()
    
    # 显示命令行，让用户通过选择序号来完成相应的操作
    # 如果带有参数，则序号和各参数之间均由空格分隔
    # 具体命令如下：
    # 1	初始化
    # 2	从上料架抓取孔板
    # 3	放置孔板到固态称量
    # 4	从固态称量堆栈抓取粉桶
    # 5	将粉桶上到固态称量中
    # 6	将粉桶从固态称量中下来
    # 7	将粉桶放到固态称量架
    # 8	将孔板从固态称量中取出
    # 9	放置孔板到移液站
    # 10 从移液站抓取孔板
    # 11 放置孔板到磁搅
    # 12 从磁搅抓取孔板
    # 13 放置孔板到HPLC
    # 14 从HPLC抓取孔板
    # 15 放置孔板到下料架
    # 16 进行固态称量
    # 17 进行磁力搅拌
    # 18 进行移液动作
    # 19 进行HPLC动作
    # 20 固态称量开门
    # 21 固态称量关门
    # 98 整体流程
    # 99 退出
    while True:
        print("请选择操作：")
        print("1 初始化")
        print("2 从上料架抓取孔板")
        print("3 放置孔板到固态称量")
        print("4 从固态称量堆栈抓取粉桶")
        print("5 将粉桶上到固态称量中")
        print("6 将粉桶从固态称量中下来")
        print("7 将粉桶放到固态称量架")
        print("8 将孔板从固态称量中取出")
        print("9 放置孔板到移液站")
        print("10 从移液站抓取孔板")
        print("11 放置孔板到磁搅")
        print("12 从磁搅抓取孔板")
        print("13 放置孔板到HPLC")
        print("14 从HPLC抓取孔板")
        print("15 放置孔板到下料架")
        print("16 进行固态称量")
        print("17 进行磁力搅拌")
        print("18 进行移液动作")
        print("19 进行HPLC动作")
        print("20 固态称量开门")
        print("21 固态称量关门")
        print("98 整体流程")
        print("99 退出")
        choice = input("请输入操作序号：")
        if choice == "99":
            break
        else:
            print("执行操作...")
            # 根据用户输入的序号执行相应的操作
            if choice == "1":
                A4.init_workstation()
            elif choice.startswith("2 "):
                # 获取2后的参数，即上料架位置
                rack_pos = int(choice.split(" ")[1])
                A4.pick_well_plate_from_loading_rack(rack_pos)
            elif choice == "3":
                A4.place_well_plate_to_solid_weighing()
            elif choice.startswith("4 "):
                # 获取4后的参数，即固态称量堆栈位置
                stack_pos = int(choice.split(" ")[1])
                A4.pick_powder_cylinder_from_stack(stack_pos)
            elif choice == "5":
                A4.place_powder_cylinder_to_solid_weighing()
            elif choice == "6":
                A4.pick_powder_cylinder_from_solid_weighing()
            elif choice.startswith("7 "):
                stack_pos = int(choice.split(" ")[1])
                A4.place_powder_cylinder_to_solid_weighing_stack(stack_pos)
            elif choice == "8":
                A4.pick_well_plate_from_solid_weighing()
            elif choice == "9":
                A4.place_well_plate_to_pipetting_station()
            elif choice == "10":
                A4.pick_well_plate_from_pipetting_station()
            elif choice == "11":
                A4.place_well_plate_to_magnetic_stirrer()
            elif choice == "12":
                A4.pick_well_plate_from_magnetic_stirrer()
            elif choice == "13":
                A4.place_well_plate_to_hplc_station()
            elif choice == "14":
                A4.pick_well_plate_from_hplc_station()
            elif choice.startswith("15 "):
                rack_pos = int(choice.split(" ")[1])
                A4.place_well_plate_to_unloading_rack(rack_pos)
            elif choice.startswith("16 "):
                gram = int(choice.split(" ")[1])
                tolerance = int(choice.split(" ")[2])
                slot = int(choice.split(" ")[3])
                A4.trigger_solid_weighing(gram, tolerance, slot)
            elif choice.startswith("17 "):
                speed = int(choice.split(" ")[1])
                temperature = int(choice.split(" ")[2])
                mins = int(choice.split(" ")[3])
                A4.trigger_magnetic_stirrer(speed, temperature, mins)
            elif choice.startswith("18 "):
                param = int(choice.split(" ")[1])
                A4.trigger_pipetting(param)
            elif choice.startswith("19 "):
                A4.trigger_hplc(param)
            elif choice == "20":
                A4.open_solid_weighing_door()
            elif choice == "21":
                A4.close_solid_weighing_door()
            elif choice == "98":
                A4.trigger_all_process()
            else:
                print("无效的操作序号，请重新输入。")

    # 结束心跳
    # A4.stop_heart_beat()

    # 断开连接
    A4.disconnect()

    print("退出程序。")



