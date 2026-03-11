"""
AI4M 设备驱动
继承自 OPC UA 通讯基类，实现具体的设备动作函数
"""

import json
import time
import traceback
from typing import Optional, List
import os
import threading
import requests

from unilabos.resources.resource_tracker import ResourceTreeSet, SampleUUIDsType, LabSample
from unilabos.utils.log import logger
from unilabos.utils.decorator import not_action
from unilabos.devices.workstation.AI4M.decks import AI4M002_deck
from unilabos.devices.workstation.AI4M.bottle_carriers import Hydrogel_Clean_1BottleCarrier

# 导入通讯基类
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription


class AI4M002Device(OpcUaClientWithSubscription):
    """
    AI4M 设备类
    继承自 OpcUaClientWithSubscription，实现具体的设备动作函数
    """
    
    def __init__(
        self, 
        url: str, 
        deck: Optional[AI4M002_deck] = None,
        csv_path: str = None, 
        username: str = None, 
        password: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0,
        subscription_interval: int = 500,
        bts_base_url: str = "http://localhost:8089",
        bts_validate_code: str = "bts-validate-code-2024",
        *args,
        **kwargs,
    ):
        """
        初始化 AI4M 设备
        
        参数:
            url: OPC UA 服务器地址
            deck: AI4M 资源树配置
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

        # 处理 deck 参数
        if deck is None or isinstance(deck.get("data") if isinstance(deck, dict) else deck, dict):
            self.deck = AI4M002_deck(setup=True)
        else:
            self.deck = deck.get("data") if isinstance(deck, dict) else deck

        if self.deck is None:
            raise ValueError("Deck 配置不能为空")

        # 创建三轴操作的线程锁，防止多个动作同时执行
        self._3axis_lock = threading.Lock()
        logger.info("✓ 三轴操作线程锁已初始化")

        # 统计仓库信息
        warehouse_count = 0
        if hasattr(self.deck, 'children'):
            warehouse_count = len(self.deck.children)
            logger.info(f"Deck 初始化完成，加载 {warehouse_count} 个资源")
        
        # 如果提供了 CSV 路径，则直接加载节点
        if csv_path:
            self.load_nodes_from_csv(csv_path)

        # BTS HTTP API 配置
        self._bts_base_url = bts_base_url
        self._bts_validate_code = bts_validate_code
        self._bts_session = requests.Session()
        self._bts_validated = False

    @not_action
    def post_init(self, ros_node):
        """ROS2 节点就绪后的初始化"""
        if not (hasattr(self, 'deck') and self.deck):
            return
            
        if not (hasattr(ros_node, 'resource_tracker') and ros_node.resource_tracker):
            logger.warning("resource_tracker 不存在，无法注册 deck")
            return
        
        # 保存 ros_node 引用
        self._ros_node = ros_node
        
        # 1. 本地注册（必需）
        ros_node.resource_tracker.add_resource(self.deck)
        
        # 2. 上传云端
        try:
            from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
            ROS2DeviceNode.run_async_func(
                ros_node.update_resource,
                True,
                resources=[self.deck]
            )
            logger.info("Deck 已上传到云端")
        except Exception as e:
            logger.error(f"上传失败: {e}")
   
    # ==================== 设备动作函数 ====================
    
    def _sync_resource_to_frontend(self) -> None:
        """将资源树同步到前端，使物料转移的中间状态能实时显示"""
        if hasattr(self, '_ros_node') and self._ros_node:
            try:
                from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                logger.info("✓ 已同步资源更新到前端")
            except Exception as e:
                logger.warning(f"前端资源更新失败: {e}")
    
    def trigger_s02_init(self) -> dict:
        """
        S02工站初始化函数：
        - 将S02工站初始化PC写true
        - 等待S02工站初始化完成为true
        - 将S02工站初始化PC写false
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("开始S02工站初始化...")
        
        # 将S02工站初始化PC写false（先复位）
        logger.info("复位S02工站初始化PC...")
        self.set_node_value("S02_Station_Initialization_PC", False)
        time.sleep(1.0)
        
        # 将S02工站初始化PC写true
        logger.info("设置S02工站初始化PC为true...")
        self.set_node_value("S02_Station_Initialization_PC", True)
        time.sleep(1.0)
        
        # 等待S02工站初始化完成为true
        logger.info("等待S02工站初始化完成...")
        init_done = self.get_node_value("S02_Station_Initialization_Done")
        while not init_done:
            logger.info("S02工站初始化中...")
            time.sleep(1.0)
            init_done = self.get_node_value("S02_Station_Initialization_Done")
        
        # 将S02工站初始化PC写false
        logger.info("S02工站初始化完成，设置初始化PC为false...")
        self.set_node_value("S02_Station_Initialization_PC", False)
        
        return {
            "message": "S02工站初始化完成",
        }
    
    def trigger_3axis_pick_from_raw_and_place_to_electrolytic_cell(
        self,
        pick_code: int,
        electrolytic_cell_id: Optional[int] = None,
        sample_uuids: SampleUUIDsType = None,
    ) -> dict:
        """
        从原始电极取料，动作完成，放到电解池
        使用进程锁保证同一时间只有一个三轴操作
        
        流程：
        1. 检查电解池是否空闲（如果electrolytic_cell_id为空，自动查找空闲电解池）
        2. 如果都有占位，释放进程锁并等待
        3. 获取进程锁
        4. 从原始电极仓库取料
        5. 等待动作完成
        6. 放到指定的电解池（搅拌仪）
        7. 等待动作完成
        
        Args:
            electrolytic_cell_id: 电解池ID（1或2），如果为None则自动查找空闲电解池
                - 1: 电解池1（对应搅拌仪1，位置3）
                - 2: 电解池2（对应搅拌仪2，位置2）
            pick_code: 目标取放料代码（对应3-Axis_Target_Pick_&_Place_Code，用于取料）
        
        Returns:
            dict: 包含 message 和相关信息
        """
        logger.info(f"开始流程：从原始电极取料 -> 放到电解池（电解池ID：{electrolytic_cell_id}，取放料代码：{pick_code}）")
        
        # 电解池映射：ID -> (位置代码, 占位节点, 名称)
        electrolytic_cell_map = {
            1: (3, "Electrolytic_Cell_1_Occupancy", "电解池1（搅拌仪1）"),
            2: (2, "Electrolytic_Cell_2_Occupancy", "电解池2（搅拌仪2）"),
        }
        
        # 使用线程锁保证同一时间只有一个三轴操作
        while True:
            # 确定目标电解池ID
            target_cell_id = electrolytic_cell_id
            
            # 如果未指定电解池ID，自动查找空闲电解池
            if target_cell_id is None:
                logger.info("未指定电解池ID，自动查找空闲电解池...")
                target_cell_id = None
                for cell_id in (1, 2):
                    _, occupancy_node, _ = electrolytic_cell_map[cell_id]
                    occupancy = self.get_node_value(occupancy_node)
                    if not occupancy:
                        target_cell_id = cell_id
                        logger.info(f"找到空闲电解池：{target_cell_id}")
                        break
                
                if target_cell_id is None:
                    # 都有占位，不获取锁，等待
                    logger.info("所有电解池都有占位，等待空闲...")
                    time.sleep(1.0)
                    continue
            
            # 检查指定电解池是否空闲
            _, occupancy_node, cell_name = electrolytic_cell_map[target_cell_id]
            logger.info(f"检查{cell_name}是否空闲...")
            occupancy = self.get_node_value(occupancy_node)
            if occupancy:
                # 不空闲，不获取锁，等待
                logger.info(f"{cell_name}忙碌中，等待空闲...")
                time.sleep(1.0)
                continue
            
            # 电解池空闲，获取锁
            logger.info(f"{cell_name}已空闲，尝试获取三轴操作锁...")
            self._3axis_lock.acquire()
            logger.info("已获取三轴操作锁")
            
            try:
                # 再次确认电解池仍然空闲
                occupancy = self.get_node_value(occupancy_node)
                if occupancy:
                    # 状态变化，释放锁并重新等待
                    logger.info(f"{cell_name}状态变化，释放锁并重新等待...")
                    self._3axis_lock.release()
                    logger.info("已释放三轴操作锁")
                    time.sleep(1.0)
                    continue
                
                # 确认空闲，继续执行
                break
            except Exception as e:
                # 如果出现异常，确保释放锁
                self._3axis_lock.release()
                logger.error(f"检查电解池状态时出错，已释放锁: {e}")
                raise
        
        try:
            target_position, _, cell_name = electrolytic_cell_map[target_cell_id]
            
            # 获取仓库资源
            raw_warehouse = self.deck.warehouses["原始电极堆栈"]
            station_warehouse = self.deck.warehouses[f"搅拌仪{target_cell_id}"]
            raw_site_key = str(pick_code)
            
            # 在执行硬件操作之前，尝试获取载具（物料转移失败不终止硬件执行）
            try:
                carrier = raw_warehouse[raw_site_key]
            except Exception as e:
                logger.warning(f"获取原始电极堆栈位置 {raw_site_key} 载具失败（不影响硬件操作）: {e}")
                carrier = None
            if carrier is None:
                logger.warning(f"原始电极堆栈位置 {raw_site_key} 没有载具，将跳过物料转移，硬件照常执行")
            
            # 步骤1：从原始电极取料
            logger.info("步骤1：从原始电极仓库取料...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置取料动作
            logger.info(f"设置三轴动作代码：1（取料），目标位置代码：1（原始电极仓库），取放料代码：{pick_code}")
            self.set_node_value("3-Axis_Action_Code", 1)
            self.set_node_value("3-Axis_Target_Position_Code", 1)
            self.set_node_value("3-Axis_Target_Pick_&_Place_Code", pick_code)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            self.set_node_value("3-Axis_Action_Trigger", False)
            time.sleep(1.0)
            
            # 触发动作
            logger.info("触发三轴取料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴取料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴取料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 从原始电极仓库取料完成")
            
            # 阶段1.5：三轴取料完成后，从原始电极堆栈解绑载具
            if carrier is not None:
                try:
                    raw_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ 已从原始电极堆栈解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"从原始电极堆栈解绑载具失败（不影响硬件操作）: {e}")
            
            # 步骤2：放到电解池
            logger.info(f"步骤2：放到{cell_name}...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置放料动作
            logger.info(f"设置三轴动作代码：2（放料），目标位置代码：{target_position}（{cell_name}），取放料代码：1（放料）")
            self.set_node_value("3-Axis_Action_Code", 2)
            self.set_node_value("3-Axis_Target_Position_Code", target_position)
            self.set_node_value("3-Axis_Target_Pick_&_Place_Code", 1)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 触发动作
            logger.info("触发三轴放料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴放料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴放料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info(f"✓ 放到{cell_name}完成")
            
            # 阶段2.5：三轴放到电解池完成后，绑定载具到搅拌仪
            if carrier is not None:
                try:
                    station_site_idx = 0
                    station_site_key = list(station_warehouse._ordering.keys())[station_site_idx]
                    station_location = station_warehouse.child_locations[station_site_key]
                    station_warehouse.assign_child_resource(carrier, location=station_location, spot=station_site_idx)
                    logger.info(f"✓ 已绑定载具 {carrier.name} 到{cell_name}")
                except Exception as e:
                    logger.warning(f"绑定载具到{cell_name}失败（不影响硬件操作）: {e}")
            
            logger.info(f"完整流程完成：从原始电极取料 -> 放到{cell_name}")
            
            self._sync_resource_to_frontend()
        finally:
            # 确保释放锁
            self._3axis_lock.release()
            logger.info("已释放三轴操作锁")
        
        return {
            "electrolytic_cell_id": target_cell_id,
            "electrolytic_cell_name": cell_name,
            "pick_code": pick_code,
            "message": f"从原始电极取料并放到{cell_name}完成",
            "unilabos_samples": [LabSample(sample_uuid=sample_uuid, oss_path="", extra={"electrolytic_cell_id": target_cell_id, "pick_code": pick_code} if isinstance(content, str) else content.serialize()) for sample_uuid, content in (sample_uuids.items() if sample_uuids else {})]
        }
    
    def trigger_3axis_pick_from_electrolytic_cell_and_place_to_finished(
        self,
        electrolytic_cell_id: int,
        cleaning_time: int,
        nitrogen_time: int,
        place_code: int,
        sample_uuids: SampleUUIDsType = None,
    ) -> dict:
        """
        从电解池1或2取料，夹住到水洗池，动作完成，放到完成电极
        使用进程锁保证同一时间只有一个三轴操作
        
        流程：
        1. 检查电解池是否加工完成
        2. 如果未完成，释放进程锁并等待
        3. 获取进程锁
        4. 从指定的电解池（搅拌仪）取料
        5. 等待动作完成
        6. 夹住到水洗池
        7. 等待动作完成
        8. 放到完成电极
        9. 等待动作完成
        
        Args:
            electrolytic_cell_id: 电解池ID（1或2）
                - 1: 电解池1（对应搅拌仪1，位置3）
                - 2: 电解池2（对应搅拌仪2，位置2）
            cleaning_time: 水洗时间设置（对应Cleaning_Timeset）
            nitrogen_time: 氮气时间设置（对应N2_Timeset）
            place_code: 目标取放料代码（对应3-Axis_Target_Pick_&_Place_Code，用于放料）
        
        Returns:
            dict: 包含 message 和相关信息
        """
        # 校验电解池ID范围
        if electrolytic_cell_id not in (1, 2):
            error_msg = f"电解池ID必须在 1-2 范围内，当前值: {electrolytic_cell_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # 电解池映射：ID -> (位置代码, 完成节点, 名称)
        electrolytic_cell_map = {
            1: (3, "Electrolytic_Cell_1_Done", "电解池1（搅拌仪1）"),
            2: (2, "Electrolytic_Cell_2_Done", "电解池2（搅拌仪2）"),
        }
        target_position, done_node, cell_name = electrolytic_cell_map[electrolytic_cell_id]
        
        # 获取仓库资源
        station_warehouse = self.deck.warehouses[f"搅拌仪{electrolytic_cell_id}"]
        water_wash_warehouse = self.deck.warehouses["水洗池"]
        finished_warehouse = self.deck.warehouses["完成电极堆栈"]
        finished_site_key = str(place_code)
        
        logger.info(f"开始流程：从{cell_name}取料 -> 夹住到水洗池 -> 放到完成电极")
        
        # 使用线程锁保证同一时间只有一个三轴操作
        while True:
            # 先检查电解池是否加工完成
            logger.info(f"检查{cell_name}是否加工完成...")
            cell_done = self.get_node_value(done_node)
            if not cell_done:
                # 没有完成，不获取锁，等待完成
                logger.info(f"{cell_name}加工未完成，等待完成...")
                time.sleep(1.0)
                continue
            
            # 加工完成，获取锁
            logger.info(f"{cell_name}加工已完成，尝试获取三轴操作锁...")
            self._3axis_lock.acquire()
            logger.info("已获取三轴操作锁")
            
            try:
                # 再次确认电解池加工完成（防止在等待过程中状态变化）
                cell_done = self.get_node_value(done_node)
                if not cell_done:
                    # 状态变化，释放锁并重新等待
                    logger.info(f"{cell_name}状态变化，释放锁并重新等待...")
                    self._3axis_lock.release()
                    logger.info("已释放三轴操作锁")
                    time.sleep(1.0)
                    continue
                
                # 确认完成，继续执行
                break
            except Exception as e:
                # 如果出现异常，确保释放锁
                self._3axis_lock.release()
                logger.error(f"检查电解池状态时出错，已释放锁: {e}")
                raise
        
        try:
            # 获取搅拌仪的载具（与 AI4M place_beaker 一致：使用 sites 列表索引）
            station_site_idx = 0
            try:
                carrier = station_warehouse.sites[station_site_idx] if station_warehouse.sites else None
            except Exception:
                carrier = None
            
            # 步骤1：从电解池取料
            logger.info(f"步骤1：从{cell_name}取料...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置取料动作
            logger.info(f"设置三轴动作代码：1（取料），目标位置代码：{target_position}（{cell_name}）")
            self.set_node_value("3-Axis_Action_Code", 1)
            self.set_node_value("3-Axis_Target_Position_Code", target_position)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            self.set_node_value("3-Axis_Action_Trigger", False)
            time.sleep(1.0)
            
            # 触发动作
            logger.info("触发三轴取料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴取料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴取料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info(f"✓ 从{cell_name}取料完成")
            
            # 阶段1.5：三轴从电解池取料完成后，从搅拌仪解绑载具（无论载具类型，统一解绑转移）
            if carrier is not None:
                try:
                    station_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ 已从{cell_name}解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"从{cell_name}解绑载具失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()

            # 步骤2：夹住到水洗池
            logger.info(f"步骤2：夹住到水洗池（水洗时间：{cleaning_time}，氮气时间：{nitrogen_time}）...")
            
            # 检查水洗池是否空闲
            logger.info("检查水洗池是否空闲...")
            occupancy = self.get_node_value("Cleaning_Tank_Occupancy")
            while occupancy:
                logger.info("水洗池忙碌中，等待空闲...")
                time.sleep(1.0)
                occupancy = self.get_node_value("Cleaning_Tank_Occupancy")
            logger.info("水洗池已空闲")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 下发水洗时间设置和氮气时间设置
            logger.info(f"设置水洗时间：{cleaning_time}，氮气时间：{nitrogen_time}")
            self.set_node_value("Cleaning_Timeset", cleaning_time)
            self.set_node_value("N2_Timeset", nitrogen_time)
            time.sleep(1.0)
            
            # 设置夹住动作
            logger.info("设置三轴动作代码：3（夹住），目标位置代码：5（水洗池）")
            self.set_node_value("3-Axis_Action_Code", 3)
            self.set_node_value("3-Axis_Target_Position_Code", 5)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 【触发时】物料转移：绑定载具到水洗池
            if carrier is not None:
                try:
                    water_site_idx = 0
                    water_site_key = list(water_wash_warehouse._ordering.keys())[water_site_idx]
                    water_location = water_wash_warehouse.child_locations[water_site_key]
                    water_wash_warehouse.assign_child_resource(carrier, location=water_location, spot=water_site_idx)
                    logger.info(f"✓ [触发时] 已绑定载具 {carrier.name} 到水洗池")
                except Exception as e:
                    logger.warning(f"[触发时] 绑定载具到水洗池失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()
            
            # 触发动作
            logger.info("触发三轴夹住动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴夹住动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴夹住动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 夹住到水洗池完成")
            
            # 步骤3：放到完成电极
            logger.info(f"步骤3：放到完成电极（取放料代码：{place_code}）...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置放料动作
            logger.info(f"设置三轴动作代码：2（放料），目标位置代码：6（完成电极），取放料代码：{place_code}")
            self.set_node_value("3-Axis_Action_Code", 2)
            self.set_node_value("3-Axis_Target_Position_Code", 6)
            self.set_node_value("3-Axis_Target_Pick_&_Place_Code", place_code)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 【触发时】物料转移：从水洗池解绑，绑定到完成电极堆栈
            if carrier is not None:
                try:
                    water_wash_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ [触发时] 已从水洗池解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"[触发时] 从水洗池解绑载具失败（不影响硬件操作）: {e}")
                try:
                    finished_site_idx = list(finished_warehouse._ordering.keys()).index(finished_site_key)
                    finished_location = finished_warehouse.child_locations[finished_site_key]
                    finished_warehouse.assign_child_resource(carrier, location=finished_location, spot=finished_site_idx)
                    logger.info(f"✓ [触发时] 已绑定载具 {carrier.name} 到完成电极堆栈 {finished_site_key}")
                except Exception as e:
                    logger.warning(f"[触发时] 绑定载具到完成电极堆栈失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()
            
            # 触发动作
            logger.info("触发三轴放料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴放料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴放料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 放到完成电极完成")
            
            logger.info(f"完整流程完成：从{cell_name}取料 -> 夹住到水洗池 -> 放到完成电极")
            
            self._sync_resource_to_frontend()
        finally:
            # 确保释放锁
            self._3axis_lock.release()
            logger.info("已释放三轴操作锁")
        
        return {
            "electrolytic_cell_id": electrolytic_cell_id,
            "electrolytic_cell_name": cell_name,
            "cleaning_time": cleaning_time,
            "nitrogen_time": nitrogen_time,
            "place_code": place_code,
            "message": f"从{cell_name}取料，夹住到水洗池，放到完成电极完成",
            "unilabos_samples": [LabSample(sample_uuid=sample_uuid, oss_path="", extra={"electrolytic_cell_id": electrolytic_cell_id, "cleaning_time": cleaning_time, "nitrogen_time": nitrogen_time, "place_code": place_code} if isinstance(content, str) else content.serialize()) for sample_uuid, content in (sample_uuids.items() if sample_uuids else {})]
        }
    
    def trigger_3axis_pick_from_raw_and_process_to_finished(
        self,
        pick_code: int,
        pickling_time: int,
        cleaning_time: int,
        nitrogen_time: int,
        place_code: int,
        sample_uuids: SampleUUIDsType = None,
    ) -> dict:
        """
        从原始电极取料，动作完成，夹住到酸洗池，动作完成，夹住到水洗池，动作完成，放到完成电极
        使用进程锁保证同一时间只有一个三轴操作
        
        流程：
        1. 获取进程锁
        2. 从原始电极仓库取料
        3. 等待动作完成
        4. 夹住到酸洗池
        5. 等待动作完成
        6. 夹住到水洗池
        7. 等待动作完成
        8. 放到完成电极
        9. 等待动作完成
        
        Args:
            pick_code: 取放料代码（对应3-Axis_Target_Pick_&_Place_Code，用于取料）
            pickling_time: 酸洗时间设置（对应Pickling_Timeset）
            cleaning_time: 水洗时间设置（对应Cleaning_Timeset）
            nitrogen_time: 氮气时间设置（对应N2_Timeset）
            place_code: 取放料代码（对应3-Axis_Target_Pick_&_Place_Code，用于放料）
        
        Returns:
            dict: 包含 message 和相关信息
        """
        logger.info("开始流程：从原始电极取料 -> 夹住到酸洗池 -> 夹住到水洗池 -> 放到完成电极")
        
        # 获取仓库资源
        raw_warehouse = self.deck.warehouses["原始电极堆栈"]
        acid_warehouse = self.deck.warehouses["酸洗池"]
        water_wash_warehouse = self.deck.warehouses["水洗池"]
        finished_warehouse = self.deck.warehouses["完成电极堆栈"]
        raw_site_key = str(pick_code)
        finished_site_key = str(place_code)
        
        # 尝试获取载具（物料转移失败不终止硬件执行）
        try:
            carrier = raw_warehouse[raw_site_key]
        except Exception as e:
            logger.warning(f"获取原始电极堆栈位置 {raw_site_key} 载具失败（不影响硬件操作）: {e}")
            carrier = None
        if carrier is None:
            logger.warning(f"原始电极堆栈位置 {raw_site_key} 没有载具，将跳过物料转移，硬件照常执行")
        
        # 使用线程锁保证同一时间只有一个三轴操作
        self._3axis_lock.acquire()
        logger.info("已获取三轴操作锁")
        
        try:
            # 步骤1：从原始电极取料
            logger.info(f"步骤1：从原始电极仓库取料（取放料代码：{pick_code}）...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置取料动作
            logger.info(f"设置三轴动作代码：1（取料），目标位置代码：1（原始电极仓库），取放料代码：{pick_code}")
            self.set_node_value("3-Axis_Action_Code", 1)
            self.set_node_value("3-Axis_Target_Position_Code", 1)
            self.set_node_value("3-Axis_Target_Pick_&_Place_Code", pick_code)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            self.set_node_value("3-Axis_Action_Trigger", False)
            time.sleep(1.0)
            
            # 触发动作
            logger.info("触发三轴取料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴取料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴取料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 从原始电极仓库取料完成")
            
            # 阶段1.5：三轴取料完成后，从原始电极堆栈解绑载具
            try:
                raw_warehouse.unassign_child_resource(carrier)
                logger.info(f"✓ 已从原始电极堆栈解绑载具 {carrier.name}")
            except Exception as e:
                logger.warning(f"从原始电极堆栈解绑载具失败（不影响硬件操作）: {e}")
            self._sync_resource_to_frontend()

            # 步骤2：夹住到酸洗池
            logger.info(f"步骤2：夹住到酸洗池（酸洗时间：{pickling_time}）...")
            
            # 检查酸洗池是否空闲
            logger.info("检查酸洗池是否空闲...")
            occupancy = self.get_node_value("Pickling_Tank_Occupancy")
            while occupancy:
                logger.info("酸洗池忙碌中，等待空闲...")
                time.sleep(1.0)
                occupancy = self.get_node_value("Pickling_Tank_Occupancy")
            logger.info("酸洗池已空闲")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 下发酸洗时间设置
            logger.info(f"设置酸洗时间：{pickling_time}")
            self.set_node_value("Pickling_Timeset", pickling_time)
            time.sleep(1.0)
            
            # 设置夹住动作
            logger.info("设置三轴动作代码：3（夹住），目标位置代码：4（酸洗池）")
            self.set_node_value("3-Axis_Action_Code", 3)
            self.set_node_value("3-Axis_Target_Position_Code", 4)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 【触发时】物料转移：绑定载具到酸洗池
            if carrier is not None:
                try:
                    acid_site_idx = 0
                    acid_site_key = list(acid_warehouse._ordering.keys())[acid_site_idx]
                    acid_location = acid_warehouse.child_locations[acid_site_key]
                    acid_warehouse.assign_child_resource(carrier, location=acid_location, spot=acid_site_idx)
                    logger.info(f"✓ [触发时] 已绑定载具 {carrier.name} 到酸洗池")
                except Exception as e:
                    logger.warning(f"[触发时] 绑定载具到酸洗池失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()
            
            # 触发动作
            logger.info("触发三轴夹住动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴夹住动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴夹住动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 夹住到酸洗池完成")
            
            # 步骤3：夹住到水洗池
            logger.info(f"步骤3：夹住到水洗池（水洗时间：{cleaning_time}，氮气时间：{nitrogen_time}）...")
            
            # 检查水洗池是否空闲
            logger.info("检查水洗池是否空闲...")
            occupancy = self.get_node_value("Cleaning_Tank_Occupancy")
            while occupancy:
                logger.info("水洗池忙碌中，等待空闲...")
                time.sleep(1.0)
                occupancy = self.get_node_value("Cleaning_Tank_Occupancy")
            logger.info("水洗池已空闲")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 下发水洗时间设置和氮气时间设置
            logger.info(f"设置水洗时间：{cleaning_time}，氮气时间：{nitrogen_time}")
            self.set_node_value("Cleaning_Timeset", cleaning_time)
            self.set_node_value("N2_Timeset", nitrogen_time)
            time.sleep(1.0)
            
            # 设置夹住动作
            logger.info("设置三轴动作代码：3（夹住），目标位置代码：5（水洗池）")
            self.set_node_value("3-Axis_Action_Code", 3)
            self.set_node_value("3-Axis_Target_Position_Code", 5)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 【触发时】物料转移：从酸洗池解绑，绑定到水洗池
            if carrier is not None:
                try:
                    acid_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ [触发时] 已从酸洗池解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"[触发时] 从酸洗池解绑载具失败（不影响硬件操作）: {e}")
                try:
                    water_site_idx = 0
                    water_site_key = list(water_wash_warehouse._ordering.keys())[water_site_idx]
                    water_location = water_wash_warehouse.child_locations[water_site_key]
                    water_wash_warehouse.assign_child_resource(carrier, location=water_location, spot=water_site_idx)
                    logger.info(f"✓ [触发时] 已绑定载具 {carrier.name} 到水洗池")
                except Exception as e:
                    logger.warning(f"[触发时] 绑定载具到水洗池失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()
            
            # 触发动作
            logger.info("触发三轴夹住动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴夹住动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴夹住动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 夹住到水洗池完成")
            
            # 步骤4：放到完成电极
            logger.info(f"步骤4：放到完成电极（取放料代码：{place_code}）...")
            
            # 等待三轴空闲
            logger.info("等待三轴空闲...")
            axis_idle = self.get_node_value("3-Axis_Idle")
            while not axis_idle:
                logger.info("三轴忙碌中，等待空闲...")
                time.sleep(1.0)
                axis_idle = self.get_node_value("3-Axis_Idle")
            logger.info("三轴已空闲")
            
            # 检查是否有故障
            axis_fault = self.get_node_value("3-Axis_Fault")
            if axis_fault:
                error_msg = "三轴存在故障，无法执行动作"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 设置放料动作
            logger.info(f"设置三轴动作代码：2（放料），目标位置代码：6（完成电极），取放料代码：{place_code}")
            self.set_node_value("3-Axis_Action_Code", 2)
            self.set_node_value("3-Axis_Target_Position_Code", 6)
            self.set_node_value("3-Axis_Target_Pick_&_Place_Code", place_code)
            time.sleep(1.0)
            
            # 复位动作完成标志
            self.set_node_value("3-Axis_Action_Done", False)
            time.sleep(1.0)
            
            # 【触发时】物料转移：从水洗池解绑，绑定到完成电极堆栈
            if carrier is not None:
                try:
                    water_wash_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ [触发时] 已从水洗池解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"[触发时] 从水洗池解绑载具失败（不影响硬件操作）: {e}")
                try:
                    finished_site_idx = list(finished_warehouse._ordering.keys()).index(finished_site_key)
                    finished_location = finished_warehouse.child_locations[finished_site_key]
                    finished_warehouse.assign_child_resource(carrier, location=finished_location, spot=finished_site_idx)
                    logger.info(f"✓ [触发时] 已绑定载具 {carrier.name} 到完成电极堆栈 {finished_site_key}")
                except Exception as e:
                    logger.warning(f"[触发时] 绑定载具到完成电极堆栈失败（不影响硬件操作）: {e}")
                self._sync_resource_to_frontend()
            
            # 触发动作
            logger.info("触发三轴放料动作...")
            self.set_node_value("3-Axis_Action_Trigger", True)
            time.sleep(1.0)
            
            # 等待动作完成
            logger.info("等待三轴放料动作完成...")
            action_done = self.get_node_value("3-Axis_Action_Done")
            while not action_done:
                logger.info("三轴放料动作执行中...")
                time.sleep(1.0)
                action_done = self.get_node_value("3-Axis_Action_Done")
                
                # 检查是否有故障
                axis_fault = self.get_node_value("3-Axis_Fault")
                if axis_fault:
                    error_msg = "三轴动作执行过程中出现故障"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # 检查动作参数错误
                param_error = self.get_node_value("Action_Parameter_Error")
                if param_error:
                    error_msg = "三轴动作参数错误"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 复位动作触发和动作完成
            self.set_node_value("3-Axis_Action_Trigger", False)
            self.set_node_value("3-Axis_Action_Done", False)
            logger.info("✓ 放到完成电极完成")
            
            logger.info("完整流程完成：从原始电极取料 -> 夹住到酸洗池 -> 夹住到水洗池 -> 放到完成电极")
            
            self._sync_resource_to_frontend()
        finally:
            # 确保释放锁
            self._3axis_lock.release()
            logger.info("已释放三轴操作锁")
        
        return {
            "pick_code": pick_code,
            "pickling_time": pickling_time,
            "cleaning_time": cleaning_time,
            "nitrogen_time": nitrogen_time,
            "place_code": place_code,
            "message": "从原始电极取料，夹住到酸洗池，夹住到水洗池，放到完成电极完成",
            "unilabos_samples": [LabSample(sample_uuid=sample_uuid, oss_path="", extra={"pick_code": pick_code, "pickling_time": pickling_time, "cleaning_time": cleaning_time, "nitrogen_time": nitrogen_time, "place_code": place_code} if isinstance(content, str) else content.serialize()) for sample_uuid, content in (sample_uuids.items() if sample_uuids else {})]
        }
    
    def set_stirrer_params(
        self,
        station_id: int,
        stir_speed: int,
        heat_temp: int,
        time_set: int,
        sample_uuids: SampleUUIDsType = None,
    ) -> dict:
        """
        设置搅拌仪参数
        
        搅拌仪站号映射：
        - 站号1：对应csv中的c4（搅拌仪1）
        - 站号2：对应csv中的c3（搅拌仪2）
        
        Args:
            station_id: 搅拌仪站号（1-2）
            stir_speed: 搅拌速度
            heat_temp: 加热温度
            time_set: 时间设置
        
        Returns:
            dict: 包含 message
        """
        # 校验站号范围
        if station_id not in (1, 2):
            error_msg = f"搅拌仪站号必须在 1-2 范围内，当前值: {station_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # 站号到csv索引的映射：站号1->c4(索引4)，站号2->c3(索引3)
        csv_index_map = {
            1: 3,  # 站号1对应c3（搅拌仪1）
            2: 4,  # 站号2对应c4（搅拌仪2）
        }
        csv_index = csv_index_map[station_id]
        station_name_map = {
            1: "搅拌仪1",
            2: "搅拌仪2",
        }
        station_name = station_name_map[station_id]
        
        logger.info(f"开始设置{station_name}（站号{station_id}，csv索引c{csv_index}）参数：搅拌速度={stir_speed}，加热温度={heat_temp}，时间设置={time_set}")
        
        # 确定电解池参数已下发和已执行的节点名称
        param_downloaded_node = f"Electrolytic_Cell_{station_id}_param_downloaded"
        params_received_node = f"Electrolytic_Cell_{station_id}_params_received"
        
        # 1. 先将"电解池参数已下发"置位为 false
        logger.info(f"复位{station_name}参数已下发标志...")
        self.set_node_value(param_downloaded_node, False)
        
        # 2. 下发搅拌仪参数
        logger.info(f"设置{station_name}参数：搅拌速度={stir_speed}，加热温度={heat_temp}，时间设置={time_set}")
        self.set_node_value(f"mag_stirrer_c{csv_index}_stir_speed", stir_speed)
        self.set_node_value(f"mag_stirrer_c{csv_index}_heat_temp", heat_temp)
        self.set_node_value(f"mag_stirrer_c{csv_index}_time_set", time_set)
        time.sleep(1.0)
        
        # 3. 将"已下发"置位为 true
        logger.info(f"设置{station_name}参数已下发标志为 true...")
        self.set_node_value(param_downloaded_node, True)
        
        # 4. 等待电解池参数执行完成
        logger.info(f"等待{station_name}参数执行完成...")
        params_received = self.get_node_value(params_received_node)
        while not params_received:
            logger.info(f"{station_name}参数执行中...")
            time.sleep(1.0)
            params_received = self.get_node_value(params_received_node)
        logger.info(f"{station_name}参数执行完成")
        
        # 5. 将"已下发"置位为 false
        logger.info(f"复位{station_name}参数已下发标志...")
        self.set_node_value(param_downloaded_node, False)
        
        logger.info(f"{station_name}参数设置完成")
        
        return {
            "station_id": station_id,
            "station_name": station_name,
            "stir_speed": stir_speed,
            "heat_temp": heat_temp,
            "time_set": time_set,
            "message": f"{station_name}参数设置完成：搅拌速度={stir_speed}，加热温度={heat_temp}，时间设置={time_set}",
            "unilabos_samples": [LabSample(sample_uuid=sample_uuid, oss_path="", extra={"station_id": station_id, "stir_speed": stir_speed, "heat_temp": heat_temp, "time_set": time_set} if isinstance(content, str) else content.serialize()) for sample_uuid, content in (sample_uuids.items() if sample_uuids else {})]
        }

    def trigger_electrolytic_cell_bts_reaction(
        self,
        electrolytic_cell_id: int,
        sample_uuids: SampleUUIDsType = None,
    ) -> dict:
        """
        触发电解池BTS反应

        对应电解池有请求加工信号时，触发BTS反应，并通过OPC UA信号与PLC同步加工流程。

        流程：
        1. 检查电解池是否有请求加工信号
        2. 触发BTS反应（调用 bts_start_cp_test）
        3. 将开始加工信号和加工完成信号置0
        4. Sleep 1s 后将开始加工信号置1
        5. 等待加工完成信号为1时，将开始加工信号置0

        Args:
            electrolytic_cell_id: 电解池编号（1或2）
            sample_uuids: 样品UUID，可为空

        Returns:
            dict: 包含 electrolytic_cell_id 和 message
        """
        if electrolytic_cell_id not in (1, 2):
            error_msg = f"电解池编号必须为1或2，当前值: {electrolytic_cell_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        request_node = f"Electrolytic_Cell_{electrolytic_cell_id}_Request"
        start_node = f"Electrolytic_Cell_{electrolytic_cell_id}_Start"
        done_node = f"Electrolytic_Cell_{electrolytic_cell_id}_Done"
        cell_name = f"电解池{electrolytic_cell_id}"

        logger.info(f"开始触发{cell_name} BTS反应...")

        # 检查是否有请求加工信号
        request_signal = self.get_node_value(request_node)
        if not request_signal:
            error_msg = f"{cell_name}无请求加工信号，无法触发BTS反应"
            logger.error(error_msg)
            raise ValueError(error_msg)

        logger.info(f"{cell_name}有请求加工信号，触发BTS反应...")

        # 触发BTS反应，同时将开始加工信号和加工完成信号置0
        bts_thread = threading.Thread(
            target=self.bts_start_cp_test,
            kwargs={"chl_list": [electrolytic_cell_id]},
            daemon=True,
        )
        bts_thread.start()
        self.set_node_value(start_node, False)
        self.set_node_value(done_node, False)
        logger.info(f"{cell_name}开始加工信号和加工完成信号已置0")

        # sleep 1s 后将开始加工信号置1
        time.sleep(1.0)
        logger.info(f"设置{cell_name}开始加工信号为True...")
        self.set_node_value(start_node, True)

        # 等待加工完成信号为1
        logger.info(f"等待{cell_name}加工完成...")
        done = self.get_node_value(done_node)
        while not done:
            logger.info(f"{cell_name}加工中...")
            time.sleep(1.0)
            done = self.get_node_value(done_node)

        # 将开始加工信号置0
        logger.info(f"{cell_name}加工完成，复位开始加工信号...")
        self.set_node_value(start_node, False)

        logger.info(f"{cell_name} BTS反应流程完成")

        return {
            "electrolytic_cell_id": electrolytic_cell_id,
            "message": f"{cell_name} BTS反应完成",
            "unilabos_samples": [
                LabSample(
                    sample_uuid=sample_uuid,
                    oss_path="",
                    extra={"electrolytic_cell_id": electrolytic_cell_id},
                )
                for sample_uuid, content in (sample_uuids.items() if sample_uuids else {})
            ],
        }

    # ==================== BTS HTTP API 驱动（CP计时电位法） ====================

    def bts_start_cp_test(
            self,
            chl_list: List[int],
            duration_sec: int = 20,  # 秒，内部转为 ms 传给 API
            current: float = 50.0,  # mA
            dev_uuid: Optional[str] = None,
        ) -> dict:
            """BTS 启动 CP 计时电位法测试（内部自动执行校验、获取设备信息、获取通道状态）"""
            # 1. 校验
            url = f"{self._bts_base_url}/api/bts/validate"
            payload = {"cmd-type": 1, "request-id": f"validate-{int(time.time())}", "data": {"check-id": self._bts_validate_code}}
            response = self._bts_session.post(url, json=payload)
            self._bts_validated = response.status_code == 200
            logger.info(f"BTS 校验: 状态码={response.status_code}, 响应={response.text}")
            if not self._bts_validated:
                return {"success": False, "message": "BTS校验失败", "test_id": None, "response": response.text}
            # 2. 获取设备信息
            url = f"{self._bts_base_url}/api/bts/device/info"
            payload = {"cmd-type": 2, "request-id": f"device-info-{int(time.time())}"}
            response = self._bts_session.get(url, json=payload)
            if response.status_code != 200:
                return {"success": False, "message": f"获取设备信息失败: {response.text}", "test_id": None}
            devices = response.json().get("data", {}).get("dev-list", [])
            logger.info(f"BTS 设备信息: 发现 {len(devices)} 个设备")
            if not devices:
                return {"success": False, "message": "未发现可用设备", "test_id": None}
            # 单设备时自动使用，多设备时需传入 dev_uuid
            if dev_uuid is None:
                if len(devices) > 1:
                    return {"success": False, "message": "多设备时需指定 dev_uuid", "test_id": None}
                dev_uuid = devices[0]["dev-uuid"]
            # 3. 获取通道状态
            url = f"{self._bts_base_url}/api/bts/test/state"
            payload = {"cmd-type": 5, "request-id": f"channel-state-{int(time.time())}", "data": [{"dev-uuid": dev_uuid, "chl-list": chl_list}]}
            response = self._bts_session.post(url, json=payload)
            logger.info(f"BTS 通道状态: 状态码={response.status_code}, 响应={response.text}")
            url = f"{self._bts_base_url}/api/bts/test/start"
            test_id = f"test-cp-{int(time.time())}"
            payload = {
                "cmd-type": 3,
                "request-id": f"start-test-{int(time.time())}",
                "data": {
                    "test-id": test_id,
                    "dev-ip": dev_uuid,
                    "chl-list": chl_list,
                    "globalProtect": {
                        "voltageProtect": {
                            "underVoltage": 0,
                            "overVoltage": 5,
                            "enableUnderVoltage": True,
                            "enableOverVoltage": True,
                            "enableRangeProtect": False,
                            "delayTime": 0,
                            "enableDelay": False
                        },
                        "currentProtect": {
                            "charge": 5000,
                            "discharge": 5000,
                            "enableCharge": True,
                            "enableDischarge": True,
                            "enableRangeProtect": False
                        }
                    },
                    "globalRecordCondi": {
                        "electricCurrent": 0,
                        "enable_electricCurrent": False,
                        "enable_time": True,
                        "enable_voltage": False,
                        "time": 1000,
                        "voltage": 0
                    },
                    "batteryInfo": {
                        "creator": "test-user",
                        "weight": 100,
                        "batteryBatchNum": "",
                        "currentUpperLimit": 5000,
                        "voltageUpperLimit": 5,
                        "voltageLowerLimit": 0
                    },
                    "stepList": [
                        {
                            "type": 21,
                            "pType": 0,
                            "mode": 1,
                            "mPara": current,
                            "rateMode": False,
                            "rateValue": 0,
                            "recordCondi": {
                                "enable_time": True,
                                "time": 1000,
                                "enable_voltage": False,
                                "voltage": 0
                            },
                            "endCondi": [
                                {
                                    "also": True,
                                    "rateMode": False,
                                    "rateModeType": 0,
                                    "rateValue": 0,
                                    "relation": 1,
                                    "type": 3,
                                    "userCustomVariable-arithmetic": 0,
                                    "userCustomVariable-isVariable": False,
                                    "userCustomVariable-value": 0,
                                    "userCustomVariable-value2": 0,
                                    "userCustomVariable-variableParam": 0,
                                    "userCustomVariable-variableParam2": 0,
                                    "value": duration_sec * 1000
                                }
                            ]
                        }
                    ]
                }
            }
            response = self._bts_session.post(url, json=payload)
            success = response.status_code == 200
            logger.info(f"BTS 启动 CP 测试: 状态码={response.status_code}, 响应={response.text}")
            if success:
                timeout_sec = duration_sec + 10
                state_url = f"{self._bts_base_url}/api/bts/test/state"
                start = time.time()
                time.sleep(5)  # 先等测试真正启动，避免拿到启动前的 state=0 误判
                while True:
                    if time.time() - start > timeout_sec:
                        return {"success": False, "message": "BTS故障", "test_id": test_id, "response": "超时未检测到通道状态为0"}
                    state_payload = {"cmd-type": 5, "request-id": f"channel-state-{int(time.time())}", "data": [{"dev-uuid": dev_uuid, "chl-list": chl_list}]}
                    r = self._bts_session.post(state_url, json=state_payload)
                    if r.status_code == 200:
                        dev_info = r.json().get("data", {}).get("dev-info", [])
                        ch_states = {cs["chl"]: cs.get("state") for dev in dev_info for cs in dev.get("chl-state", []) if cs.get("chl") in chl_list}
                        logger.info(f"轮询通道状态: {ch_states}")
                        all_zero = all(ch_states.get(ch) == 0 for ch in chl_list)
                        if all_zero:
                            logger.info("通道状态已为0，测试完成，调用停止")
                            self.bts_stop_test(dev_uuid, chl_list)
                            break
                    time.sleep(5)
            return {"success": success, "test_id": test_id if success else None, "message": "启动CP测试成功" if success else "启动测试失败", "response": response.text}

    def bts_get_channel_state(self, chl_list: List[int], dev_uuid: Optional[str] = None) -> dict:
        """BTS 查询通道状态（单设备时 dev_uuid 可省略）"""
        if not self._bts_validated:
            url = f"{self._bts_base_url}/api/bts/validate"
            r = self._bts_session.post(url, json={"cmd-type": 1, "request-id": f"validate-{int(time.time())}", "data": {"check-id": self._bts_validate_code}})
            self._bts_validated = r.status_code == 200
            if not self._bts_validated:
                return {"success": False, "message": "BTS校验失败", "response": r.text}
        if dev_uuid is None:
            url = f"{self._bts_base_url}/api/bts/device/info"
            response = self._bts_session.get(url, json={"cmd-type": 2, "request-id": f"device-info-{int(time.time())}"})
            if response.status_code != 200:
                return {"success": False, "message": f"获取设备信息失败: {response.text}"}
            devices = response.json().get("data", {}).get("dev-list", [])
            if len(devices) != 1:
                return {"success": False, "message": "多设备时需指定 dev_uuid"}
            dev_uuid = devices[0]["dev-uuid"]
        url = f"{self._bts_base_url}/api/bts/test/state"
        payload = {"cmd-type": 5, "request-id": f"channel-state-{int(time.time())}", "data": [{"dev-uuid": dev_uuid, "chl-list": chl_list}]}
        response = self._bts_session.post(url, json=payload)
        logger.info(f"BTS 通道状态: 状态码={response.status_code}, 响应={response.text}")
        return {"success": response.status_code == 200, "response": response.text}

    def bts_stop_test(self, dev_uuid: str, chl_list: List[int]) -> dict:
        """BTS 停止测试"""
        if not self._bts_validated:
            return {"success": False, "message": "请先通过 bts_validate 校验"}
        url = f"{self._bts_base_url}/api/bts/test/stop"
        payload = {
            "cmd-type": 4,
            "request-id": f"stop-test-{int(time.time())}",
            "data": {"dev-ip": dev_uuid, "chl-list": chl_list}
        }
        response = self._bts_session.post(url, json=payload)
        success = response.status_code == 200
        logger.info(f"BTS 停止测试: 状态码={response.status_code}, 响应={response.text}")
        return {"success": success, "message": "停止测试成功" if success else "停止测试失败", "response": response.text}


if __name__ == '__main__':
    
    # 调试用法
    A4 = AI4M002Device(
        url="opc.tcp://127.0.0.1:49320",
        csv_path="opcua_nodes_AI4M_sim.csv"
    )
    
    
    # A4.trigger_init()
    # print("初始化完成")
    # A4.bts_start_cp_test(chl_list=[2], duration_sec=10, current=50.0)
    # print("CP测试完成")

    result = A4.trigger_electrolytic_cell_bts_reaction(electrolytic_cell_id=1)
    print(f"电解池BTS反应完成: {result}")

    # # 给水凝胶堆栈A1位置添加clean物料
    # rack_warehouse = A4.deck.warehouses["水凝胶烧杯堆栈"]
    # clean_carrier = Hydrogel_Clean_1BottleCarrier("烧杯")
    
    # # 获取A1位置的索引和位置信息
    # rack_site_key = "A1"
    # rack_site_idx = list(rack_warehouse._ordering.keys()).index(rack_site_key)
    # rack_location = rack_warehouse.child_locations[rack_site_key]
    
    # # 将载具分配到A1位置
    # rack_warehouse.assign_child_resource(clean_carrier, location=rack_location, spot=rack_site_idx)
    # print(f"✓ 已添加clean物料到A1位置: {clean_carrier.name}")
    
    # pick_result = A4.trigger_robot_pick_beaker(1, 1)
    # print("取烧杯完成")
    
    # A4.trigger_robot_place_beaker(pick_result['pick_beaker_id'], pick_result['place_station_id'])
    # print("放烧杯完成")
    
    # while True:
    #     time.sleep(1)
