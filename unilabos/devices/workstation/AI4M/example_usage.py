"""
示例：如何在其他程序中使用 OPC UA 通讯基类

这个文件展示了如何继承通讯基类来创建自己的设备驱动
"""

import time
from unilabos.devices.workstation.AI4M.base_opcua_client import (
    BaseOpcUaClient, 
    OpcUaClientWithSubscription
)
from unilabos.utils.log import logger


# 示例1: 使用基础通讯客户端（不带订阅）
class MySimpleDevice(BaseOpcUaClient):
    """
    简单设备示例 - 仅使用基础通讯功能
    不需要订阅和缓存
    """
    
    def __init__(self, url: str, csv_path: str = None):
        super().__init__()
        
        from opcua import Client
        client = Client(url)
        self._set_client(client)
        self._connect()
        
        if csv_path:
            self.register_node_list_from_csv_path(csv_path)
    
    # 添加自定义动作函数
    def my_custom_action(self, param1: int, param2: str) -> bool:
        """自定义动作函数"""
        logger.info(f"执行自定义动作: param1={param1}, param2={param2}")
        
        # 使用基类提供的读写方法
        try:
            # 写入节点
            result = self.write_node('{"node_name": "some_node", "value": ' + str(param1) + '}')
            logger.info(f"写入结果: {result}")
            
            # 读取节点
            result = self.read_node("some_node")
            logger.info(f"读取结果: {result}")
            
            return True
        except Exception as e:
            logger.error(f"动作执行失败: {e}")
            return False


# 示例2: 使用带订阅功能的通讯客户端
class MyAdvancedDevice(OpcUaClientWithSubscription):
    """
    高级设备示例 - 使用订阅和缓存功能
    适合需要实时监控节点变化的场景
    """
    
    def __init__(
        self, 
        url: str, 
        csv_path: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0
    ):
        super().__init__(
            url=url,
            use_subscription=use_subscription,
            cache_timeout=cache_timeout
        )
        
        if csv_path:
            self.load_nodes_from_csv(csv_path)
    
    # 添加自定义动作函数
    def start_process(self, speed: int, temperature: float) -> bool:
        """启动工艺流程"""
        logger.info(f"启动工艺: 速度={speed}, 温度={temperature}")
        
        try:
            # 使用缓存读取（从订阅或缓存中获取）
            current_state = self.get_node_value("device_state", use_cache=True)
            logger.info(f"当前设备状态: {current_state}")
            
            # 写入参数
            self.set_node_value("speed_setpoint", speed)
            self.set_node_value("temp_setpoint", temperature)
            
            # 启动
            self.set_node_value("process_start", True)
            
            # 等待完成
            while True:
                process_complete = self.get_node_value("process_complete")
                if process_complete:
                    logger.info("工艺完成")
                    break
                time.sleep(1.0)
            
            return True
        except Exception as e:
            logger.error(f"工艺执行失败: {e}")
            return False
    
    def stop_process(self) -> bool:
        """停止工艺流程"""
        logger.info("停止工艺")
        
        try:
            self.set_node_value("process_start", False)
            return True
        except Exception as e:
            logger.error(f"停止失败: {e}")
            return False
    
    def get_realtime_data(self) -> dict:
        """获取实时数据（利用订阅的优势）"""
        try:
            data = {
                "speed": self.get_node_value("current_speed", use_cache=True),
                "temperature": self.get_node_value("current_temp", use_cache=True),
                "pressure": self.get_node_value("current_pressure", use_cache=True),
                "state": self.get_node_value("device_state", use_cache=True)
            }
            return data
        except Exception as e:
            logger.error(f"获取实时数据失败: {e}")
            return {}


# 示例3: 使用工作流功能
class MyWorkflowDevice(OpcUaClientWithSubscription):
    """
    工作流设备示例 - 使用基类提供的工作流功能
    适合复杂的多步骤流程
    """
    
    def __init__(self, url: str, csv_path: str = None):
        super().__init__(url=url)
        
        if csv_path:
            self.load_nodes_from_csv(csv_path)
    
    def setup_workflow(self):
        """设置工作流"""
        # 使用 JSON 配置定义工作流
        workflow_config = [
            {
                "name": "初始化工作流",
                "parameters": ["speed", "temperature"],
                "description": "设备初始化流程",
                "action": [
                    {
                        "init_function": {
                            "func_name": "init_params",
                            "write_nodes": {
                                "speed_setpoint": "speed",
                                "temp_setpoint": "temperature"
                            }
                        },
                        "start_function": {
                            "func_name": "start_init",
                            "write_nodes": ["init_trigger"],
                            "condition_nodes": ["init_complete"],
                            "stop_condition_expression": "init_complete == True"
                        },
                        "stop_function": {
                            "func_name": "stop_init",
                            "write_nodes": {"init_trigger": False}
                        }
                    }
                ]
            }
        ]
        
        # 创建工作流
        self.create_workflow_from_json(workflow_config)
        
        # 注册为方法
        self.register_workflows_as_methods()
    
    def run_initialization(self, speed: int, temperature: float):
        """运行初始化工作流"""
        # 工作流已经被注册为方法，可以直接调用
        # 方法名就是工作流的name
        if hasattr(self, "初始化工作流"):
            return self.初始化工作流(speed=speed, temperature=temperature)
        else:
            logger.error("工作流未设置")
            return False


# 使用示例
if __name__ == "__main__":
    print("="*80)
    print("OPC UA 通讯基类使用示例")
    print("="*80)
    
    # 示例1: 简单设备
    print("\n1. 简单设备示例（无订阅）:")
    print("-" * 80)
    try:
        simple_device = MySimpleDevice(
            url="opc.tcp://localhost:4840",
            csv_path="nodes.csv"
        )
        # simple_device.my_custom_action(100, "test")
        # simple_device.disconnect()
        print("✓ 简单设备初始化成功")
    except Exception as e:
        print(f"✗ 简单设备示例失败: {e}")
    
    # 示例2: 高级设备
    print("\n2. 高级设备示例（带订阅）:")
    print("-" * 80)
    try:
        advanced_device = MyAdvancedDevice(
            url="opc.tcp://localhost:4840",
            csv_path="nodes.csv",
            use_subscription=True,
            cache_timeout=5.0
        )
        # advanced_device.start_process(speed=100, temperature=25.5)
        # data = advanced_device.get_realtime_data()
        # print(f"实时数据: {data}")
        # advanced_device.disconnect()
        print("✓ 高级设备初始化成功")
    except Exception as e:
        print(f"✗ 高级设备示例失败: {e}")
    
    # 示例3: 工作流设备
    print("\n3. 工作流设备示例:")
    print("-" * 80)
    try:
        workflow_device = MyWorkflowDevice(
            url="opc.tcp://localhost:4840",
            csv_path="nodes.csv"
        )
        # workflow_device.setup_workflow()
        # workflow_device.run_initialization(speed=100, temperature=25.5)
        # workflow_device.disconnect()
        print("✓ 工作流设备初始化成功")
    except Exception as e:
        print(f"✗ 工作流设备示例失败: {e}")
    
    print("\n" + "="*80)
    print("示例完成")
    print("="*80)
