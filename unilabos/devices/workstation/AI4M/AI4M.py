"""
AI4M 设备驱动
继承自 OPC UA 通讯基类，实现具体的设备动作函数
"""

import json
import time
import traceback
from typing import Optional
import os

from opcua import Client
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.log import logger
from unilabos.devices.workstation.AI4M.decks import AI4M_deck

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

        # 统计仓库信息
        warehouse_count = 0
        if hasattr(self.deck, 'children'):
            warehouse_count = len(self.deck.children)
            logger.info(f"Deck 初始化完成，加载 {warehouse_count} 个资源")
        
        # 如果提供了 CSV 路径，则直接加载节点
        if csv_path:
            self.load_nodes_from_csv(csv_path)

    def post_init(self, ros_node):
        """ROS2 节点就绪后的初始化"""
        if not (hasattr(self, 'deck') and self.deck):
            return
            
        if not (hasattr(ros_node, 'resource_tracker') and ros_node.resource_tracker):
            logger.warning("resource_tracker 不存在，无法注册 deck")
            return
        
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
    
    def start_manual_mode(self) -> bool:
        """
        指令作业模式函数：
        - 将模式切换、手自动切换写false
        - 等待自动模式为false
        - 将模式切换写true

        返回: 是否成功完成自动作业
        """
        self.success = False

        print("启动指令作业模式...")

        # 将模式切换、手自动切换写true
        print("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", True)
        self.set_node_value("manual_auto_switch", False)

        # 等待自动模式为false
        print("等待自动模式为False...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            print("等待自动模式变为False...")
            time.sleep(1.0)
            auto_mode = self.get_node_value("auto_mode")
        else:
            print("模式切换完成")
            self.success = True

        return self.success

    def trigger_robot_pick_beaker(
        self,
        pick_beaker_id: int,
        place_station_id: int,
    ) -> bool:
        """
        机器人取烧杯并放到检测位：
        - 先写入取烧杯编号，等待取烧杯完成
        - 取完成后再写入放检测编号，等待对应的放检测完成信号
        
        参数:
            pick_beaker_id: 取烧杯编号（1-5）
            place_station_id: 放检测编号（1-3）
            
        返回: 是否成功完成操作
        """
        self.success = False

        # 校验输入范围
        if pick_beaker_id not in (1, 2, 3, 4, 5):
            logger.error("取烧杯编号必须在 1-5 范围内")
            return False
        if place_station_id not in (1, 2, 3):
            logger.error("放检测编号必须在 1-3 范围内")
            return False

        # 获取仓库资源
        rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
        station_warehouse = self.deck.warehouses[f"反应工站{place_station_id}"]
        rack_site_key = f"A{pick_beaker_id}"

        pick_complete_node = f"robot_rack_pick_beaker_{pick_beaker_id}_complete"
        place_complete_node = f"robot_place_station_{place_station_id}_complete"

        # 阶段1：下发取烧杯编号并等待完成
        print("下发取烧杯编号，等待完成...")
        self.set_node_value("robot_pick_beaker_id", pick_beaker_id)
        
        # 等待取烧杯完成
        pick_complete = self.get_node_value(pick_complete_node)
        while not pick_complete:
            print("取烧杯中...")
            time.sleep(2.0)
            pick_complete = self.get_node_value(pick_complete_node)
        
        # 获取载具（carrier）
        carrier = rack_warehouse[rack_site_key]
        if carrier is None:
            logger.error(f"堆栈位置 {rack_site_key} 没有载具")
            return False
        
        # 阶段1.5：机器人取烧杯完成后，从堆栈解绑载具
        try:
            rack_warehouse.unassign_child_resource(carrier)
            print(f"✓ 已从堆栈解绑载具 {carrier.name}")
        except Exception as e:
            logger.error(f"从堆栈解绑载具失败: {e}")
            return False
        
        # 阶段2：取完成后再下发放检测编号并等待完成
        print("取完成，开始下发放检测编号...")
        self.set_node_value("robot_place_station_id", place_station_id)
        
        # 等待放检测完成
        place_complete = self.get_node_value(place_complete_node)
        while not place_complete:
            print("放检测中...")
            time.sleep(2.0)
            place_complete = self.get_node_value(place_complete_node)
        
        # 阶段2.5：机器人放到检测站完成后，绑定载具到检测站
        try:
            station_site_idx = 0
            station_site_key = list(station_warehouse._ordering.keys())[station_site_idx]
            station_location = station_warehouse.child_locations[station_site_key]
            
            station_warehouse.assign_child_resource(carrier, location=station_location, spot=station_site_idx)
            print(f"✓ 已绑定载具 {carrier.name} 到检测站{place_station_id}")
        except Exception as e:
            logger.error(f"绑定载具到检测站失败: {e}")
        
        print("放检测完成")
        self.success = True
            
        # 更新资源树到前端
        if hasattr(self, '_ros_node') and self._ros_node:
            try:
                from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                print(f"✓ 已同步资源更新到前端")
            except Exception as e:
                logger.warning(f"前端资源更新失败: {e}")

        return self.success

    def trigger_robot_place_beaker(
        self,
        place_beaker_id: int,
        pick_station_id: int,
    ) -> bool:
        """
        机器人从检测位取烧杯并放回：
        - 先写入取检测编号，等待取检测完成
        - 取完成后再写入放烧杯编号，等待对应的放烧杯完成信号
        
        参数:
            place_beaker_id: 放烧杯编号（1-5）
            pick_station_id: 取检测编号（1-3）
            
        返回: 是否成功完成操作
        """
        self.success = False

        # 校验输入范围
        if place_beaker_id not in (1, 2, 3, 4, 5):
            logger.error("放烧杯编号必须在 1-5 范围内")
            return False
        if pick_station_id not in (1, 2, 3):
            logger.error("取检测编号必须在 1-3 范围内")
            return False

        # 获取仓库资源
        rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
        station_warehouse = self.deck.warehouses[f"反应工站{pick_station_id}"]
        
        # 获取检测站的载具
        station_site_idx = 0
        
        if not station_warehouse.sites or len(station_warehouse.sites) == 0:
            logger.error(f"检测站{pick_station_id} 的 warehouse sites 列表为空")
            return False
        
        carrier = station_warehouse.sites[station_site_idx]
        
        # 检查是否是 ResourceHolder
        if carrier is None or type(carrier).__name__ == 'ResourceHolder':
            logger.error(f"检测站{pick_station_id} 没有载具（可能是空的 ResourceHolder）")
            return False
        
        # 确定堆栈目标位置
        rack_site_key = f"C{place_beaker_id}"

        pick_complete_node = f"robot_pick_station_{pick_station_id}_complete"
        place_complete_node = f"robot_rack_place_beaker_{place_beaker_id}_complete"

        # 阶段1：下发取检测编号并等待完成
        print("下发取检测编号，等待完成...")
        self.set_node_value("robot_pick_station_id", pick_station_id)
        
        # 等待取检测完成
        pick_complete = self.get_node_value(pick_complete_node)
        while not pick_complete:
            print("取检测中...")
            time.sleep(2.0)
            pick_complete = self.get_node_value(pick_complete_node)
        
        # 阶段1.5：机器人取检测完成后，从检测站解绑载具
        try:
            station_warehouse.unassign_child_resource(carrier)
            print(f"✓ 已从检测站{pick_station_id}解绑载具 {carrier.name}")
        except Exception as e:
            logger.error(f"从检测站解绑载具失败: {e}")
            return False
        
        # 阶段2：取完成后再下发放烧杯编号并等待完成
        print("取完成，开始下发放烧杯编号...")
        self.set_node_value("robot_place_beaker_id", place_beaker_id)
        
        # 等待放烧杯完成
        place_complete = self.get_node_value(place_complete_node)
        while not place_complete:
            print("放烧杯中...")
            time.sleep(2.0)
            place_complete = self.get_node_value(place_complete_node)
        
        # 阶段2.5：机器人放烧杯完成后，绑定载具回堆栈
        try:
            rack_site_idx = list(rack_warehouse._ordering.keys()).index(rack_site_key)
            rack_location = rack_warehouse.child_locations[rack_site_key]
            
            rack_warehouse.assign_child_resource(carrier, location=rack_location, spot=rack_site_idx)
            print(f"✓ 已绑定载具 {carrier.name} 回堆栈 {rack_site_key}")
        except Exception as e:
            logger.error(f"绑定载具回堆栈失败: {e}")
        
        print("放烧杯完成")
        self.success = True
        
        # 更新资源树到前端
        if hasattr(self, '_ros_node') and self._ros_node:
            try:
                from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                print(f"✓ 已同步资源更新到前端")
            except Exception as e:
                logger.warning(f"前端资源更新失败: {e}")

        return self.success

    def trigger_station_process(
        self,
        station_id: int,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
    ) -> bool:
        """
        执行检测工艺流程：
        1. 等待检测站请求参数
        2. 下发对应编号的搅拌仪和注射泵参数
        3. 等待参数已执行
        4. 给出检测开始信号
        5. 等待检测工艺完成
        
        参数:
            station_id: 检测编号（1-3）
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            
        返回: 是否成功完成工艺
        """
        self.success = False

        # 校验输入范围
        if station_id not in (1, 2, 3):
            logger.error("检测编号必须在 1-3 范围内")
            return False

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
        print(f"等待检测{station_id}请求参数...")
        request_params = self.get_node_value(request_node)
        while not request_params:
            print(f"等待检测{station_id}请求参数中...")
            time.sleep(2.0)
            request_params = self.get_node_value(request_node)
        
        print(f"检测{station_id}已请求参数，开始下发...")
        
        # 阶段2：下发对应编号的搅拌仪参数
        self.set_node_value(f"mag_stirrer_c{station_idx}_stir_speed", mag_stir_stir_speed)
        self.set_node_value(f"mag_stirrer_c{station_idx}_heat_temp", mag_stir_heat_temp)
        self.set_node_value(f"mag_stirrer_c{station_idx}_time_set", mag_stir_time_set)
        print(f"已下发检测{station_id}磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")
        
        # 下发对应编号的注射泵参数
        self.set_node_value(f"syringe_pump_{station_idx}_abs_position_set", syringe_pump_abs_position_set)
        print(f"已下发检测{station_id}注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        
        # 阶段3：等待参数已执行
        self.set_node_value(start_node, True)
        print(f"等待检测{station_id}参数已执行...")
        params_received = self.get_node_value(params_received_node)
        while not params_received:
            print(f"检测{station_id}参数执行中...")
            time.sleep(2.0)
            params_received = self.get_node_value(params_received_node)
        
        print(f"检测{station_id}参数已执行")
           
        # 阶段4：等待检测工艺完成
        print(f"等待检测{station_id}工艺完成...")
        process_complete = self.get_node_value(complete_node)
        while not process_complete:
            print(f"检测{station_id}工艺执行中...")
            time.sleep(2.0)
            process_complete = self.get_node_value(complete_node)
        else:
            print(f"检测{station_id}工艺完成")
            self.set_node_value(start_node, False)
            self.success = True

        return self.success

    def trigger_init(self) -> bool:
        """
        初始化函数：
        - 将手自动切换写false
        - 等待自动模式为false
        - 将初始化PC写true
        - 等待初始化完成PC为true
        - 将初始化PC写false
        - 返回成功

        返回: 是否成功完成初始化
        """
        self.success = False

        print("开始初始化...")
        
        # 将手自动切换写false
        print("设置手自动切换为false...")
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("initialize", False)
        time.sleep(1.0)
        
        # 等待自动模式为false
        print("等待自动模式为false...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            print("等待自动模式变为false...")
            time.sleep(2.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将初始化PC写true
        print("自动模式已为false，设置初始化PC为true...")
        self.set_node_value("initialize", True)
        time.sleep(2.0)
        
        # 等待初始化完成PC为true
        print("等待初始化完成...")
        init_finished = self.get_node_value("init finished")
        while not init_finished:
            print("初始化中...")
            time.sleep(2.0)
            init_finished = self.get_node_value("init finished")
        else:
            # 将初始化PC写false
            print("初始化完成，设置初始化PC为false...")
            self.set_node_value("initialize", False)
            self.success = True
        
        return self.success

    def download_auto_params(
        self,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
        auto_job_stop_delay: int
    ) -> bool:
        """
        自动模式参数下发函数：
        - 将搅拌仪的搅拌速度、加热温度、时间设置、泵的绝对位置设置和自动作业停止等待时间作为传入参数
        - 一起下发给3个搅拌仪和3个泵
        - 下发后将自动作业参数已下发写true
        - 等待自动作业参数已执行为true
        - 将已下发写false
        - 返回成功

        参数:
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            auto_job_stop_delay: 自动作业等待停止时间

        返回: 是否成功完成参数下发
        """
        self.success = False
        
        print("开始下发自动模式参数...")
        self.set_node_value("auto_param_applied", False)
        self.set_node_value("auto_param_downloaded", False)
        self.set_node_value("mode_switch", False)
        
        # 下发3个磁力搅拌仪的参数
        for c in (0, 1, 2):
            self.set_node_value(f"mag_stirrer_c{c}_stir_speed", mag_stir_stir_speed)
            self.set_node_value(f"mag_stirrer_c{c}_heat_temp", mag_stir_heat_temp)
            self.set_node_value(f"mag_stirrer_c{c}_time_set", mag_stir_time_set)
        print(f"已下发3个磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")

        # 下发3个注射泵的绝对位置设置
        for p in (0, 1, 2):
            self.set_node_value(f"syringe_pump_{p}_abs_position_set", syringe_pump_abs_position_set)
        print(f"已下发3个注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        # 下发自动作业等待停止时间
        self.set_node_value("auto_job_stop_delay", auto_job_stop_delay)
        print(f"已下发自动作业等待停止时间：{auto_job_stop_delay}")

        # 将自动作业参数已下发写true
        print("设置自动作业参数已下发为true...")
        self.set_node_value("auto_param_downloaded", True)

        # 等待自动作业参数已执行为true
        print("等待自动作业参数已执行...")
        param_applied = self.get_node_value("auto_param_applied")
        while not param_applied:
            print("参数执行中...")
            time.sleep(2.0)
            param_applied = self.get_node_value("auto_param_applied")
        else:
            print("自动作业参数已执行")
            # 将已下发写false
            self.set_node_value("auto_param_downloaded", False)
            self.success = True

        return self.success

    def start_auto_mode(self) -> bool:
        """
        自动作业模式函数：
        - 将模式切换、手自动切换写true
        - 等待自动模式为true
        - 将自动作业开始触发写true
        - 等待自动作业完成为true
        - 返回成功

        返回: 是否成功完成自动作业
        """
        self.success = False

        print("启动自动作业模式...")

        # 将模式切换、手自动切换写true
        print("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", False)
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("auto_run_start_trigger", False)
        self.set_node_value("auto_run_complete", False)
        time.sleep(1.0)
        self.set_node_value("manual_auto_switch", True)

        # 等待自动模式为true
        print("等待自动模式为true...")
        auto_mode = self.get_node_value("auto_mode")
        while not auto_mode:
            print("等待自动模式变为true...")
            time.sleep(5.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将自动作业开始触发写true
        print("自动模式已为true，设置自动作业开始触发为true...")
        self.set_node_value("auto_run_start_trigger", True)

        # 等待自动作业完成为true
        print("等待自动作业完成...")
        auto_run_complete = self.get_node_value("auto_run_complete")
        while not auto_run_complete:
            print("自动作业执行中...")
            time.sleep(5.0)
            auto_run_complete = self.get_node_value("auto_run_complete")
        else:
            print("自动作业完成")
            self.set_node_value("manual_auto_switch", False)
            self.success = True

        return self.success


# 为了向后兼容，保留旧的类名
OpcUaClient = AI4MDevice
    

if __name__ == '__main__':
    # 示例用法
    try:
        client = AI4MDevice(
            url="opc.tcp://127.0.0.1:49320",
            csv_path="opcua_nodes_AI4M.csv"
        )
        
        # 测试初始化函数
        print("\n" + "="*80)
        print("测试: 初始化函数")
        print("="*80)
        
        result = client.trigger_init()
        print(f"初始化结果: {'成功' if result else '失败'}")
        
        print("\n" + "="*80)
        print("测试完成")
        print("="*80)
        
        # 断开连接
        client.disconnect()
        
    except Exception as e:
        print(f"错误: {e}")
        traceback.print_exc()
