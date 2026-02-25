"""
AI4M 设备驱动
继承自 OPC UA 通讯基类，实现具体的设备动作函数
"""

import json
import time
import traceback
from typing import Optional
import os
import threading

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.log import logger
from unilabos.utils.decorator import not_action
from unilabos.devices.workstation.AI4M.decks import AI4M_deck
from unilabos.devices.workstation.AI4M.bottle_carriers import Hydrogel_Clean_1BottleCarrier

# 导入通讯基类
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription


class AI4MDevice(OpcUaClientWithSubscription):
    """
    AI4M 设备类
    继承自 OpcUaClientWithSubscription，实现具体的设备动作函数
    """
    
    def __init__(
        self, 
        url: str, 
        deck: Optional[AI4M_deck] = None,
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
            self.deck = AI4M_deck(setup=True)
        else:
            self.deck = deck.get("data") if isinstance(deck, dict) else deck

        if self.deck is None:
            raise ValueError("Deck 配置不能为空")

        # 创建机器人操作的线程锁，防止 pick 和 place 同时执行
        self._robot_lock = threading.Lock()
        logger.info("✓ 机器人操作线程锁已初始化")

        # 统计仓库信息
        warehouse_count = 0
        if hasattr(self.deck, 'children'):
            warehouse_count = len(self.deck.children)
            logger.info(f"Deck 初始化完成，加载 {warehouse_count} 个资源")
        
        # 如果提供了 CSV 路径，则直接加载节点
        if csv_path:
            self.load_nodes_from_csv(csv_path)

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
    
    def start_manual_mode(self) -> dict:
        """
        指令作业模式函数：
        - 将模式切换、手自动切换写false
        - 等待自动模式为false
        - 将模式切换写true

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("启动指令作业模式...")

        # 将模式切换、手自动切换写true
        logger.info("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", True)
        self.set_node_value("manual_auto_switch", False)

        # 等待自动模式为false
        logger.info("等待自动模式为False...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            logger.info("等待自动模式变为False...")
            time.sleep(1.0)
            auto_mode = self.get_node_value("auto_mode")
        
        logger.info("模式切换完成")
        return {
            "message": "指令作业模式启动成功",
        }

    def trigger_robot_pick_beaker(
        self,
        pick_beaker_id: int,
        place_station_id: int = None,
    ) -> dict:
        """
        机器人取烧杯并放到检测位：
        - 使用线程锁保证同一时间只有一个机器人操作
        - 查询机器人空闲状态，循环等待直到机器人空闲
        - 如果未指定place_station_id，则查找空闲检测站，如果没有则等待后重试
        - 先写入取烧杯编号，等待取烧杯完成
        - 取完成后再写入放检测编号，等待对应的放检测完成信号
        
        Args:
            pick_beaker_id: 取烧杯编号（1-5）
            place_station_id: 放检测编号（1-3），如果为None则自动查找空闲检测站
            
        Returns:
            dict: 包含 success, pick_beaker_id, place_station_id, message
        """
        # 校验输入范围
        if pick_beaker_id not in (1, 2, 3, 4, 5):
            error_msg = f"取烧杯编号必须在 1-5 范围内，当前值: {pick_beaker_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        if place_station_id is not None and place_station_id not in (1, 2, 3):
            error_msg = f"放检测编号必须在 1-3 范围内，当前值: {place_station_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # 在获取锁之前，先检查是否有空闲检测站
        # 如果没有空闲检测站，不进行锁的争抢
        if place_station_id is None:
            # 如果未指定检测站，则查找空闲检测站
            place_station_id = None
            for station_id in (1, 2, 3):
                station_ready_node = f"station_{station_id}_ready"
                station_ready = self.get_node_value(station_ready_node)
                if station_ready:
                    place_station_id = station_id
                    logger.info(f"[机器人取烧杯{pick_beaker_id}] 找到空闲检测站{place_station_id}")
                    break
            
            if place_station_id is None:
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 没有空闲检测站，不进行锁的争抢，等待中...")
                # 循环等待直到找到空闲检测站
                while place_station_id is None:
                    for station_id in (1, 2, 3):
                        station_ready_node = f"station_{station_id}_ready"
                        station_ready = self.get_node_value(station_ready_node)
                        if station_ready:
                            place_station_id = station_id
                            logger.info(f"[机器人取烧杯{pick_beaker_id}] 找到空闲检测站{place_station_id}")
                            break
                    
                    if place_station_id is None:
                        logger.info(f"[机器人取烧杯{pick_beaker_id}] 没有空闲检测站，等待中...")
                        time.sleep(1.0)
        else:
            # 如果指定了检测站，检查该检测站是否空闲
            station_ready_node = f"station_{place_station_id}_ready"
            station_ready = self.get_node_value(station_ready_node)
            if not station_ready:
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站{place_station_id}不空闲，不进行锁的争抢，等待中...")
                # 循环等待直到检测站空闲
                while not station_ready:
                    logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站{place_station_id}忙碌中，等待空闲...")
                    time.sleep(1.0)
                    station_ready = self.get_node_value(station_ready_node)
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站{place_station_id}已空闲")

        # 使用线程锁保证同一时间只有一个机器人操作
        # 只有在确认有空闲检测站后，才进行锁的争抢
        # 注意：等待循环在获取锁之前，等待时不持有锁，让其他操作（如place）有机会执行
        # 一旦检测站空闲，立即尝试获取锁，但在获取锁之前再次确认检测站状态
        while True:
            # 再次确认检测站仍然空闲（防止在等待过程中检测站被占用）
            if place_station_id is None:
                # 重新查找空闲检测站
                place_station_id = None
                for station_id in (1, 2, 3):
                    station_ready_node = f"station_{station_id}_ready"
                    station_ready = self.get_node_value(station_ready_node)
                    if station_ready:
                        place_station_id = station_id
                        break
                if place_station_id is None:
                    logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站状态变化，重新等待空闲检测站...")
                    time.sleep(1.0)
                    continue
            else:
                # 确认指定的检测站仍然空闲
                station_ready_node = f"station_{place_station_id}_ready"
                station_ready = self.get_node_value(station_ready_node)
                if not station_ready:
                    logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站{place_station_id}状态变化，重新等待...")
                    time.sleep(1.0)
                    continue
            
            # 检测站空闲，尝试获取锁（阻塞方式，但等待循环在获取锁之前，所以不会阻塞其他操作）
            logger.info(f"[机器人取烧杯{pick_beaker_id}] 检测站{place_station_id}空闲，尝试获取机器人操作锁...")
            with self._robot_lock:
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 已获取机器人操作锁，开始执行")
                
                # 互锁1：循环等待直到机器人空闲
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 等待机器人空闲...")
                robot_ready = self.get_node_value("robot_ready")
                while not robot_ready:
                    logger.info(f"[机器人取烧杯{pick_beaker_id}] 机器人忙碌中，等待空闲...")
                    time.sleep(1.0)
                    robot_ready = self.get_node_value("robot_ready")
                logger.info(f"[机器人取烧杯{pick_beaker_id}] 机器人已空闲")
                
                # 再次确认检测站仍然空闲（防止在等待锁的过程中检测站被占用）
                station_ready_node = f"station_{place_station_id}_ready"
                station_ready = self.get_node_value(station_ready_node)
                if not station_ready:
                    error_msg = f"检测站{place_station_id}在获取锁后不再空闲，操作取消"
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                # 获取仓库资源
                rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
                station_warehouse = self.deck.warehouses[f"反应工站{place_station_id}"]
                rack_site_key = f"A{pick_beaker_id}"

                # 在执行硬件操作之前，先检查载具是否存在
                carrier = rack_warehouse[rack_site_key]
                if carrier is None:
                    error_msg = f"堆栈位置 {rack_site_key} 没有载具"
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                pick_complete_node = f"robot_rack_pick_beaker_{pick_beaker_id}_complete"
                place_complete_node = f"robot_place_station_{place_station_id}_complete"

                # 阶段1：下发取烧杯编号并等待完成
                logger.info("下发取烧杯编号，等待完成...")
                self.set_node_value("robot_pick_beaker_id", pick_beaker_id)
                
                # 等待取烧杯完成
                pick_complete = self.get_node_value(pick_complete_node)
                while not pick_complete:
                    logger.info("取烧杯中...")
                    time.sleep(5.0)
                    pick_complete = self.get_node_value(pick_complete_node)
                
                # 阶段1.5：机器人取烧杯完成后，从堆栈解绑载具
                try:
                    rack_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ 已从堆栈解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"从堆栈解绑载具失败（不影响硬件操作）: {e}")
                
                # 阶段2：取完成后再下发放检测编号并等待完成
                logger.info("取完成，开始下发放检测编号...")
                self.set_node_value("robot_place_station_id", place_station_id)
                
                # 等待放检测完成
                place_complete = self.get_node_value(place_complete_node)
                while not place_complete:
                    logger.info("放检测中...")
                    time.sleep(5.0)
                    place_complete = self.get_node_value(place_complete_node)
                
                # 阶段2.5：机器人放到检测站完成后，绑定载具到检测站
                try:
                    station_site_idx = 0
                    station_site_key = list(station_warehouse._ordering.keys())[station_site_idx]
                    station_location = station_warehouse.child_locations[station_site_key]
                    
                    station_warehouse.assign_child_resource(carrier, location=station_location, spot=station_site_idx)
                    logger.info(f"✓ 已绑定载具 {carrier.name} 到检测站{place_station_id}")
                except Exception as e:
                    logger.warning(f"绑定载具到检测站失败（不影响硬件操作）: {e}")
                
                logger.info("放检测完成")
                    
                # 更新资源树到前端
                if hasattr(self, '_ros_node') and self._ros_node:
                    try:
                        from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                        logger.info(f"✓ 已同步资源更新到前端")
                    except Exception as e:
                        logger.warning(f"前端资源更新失败: {e}")

                logger.info(f"[机器人取烧杯{pick_beaker_id}] 释放机器人操作锁")
                # 成功执行后退出外层循环
                break

        return {
            "pick_beaker_id": pick_beaker_id,
            "place_station_id": place_station_id,
            "message": f"机器人取烧杯{pick_beaker_id}并放到检测站{place_station_id}完成",
        }

    def trigger_robot_place_beaker(
        self,
        place_beaker_id: int,
        pick_station_id: int,
    ) -> dict:
        """
        机器人从检测位取烧杯并放回：
        - 使用线程锁保证同一时间只有一个机器人操作
        - 查询机器人空闲状态，循环等待直到机器人空闲
        - 先写入取检测编号，等待取检测完成
        - 取完成后再写入放烧杯编号，等待对应的放烧杯完成信号
        
        Args:
            place_beaker_id: 放烧杯编号（1-5）
            pick_station_id: 取检测编号（1-3）
            
        Returns:
            dict: 包含 success, place_beaker_id, pick_station_id, message
        """
        # 校验输入范围
        if place_beaker_id not in (1, 2, 3, 4, 5):
            error_msg = f"放烧杯编号必须在 1-5 范围内，当前值: {place_beaker_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        if pick_station_id not in (1, 2, 3):
            error_msg = f"取检测编号必须在 1-3 范围内，当前值: {pick_station_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # 获取仓库资源
        rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
        station_warehouse = self.deck.warehouses[f"反应工站{pick_station_id}"]
        station_site_idx = 0

        # 使用线程锁保证同一时间只有一个机器人操作
        logger.info(f"[机器人放烧杯{place_beaker_id}] 尝试获取机器人操作锁...")
        with self._robot_lock:
            logger.info(f"[机器人放烧杯{place_beaker_id}] 已获取机器人操作锁，开始执行")
            
            # 互锁：循环等待直到机器人空闲
            logger.info(f"[机器人放烧杯{place_beaker_id}] 等待机器人空闲...")
            robot_ready = self.get_node_value("robot_ready")
            while not robot_ready:
                logger.info(f"[机器人放烧杯{place_beaker_id}] 机器人忙碌中，等待空闲...")
                time.sleep(1.0)
                robot_ready = self.get_node_value("robot_ready")
            logger.info(f"[机器人放烧杯{place_beaker_id}] 机器人已空闲")

            # 获取检测站的载具（如果存在）
            try:
                carrier = station_warehouse.sites[station_site_idx] if station_warehouse.sites else None
            except Exception:
                carrier = None
            
            # 确定堆栈目标位置
            rack_site_key = f"C{place_beaker_id}"
            # 预先计算 rack_site_idx，即使不执行绑定操作也需要这个值
            try:
                rack_site_idx = list(rack_warehouse._ordering.keys()).index(rack_site_key)
            except Exception:
                rack_site_idx = None

            pick_complete_node = f"robot_pick_station_{pick_station_id}_complete"
            place_complete_node = f"robot_rack_place_beaker_{place_beaker_id}_complete"

            # 阶段1：下发取检测编号并等待完成
            logger.info("下发取检测编号，等待完成...")
            self.set_node_value("robot_pick_station_id", pick_station_id)
            
            # 等待取检测完成
            pick_complete = self.get_node_value(pick_complete_node)
            while not pick_complete:
                logger.info("取检测中...")
                time.sleep(5.0)
                pick_complete = self.get_node_value(pick_complete_node)
            
            # 阶段1.5：机器人取检测完成后，从检测站解绑载具
            if carrier is not None and type(carrier).__name__ != 'ResourceHolder':
                try:
                    station_warehouse.unassign_child_resource(carrier)
                    logger.info(f"✓ 已从检测站{pick_station_id}解绑载具 {carrier.name}")
                except Exception as e:
                    logger.warning(f"从检测站解绑载具失败（不影响硬件操作）: {e}")
            
            # 阶段2：取完成后再下发放烧杯编号并等待完成
            logger.info("取完成，开始下发放烧杯编号...")
            self.set_node_value("robot_place_beaker_id", place_beaker_id)
            
            # 等待放烧杯完成
            place_complete = self.get_node_value(place_complete_node)
            while not place_complete:
                logger.info("放烧杯中...")
                time.sleep(5.0)
                place_complete = self.get_node_value(place_complete_node)
            
            # 阶段2.5：机器人放烧杯完成后，绑定载具回堆栈
            if carrier is not None and type(carrier).__name__ != 'ResourceHolder' and rack_site_idx is not None:
                try:
                    rack_location = rack_warehouse.child_locations[rack_site_key]
                    
                    rack_warehouse.assign_child_resource(carrier, location=rack_location, spot=rack_site_idx)
                    logger.info(f"✓ 已绑定载具 {carrier.name} 回堆栈 {rack_site_key}")
                except Exception as e:
                    logger.warning(f"绑定载具回堆栈失败（不影响硬件操作）: {e}")
            
            logger.info("放烧杯完成")
            
            # 更新资源树到前端
            if hasattr(self, '_ros_node') and self._ros_node:
                try:
                    from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                    ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                    logger.info(f"✓ 已同步资源更新到前端")
                except Exception as e:
                    logger.warning(f"前端资源更新失败: {e}")

            logger.info(f"[机器人放烧杯{place_beaker_id}] 释放机器人操作锁")

        # !!样例：准备载具信息作为样本数据，记录保存样品数据
        carrier_info = {
            "name": carrier.name if carrier is not None else None,
            "type": "carrier",
            "rack_location": rack_site_key,
            "station_id": pick_station_id,
            "rack_site_index": rack_site_idx
        }

        return {
            "place_beaker_id": place_beaker_id,
            "pick_station_id": pick_station_id,
            "message": f"机器人从检测站{pick_station_id}取烧杯并放回位置{place_beaker_id}完成",
            # "unilabos_samples": [carrier_info],  # 使用 unilabos_samples 避免被外部服务转换
        }

    def trigger_station_process(
        self,
        station_id: int,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
    ) -> dict:
        """
        执行检测工艺流程：
        1. 等待检测站请求参数
        2. 下发对应编号的搅拌仪和注射泵参数
        3. 等待参数已执行
        4. 给出检测开始信号
        5. 等待检测工艺完成
        
        Args:
            station_id: 检测编号（1-3）
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            
        Returns:
            dict: 包含 success, station_id, message
        """
        # 校验输入范围
        if station_id not in (1, 2, 3):
            error_msg = f"检测编号必须在 1-3 范围内，当前值: {station_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # 检测站索引（0-2）
        station_idx = station_id - 1
        
        # 节点名称
        request_node = f"station_{station_id}_request_params"
        params_received_node = f"station_{station_id}_params_received"
        start_node = f"station_{station_id}_start"
        complete_node = f"station_{station_id}_process_complete"
        
        self.set_node_value(complete_node, False)
        self.set_node_value(start_node, False)
        self.set_node_value(params_received_node, False)

        # 阶段1：等待检测站请求参数
        logger.info(f"等待检测{station_id}请求参数...")
        request_params = self.get_node_value(request_node)
        while not request_params:
            logger.info(f"等待检测{station_id}请求参数中...")
            time.sleep(1.0)
            request_params = self.get_node_value(request_node)
        
        logger.info(f"检测{station_id}已请求参数，开始下发...")
        
        # 阶段2：下发对应编号的搅拌仪参数
        self.set_node_value(f"mag_stirrer_c{station_idx}_stir_speed", mag_stir_stir_speed)
        self.set_node_value(f"mag_stirrer_c{station_idx}_heat_temp", mag_stir_heat_temp)
        self.set_node_value(f"mag_stirrer_c{station_idx}_time_set", mag_stir_time_set)
        logger.info(f"已下发检测{station_id}磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")
        
        # 下发对应编号的注射泵参数
        self.set_node_value(f"syringe_pump_{station_idx}_abs_position_set", syringe_pump_abs_position_set)
        logger.info(f"已下发检测{station_id}注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        
        # 阶段3：等待参数已执行
        self.set_node_value(start_node, True)
        logger.info(f"等待检测{station_id}参数已执行...")
        params_received = self.get_node_value(params_received_node)
        while not params_received:
            logger.info(f"检测{station_id}参数执行中...")
            time.sleep(1.0)
            params_received = self.get_node_value(params_received_node)
        
        logger.info(f"检测{station_id}参数已执行")
           
        # 阶段4：等待检测工艺完成
        logger.info(f"等待检测{station_id}工艺完成...")
        process_complete = self.get_node_value(complete_node)
        while not process_complete:
            logger.info(f"检测{station_id}工艺执行中...")
            time.sleep(5.0)
            process_complete = self.get_node_value(complete_node)
        
        logger.info(f"检测{station_id}工艺完成")
        self.set_node_value(start_node, False)

        return {
            "station_id": station_id,
            "message": f"检测站{station_id}工艺执行完成",
        }

    def trigger_init(self) -> dict:
        """
        初始化函数：
        - 将手自动切换写false
        - 等待自动模式为false
        - 将初始化PC写true
        - 等待初始化完成PC为true
        - 将初始化PC写false
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("开始初始化...")
        
        # 将手自动切换写false
        logger.info("设置手自动切换为false...")
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("initialize", False)
        time.sleep(1.0)
        
        # 等待自动模式为false
        logger.info("等待自动模式为false...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            logger.info("等待自动模式变为false...")
            time.sleep(1.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将初始化PC写true
        logger.info("自动模式已为false，设置初始化PC为true...")
        self.set_node_value("initialize", True)
        time.sleep(1.0)
        
        # 等待初始化完成PC为true
        logger.info("等待初始化完成...")
        init_finished = self.get_node_value("init finished")
        while not init_finished:
            logger.info("初始化中...")
            time.sleep(1.0)
            init_finished = self.get_node_value("init finished")
        
        # 将初始化PC写false
        logger.info("初始化完成，设置初始化PC为false...")
        self.set_node_value("initialize", False)
        
        return {
            "message": "设备初始化完成",
        }

    def download_auto_params(
        self,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
        auto_job_stop_delay: int
    ) -> dict:
        """
        自动模式参数下发函数：
        - 将搅拌仪的搅拌速度、加热温度、时间设置、泵的绝对位置设置和自动作业停止等待时间作为传入参数
        - 一起下发给3个搅拌仪和3个泵
        - 下发后将自动作业参数已下发写true
        - 等待自动作业参数已执行为true
        - 将已下发写false
        - 返回成功

        Args:
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            auto_job_stop_delay: 自动作业等待停止时间

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("开始下发自动模式参数...")
        self.set_node_value("auto_param_applied", False)
        self.set_node_value("auto_param_downloaded", False)
        self.set_node_value("mode_switch", False)
        
        # 下发3个磁力搅拌仪的参数
        for c in (0, 1, 2):
            self.set_node_value(f"mag_stirrer_c{c}_stir_speed", mag_stir_stir_speed)
            self.set_node_value(f"mag_stirrer_c{c}_heat_temp", mag_stir_heat_temp)
            self.set_node_value(f"mag_stirrer_c{c}_time_set", mag_stir_time_set)
        logger.info(f"已下发3个磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")

        # 下发3个注射泵的绝对位置设置
        for p in (0, 1, 2):
            self.set_node_value(f"syringe_pump_{p}_abs_position_set", syringe_pump_abs_position_set)
        logger.info(f"已下发3个注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        # 下发自动作业等待停止时间
        self.set_node_value("auto_job_stop_delay", auto_job_stop_delay)
        logger.info(f"已下发自动作业等待停止时间：{auto_job_stop_delay}")

        # 将自动作业参数已下发写true
        logger.info("设置自动作业参数已下发为true...")
        self.set_node_value("auto_param_downloaded", True)

        # 等待自动作业参数已执行为true
        logger.info("等待自动作业参数已执行...")
        param_applied = self.get_node_value("auto_param_applied")
        while not param_applied:
            logger.info("参数执行中...")
            time.sleep(1.0)
            param_applied = self.get_node_value("auto_param_applied")
        
        logger.info("自动作业参数已执行")
        # 将已下发写false
        self.set_node_value("auto_param_downloaded", False)

        return {
            "message": "自动模式参数下发完成",
        }

    def start_auto_mode(self) -> dict:
        """
        自动作业模式函数：
        - 将模式切换、手自动切换写true
        - 等待自动模式为true
        - 将自动作业开始触发写true
        - 等待自动作业完成为true
        - 返回成功

        Returns:
            dict: 包含 success 和 message
        """
        logger.info("启动自动作业模式...")

        # 将模式切换、手自动切换写true
        logger.info("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", False)
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("auto_run_start_trigger", False)
        self.set_node_value("auto_run_complete", False)
        time.sleep(1.0)
        self.set_node_value("manual_auto_switch", True)

        # 等待自动模式为true
        logger.info("等待自动模式为true...")
        auto_mode = self.get_node_value("auto_mode")
        while not auto_mode:
            logger.info("等待自动模式变为true...")
            time.sleep(1.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将自动作业开始触发写true
        logger.info("自动模式已为true，设置自动作业开始触发为true...")
        self.set_node_value("auto_run_start_trigger", True)

        # 等待自动作业完成为true
        logger.info("等待自动作业完成...")
        auto_run_complete = self.get_node_value("auto_run_complete")
        while not auto_run_complete:
            logger.info("自动作业执行中...")
            time.sleep(5.0)
            auto_run_complete = self.get_node_value("auto_run_complete")
        
        logger.info("自动作业完成")
        self.set_node_value("manual_auto_switch", False)

        return {
            "message": "自动作业模式执行完成",
        }


    

if __name__ == '__main__':
    # 调试用法
    A4 = AI4MDevice(
        url="opc.tcp://127.0.0.1:49320",
        csv_path="opcua_nodes_AI4M_sim.csv"
    )
    
    
    A4.trigger_init()
    print("初始化完成")
    
    # 给水凝胶堆栈A1位置添加clean物料
    rack_warehouse = A4.deck.warehouses["水凝胶烧杯堆栈"]
    clean_carrier = Hydrogel_Clean_1BottleCarrier("烧杯")
    
    # 获取A1位置的索引和位置信息
    rack_site_key = "A1"
    rack_site_idx = list(rack_warehouse._ordering.keys()).index(rack_site_key)
    rack_location = rack_warehouse.child_locations[rack_site_key]
    
    # 将载具分配到A1位置
    rack_warehouse.assign_child_resource(clean_carrier, location=rack_location, spot=rack_site_idx)
    print(f"✓ 已添加clean物料到A1位置: {clean_carrier.name}")
    
    pick_result = A4.trigger_robot_pick_beaker(1, 1)
    print("取烧杯完成")
    
    A4.trigger_robot_place_beaker(pick_result['pick_beaker_id'], pick_result['place_station_id'])
    print("放烧杯完成")
    
    # while True:
    #     time.sleep(1)
