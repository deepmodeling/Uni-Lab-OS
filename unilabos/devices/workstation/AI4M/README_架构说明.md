# AI4M 设备驱动架构说明

## 概述

本次重构将 AI4M 设备驱动的**通讯部分**与**动作函数**分离，使得通讯功能可以被其他程序复用。

## 新架构

### 文件结构

```
AI4M/
├── base_opcua_client.py    # OPC UA 通讯基类（新增）
├── AI4M.py                  # AI4M 设备类（重构）
├── example_usage.py         # 使用示例（新增）
├── decks.py                 # Deck 配置
└── opcua_nodes_AI4M.csv    # 节点配置文件
```

### 类层次结构

```
UniversalDriver
    ↓
BaseOpcUaClient (base_opcua_client.py)
    ├── 客户端连接管理
    ├── 节点注册和查找
    ├── 节点读写操作
    ├── 工作流执行
    └── JSON配置解析
    ↓
OpcUaClientWithSubscription (base_opcua_client.py)
    ├── 继承 BaseOpcUaClient
    ├── 订阅机制
    ├── 缓存机制
    └── 连接监控
    ↓
AI4MDevice (AI4M.py)
    ├── 继承 OpcUaClientWithSubscription
    ├── Deck 资源管理
    └── 具体的设备动作函数
        ├── start_manual_mode()
        ├── trigger_robot_pick_beaker()
        ├── trigger_robot_place_beaker()
        ├── trigger_station_process()
        ├── trigger_init()
        ├── download_auto_params()
        └── start_auto_mode()
```

## 主要类说明

### 1. BaseOpcUaClient

**位置**: `base_opcua_client.py`

**功能**: 提供基础的 OPC UA 通讯功能

**主要方法**:
- `_connect()` - 连接到 OPC UA 服务器
- `_find_nodes()` - 查找并注册节点
- `use_node(name)` - 获取已注册的节点
- `read_node(node_name)` - 读取节点值
- `write_node(json_input)` - 写入节点值
- `call_method(node_name, *args)` - 调用方法节点
- `register_node_list_from_csv_path(path)` - 从 CSV 注册节点
- `create_workflow_from_json(data)` - 从 JSON 创建工作流
- `run_opcua_workflow_model(workflow)` - 运行工作流

**适用场景**: 只需要基本的 OPC UA 通讯功能，不需要订阅和缓存

### 2. OpcUaClientWithSubscription

**位置**: `base_opcua_client.py`

**功能**: 在 BaseOpcUaClient 基础上增加订阅和缓存功能

**主要方法**:
- `get_node_value(name, use_cache, force_read)` - 获取节点值（带缓存）
- `set_node_value(name, value)` - 设置节点值（更新缓存）
- `_setup_subscriptions()` - 设置订阅
- `get_cache_stats()` - 获取缓存统计
- `load_nodes_from_csv(csv_path)` - 从 CSV 加载节点
- `disconnect()` - 断开连接并清理资源

**适用场景**: 需要实时监控节点变化，或需要高频读取节点值的场景

### 3. AI4MDevice

**位置**: `AI4M.py`

**功能**: AI4M 设备的具体实现，包含设备特定的动作函数

**主要方法**:
- `trigger_init()` - 设备初始化
- `start_manual_mode()` - 启动手动模式
- `start_auto_mode()` - 启动自动模式
- `trigger_robot_pick_beaker()` - 机器人取烧杯
- `trigger_robot_place_beaker()` - 机器人放烧杯
- `trigger_station_process()` - 执行工艺流程
- `download_auto_params()` - 下发自动模式参数

## 如何使用

### 1. 使用 AI4M 设备（原有功能保持不变）

```python
from unilabos.devices.workstation.AI4M.AI4M import AI4MDevice

# 创建设备实例
device = AI4MDevice(
    url="opc.tcp://127.0.0.1:49320",
    csv_path="opcua_nodes_AI4M.csv",
    use_subscription=True,
    cache_timeout=5.0
)

# 使用设备动作
device.trigger_init()
device.trigger_robot_pick_beaker(pick_beaker_id=1, place_station_id=1)
device.trigger_station_process(
    station_id=1,
    mag_stir_stir_speed=500,
    mag_stir_heat_temp=25,
    mag_stir_time_set=60,
    syringe_pump_abs_position_set=100
)

# 断开连接
device.disconnect()
```

### 2. 在其他程序中使用通讯基类（简单场景）

```python
from unilabos.devices.workstation.AI4M.base_opcua_client import BaseOpcUaClient
from opcua import Client

class MyDevice(BaseOpcUaClient):
    def __init__(self, url: str):
        super().__init__()
        client = Client(url)
        self._set_client(client)
        self._connect()
    
    def my_action(self):
        # 使用基类提供的读写方法
        result = self.write_node('{"node_name": "node1", "value": 100}')
        value = self.read_node("node1")
        return value

# 使用
device = MyDevice("opc.tcp://localhost:4840")
device.my_action()
```

### 3. 在其他程序中使用通讯基类（带订阅）

