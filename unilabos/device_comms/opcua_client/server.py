#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPC UA测试服务器
用于测试OPC UA客户端功能，特别是temperature_control和valve_control工作流
"""

import sys
import time
import logging
from opcua import Server, ua
import threading

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class OpcUaTestServer:
    """OPC UA测试服务器类"""
    
    def __init__(self, endpoint="opc.tcp://localhost:4840/freeopcua/server/"):
        """
        初始化OPC UA服务器
        
        Args:
            endpoint: 服务器端点URL
        """
        self.server = Server()
        self.server.set_endpoint(endpoint)
        
        # 设置服务器名称
        self.server.set_server_name("UniLabOS OPC UA Test Server")
        
        # 设置服务器命名空间
        self.idx = self.server.register_namespace("http://unilabos.com/opcua/test")
        
        # 获取Objects节点
        self.objects = self.server.get_objects_node()
        
        # 创建设备对象
        self.device = self.objects.add_object(self.idx, "TestDevice")
        
        # 存储所有节点的字典
        self.nodes = {}
        
        # 初始化标志
        self.running = False
        
        # 控制标志
        self.simulation_active = True
    
    def add_variable(self, name, value, data_type=None):
        """
        添加变量节点
        
        Args:
            name: 变量名称
            value: 初始值
            data_type: 数据类型 (可选)
        """
        if data_type is None:
            var = self.device.add_variable(self.idx, name, value)
        else:
            var = self.device.add_variable(self.idx, name, value, data_type)
        
        # 设置变量可写
        var.set_writable()
        
        # 存储节点
        self.nodes[name] = var
        logger.info(f"添加变量节点: {name}, 初始值: {value}")
        return var
    
    def add_method(self, name, callback, inputs=None, outputs=None):
        """
        添加方法节点
        
        Args:
            name: 方法名称
            callback: 回调函数
            inputs: 输入参数列表 [(name, type), ...]
            outputs: 输出参数列表 [(name, type), ...]
        """
        if inputs is None:
            inputs = []
        if outputs is None:
            outputs = []
        
        # 创建输入参数
        input_args = []
        for arg_name, arg_type in inputs:
            input_args.append(ua.Argument())
            input_args[-1].Name = arg_name
            input_args[-1].DataType = arg_type
            input_args[-1].ValueRank = -1
        
        # 创建输出参数
        output_args = []
        for arg_name, arg_type in outputs:
            output_args.append(ua.Argument())
            output_args[-1].Name = arg_name
            output_args[-1].DataType = arg_type
            output_args[-1].ValueRank = -1
        
        # 添加方法
        method = self.device.add_method(
            self.idx, 
            name, 
            callback, 
            input_args, 
            output_args
        )
        
        # 存储节点
        self.nodes[name] = method
        logger.info(f"添加方法节点: {name}")
        return method
    
    def start(self):
        """启动服务器"""
        if not self.running:
            self.server.start()
            self.running = True
            logger.info("OPC UA服务器已启动")
            
            # 启动模拟线程
            self.simulation_thread = threading.Thread(target=self.run_simulation)
            self.simulation_thread.daemon = True
            self.simulation_thread.start()
    
    def stop(self):
        """停止服务器"""
        if self.running:
            self.simulation_active = False
            if hasattr(self, 'simulation_thread'):
                self.simulation_thread.join(timeout=2)
            self.server.stop()
            self.running = False
            logger.info("OPC UA服务器已停止")
    
    def get_node(self, name):
        """获取节点"""
        if name in self.nodes:
            return self.nodes[name]
        return None
    
    def update_variable(self, name, value):
        """更新变量值"""
        if name in self.nodes:
            self.nodes[name].set_value(value)
            logger.debug(f"更新变量 {name} = {value}")
            return True
        logger.warning(f"变量 {name} 不存在")
        return False
    
    def run_simulation(self):
        """运行模拟线程"""
        logger.info("启动模拟线程")
        
        temp = 20.0
        valve_position = 0.0
        flow_rate = 0.0
        
        while self.simulation_active and self.running:
            try:
                # 温度控制模拟
                heating_enabled = self.get_node("HeatingEnabled").get_value()
                setpoint = self.get_node("Setpoint").get_value()
                
                if heating_enabled:
                    self.update_variable("HeatingStatus", True)
                    if temp < setpoint:
                        temp += 0.5  # 加快温度上升速度
                    else:
                        temp -= 0.1
                else:
                    self.update_variable("HeatingStatus", False)
                    if temp > 20.0:
                        temp -= 0.2
                
                # 更新温度
                self.update_variable("Temperature", round(temp, 2))
                
                # 阀门控制模拟
                valve_control = self.get_node("ValveControl").get_value()
                valve_setpoint = self.get_node("ValveSetpoint").get_value()
                
                if valve_control:
                    if valve_position < valve_setpoint:
                        valve_position += 5.0  # 加快阀门开启速度
                        if valve_position > valve_setpoint:
                            valve_position = valve_setpoint
                    else:
                        valve_position -= 1.0
                        if valve_position < 0:
                            valve_position = 0
                else:
                    if valve_position > 0:
                        valve_position -= 5.0
                        if valve_position < 0:
                            valve_position = 0
                
                # 更新阀门位置
                self.update_variable("ValvePosition", round(valve_position, 2))
                
                # 流量模拟 - 与阀门位置成正比
                flow_rate = valve_position * 0.2  # 简单线性关系
                self.update_variable("FlowRate", round(flow_rate, 2))
                
                # 更新系统状态
                status = []
                if heating_enabled:
                    status.append("Heating")
                if valve_control:
                    status.append("Valve_Open")
                
                if status:
                    self.update_variable("SystemStatus", "_".join(status))
                else:
                    self.update_variable("SystemStatus", "Idle")
                
                # 每200毫秒更新一次
                time.sleep(0.2)
                
            except Exception as e:
                logger.error(f"模拟线程错误: {e}")
                time.sleep(1)  # 出错时稍等一会再继续
        
        logger.info("模拟线程已停止")

def reset_alarm_callback(parent, *args):
    """重置报警的回调函数"""
    logger.info("调用了重置报警方法")
    return True

def start_process_callback(parent, *args):
    """启动流程的回调函数"""
    process_id = args[0] if args else 0
    logger.info(f"启动流程 ID: {process_id}")
    return process_id

def stop_process_callback(parent, *args):
    """停止流程的回调函数"""
    process_id = args[0] if args else 0
    logger.info(f"停止流程 ID: {process_id}")
    return True

def main():
    """主函数"""
    try:
        # 创建服务器
        server = OpcUaTestServer()
        
        # 添加变量节点 - 温度控制相关
        server.add_variable("Temperature", 20.0, ua.VariantType.Float)
        server.add_variable("Setpoint", 22.0, ua.VariantType.Float)
        server.add_variable("HeatingEnabled", False, ua.VariantType.Boolean)
        server.add_variable("HeatingStatus", False, ua.VariantType.Boolean)
        
        # 添加变量节点 - 阀门控制相关
        server.add_variable("ValvePosition", 0.0, ua.VariantType.Float)
        server.add_variable("ValveSetpoint", 0.0, ua.VariantType.Float)
        server.add_variable("ValveControl", False, ua.VariantType.Boolean)
        server.add_variable("FlowRate", 0.0, ua.VariantType.Float)
        
        # 其他状态变量
        server.add_variable("SystemStatus", "Idle", ua.VariantType.String)
        
        # 添加方法节点
        server.add_method(
            "ResetAlarm", 
            reset_alarm_callback, 
            [], 
            [("Result", ua.VariantType.Boolean)]
        )
        
        server.add_method(
            "StartProcess", 
            start_process_callback, 
            [("ProcessId", ua.VariantType.Int32)], 
            [("Result", ua.VariantType.Int32)]
        )
        
        server.add_method(
            "StopProcess", 
            stop_process_callback, 
            [("ProcessId", ua.VariantType.Int32)], 
            [("Result", ua.VariantType.Boolean)]
        )
        
        # 启动服务器
        server.start()
        logger.info("服务器已启动，按Ctrl+C停止")
        
        # 保持服务器运行
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到键盘中断，正在停止服务器...")
        
        # 停止服务器
        server.stop()
        
    except Exception as e:
        logger.error(f"服务器错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main() 