```python
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription

class MyAdvancedDevice(OpcUaClientWithSubscription):
    def __init__(self, url: str, csv_path: str):
        super().__init__(
            url=url,
            use_subscription=True,
            cache_timeout=5.0
        )
        self.load_nodes_from_csv(csv_path)
    
    def monitor_process(self):
        # 利用订阅和缓存，高效读取节点值
        while True:
            temp = self.get_node_value("temperature", use_cache=True)
            pressure = self.get_node_value("pressure", use_cache=True)
            print(f"温度: {temp}, 压力: {pressure}")
            time.sleep(0.1)  # 频繁读取，但不会每次都访问服务器

# 使用
device = MyAdvancedDevice("opc.tcp://localhost:4840", "nodes.csv")
device.monitor_process()
```

### 4. 使用工作流功能

```python
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription

class MyWorkflowDevice(OpcUaClientWithSubscription):
    def __init__(self, url: str):
        super().__init__(url=url)
        self.setup_workflow()
    
    def setup_workflow(self):
        workflow_config = [
            {
                "name": "测试工作流",
                "parameters": ["speed", "temp"],
                "action": [
                    {
                        "init_function": {
                            "func_name": "init",
                            "write_nodes": {"speed_sp": "speed", "temp_sp": "temp"}
                        },
                        "start_function": {
                            "func_name": "start",
                            "write_nodes": ["start_trigger"],
                            "condition_nodes": ["process_complete"],
                            "stop_condition_expression": "process_complete == True"
                        },
                        "stop_function": {
                            "func_name": "stop",
                            "write_nodes": {"start_trigger": False}
                        }
                    }
                ]
            }
        ]
        self.create_workflow_from_json(workflow_config)
        self.register_workflows_as_methods()
    
    def run_test(self, speed, temp):
        # 工作流已被注册为方法，可以直接调用
        return self.测试工作流(speed=speed, temp=temp)

# 使用
device = MyWorkflowDevice("opc.tcp://localhost:4840")
device.run_test(speed=100, temp=25.5)
```

## 向后兼容性

为了保持向后兼容，`AI4M.py` 中保留了 `OpcUaClient` 作为 `AI4MDevice` 的别名：

```python
# AI4M.py 中
OpcUaClient = AI4MDevice
```

因此，原有代码无需修改即可继续使用：

```python
# 旧代码仍然可以工作
from unilabos.devices.workstation.AI4M.AI4M import OpcUaClient

client = OpcUaClient(
    url="opc.tcp://127.0.0.1:49320",
    csv_path="opcua_nodes_AI4M.csv"
)
```

## 优势

### 1. 代码复用
- 通讯功能被提取到独立的基类
- 其他设备可以直接继承，无需重复编写通讯代码

### 2. 职责分离
- **通讯基类**: 专注于 OPC UA 通讯
- **设备类**: 专注于设备特定的业务逻辑

### 3. 易于维护
- 通讯功能的修改只需要在基类中进行
- 设备特定功能的修改只影响设备类

### 4. 灵活性
- 可以选择使用基础通讯功能（BaseOpcUaClient）
- 或使用带订阅的高级功能（OpcUaClientWithSubscription）
- 根据实际需求选择合适的基类

### 5. 易于测试
- 通讯功能可以独立测试
- 设备功能可以在模拟环境中测试

## 迁移指南

如果你有现有的设备需要迁移到新架构：

### 步骤 1: 确定需要的基类

- 如果只需要基本通讯功能 → 继承 `BaseOpcUaClient`
- 如果需要订阅和缓存 → 继承 `OpcUaClientWithSubscription`

### 步骤 2: 创建设备类

```python
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription

class MyDevice(OpcUaClientWithSubscription):
    def __init__(self, url: str, csv_path: str = None):
        super().__init__(url=url)
        
        if csv_path:
            self.load_nodes_from_csv(csv_path)
    
    # 添加设备特定的动作函数
    def my_device_action(self):
        pass
```

### 步骤 3: 实现设备特定功能

将设备特定的业务逻辑实现为方法：

```python
def my_device_action(self, param1, param2):
    """设备特定的动作"""
    # 使用基类提供的通讯方法
    self.set_node_value("param1_node", param1)
    self.set_node_value("param2_node", param2)
    
    # 等待完成
    while not self.get_node_value("action_complete"):
        time.sleep(0.5)
    
    return True
```

## 常见问题

### Q: 我应该使用哪个基类？

**A**: 
- 如果你的设备只需要偶尔读写节点 → `BaseOpcUaClient`
- 如果你的设备需要实时监控节点变化 → `OpcUaClientWithSubscription`
- 如果你不确定 → 使用 `OpcUaClientWithSubscription`（功能更全面）

### Q: 订阅模式和按需读取有什么区别？

**A**:
- **订阅模式**: 服务器主动推送节点变化，读取速度快，但占用更多资源
- **按需读取**: 每次读取时访问服务器，速度较慢，但资源占用少
- 可以通过 `use_subscription=True/False` 参数控制

### Q: 如何禁用缓存？

**A**: 
```python
# 强制从服务器读取，忽略缓存
value = device.get_node_value("node_name", use_cache=False, force_read=True)
```

### Q: 原有的 AI4M 代码需要修改吗？

**A**: 不需要。为了保持向后兼容，原有的 `OpcUaClient` 类名仍然可用。

## 参考示例

详细的使用示例请参考 `example_usage.py` 文件，其中包含：
- 简单设备示例（不带订阅）
- 高级设备示例（带订阅和缓存）
- 工作流设备示例（使用 JSON 配置）

## 技术支持

如有问题或建议，请联系开发团队。
