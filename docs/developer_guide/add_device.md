# 添加设备：编写驱动

在 Uni-Lab 中，设备（Device）是实验操作的基础单元。Uni-Lab 使用**注册表机制**来兼容管理种类繁多的设备驱动程序。抽象的设备对外拥有【话题】【服务】【动作】三种通信机制，因此将设备添加进 Uni-Lab，实际上是将设备驱动中的这三种机制映射到 Uni-Lab 标准指令集上。

> **💡 提示：** 本文档介绍如何使用已有的设备驱动（SDK）。若设备没有现成的驱动程序，需要自己开发驱动，请参考 {doc}`add_old_device`。

## 支持的驱动类型

Uni-Lab 支持以下两种驱动程序：

### 1. Python Class（推荐）

Python 类设备驱动在完成注册表后可以直接在 Uni-Lab 中使用，无需额外编译。

**示例：**

```python
from unilabos.registry.decorators import device, topic_config

@device(id="mock_gripper", category=["gripper"], description="Mock Gripper")
class MockGripper:
    def __init__(self):
        self._position: float = 0.0
        self._velocity: float = 2.0
        self._torque: float = 0.0
        self._status = "Idle"

    @property
    @topic_config()  # 添加 @topic_config 才会定时广播
    def position(self) -> float:
        return self._position

    @property
    @topic_config()
    def velocity(self) -> float:
        return self._velocity

    @property
    @topic_config()
    def torque(self) -> float:
        return self._torque

    # 使用 @topic_config 装饰的属性，接入 Uni-Lab 时会定时对外广播
    @property
    @topic_config(period=2.0)  # 可自定义发布周期
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, target):
        self._status = target

    # 会被自动识别的设备动作，接入 Uni-Lab 时会作为 ActionServer 接受任意控制者的指令
    def push_to(self, position: float, torque: float, velocity: float = 0.0):
        self._status = "Running"
        current_pos = self.position
        if velocity == 0.0:
            velocity = self.velocity

        move_time = abs(position - current_pos) / velocity
        for i in range(20):
            self._position = current_pos + (position - current_pos) / 20 * (i+1)
            self._torque = torque / (20 - i)
            self._velocity = velocity
            time.sleep(move_time / 20)
        self._torque = torque
        self._status = "Idle"
```

### 2. C# Class

C# 驱动设备在完成注册表后，需要调用 Uni-Lab C# 编译后才能使用（仅需一次）。

**示例：**

```csharp
using System;
using System.Threading.Tasks;

public class MockGripper
{
    // 会被自动识别的设备属性，接入 Uni-Lab 时会定时对外广播
    public double position { get; private set; } = 0.0;
    public double velocity { get; private set; } = 2.0;
    public double torque { get; private set; } = 0.0;
    public string status { get; private set; } = "Idle";

    // 需要在注册表添加的设备动作，接入 Uni-Lab 时会作为 ActionServer 接受任意控制者的指令
    public async Task PushToAsync(double Position, double Torque, double Velocity = 0.0)
    {
        status = "Running";
        double currentPos = Position;
        if (Velocity == 0.0)
        {
            velocity = Velocity;
        }
        double moveTime = Math.Abs(Position - currentPos) / velocity;
        for (int i = 0; i < 20; i++)
        {
            position = currentPos + (Position - currentPos) / 20 * (i + 1);
            torque = Torque / (20 - i);
            velocity = Velocity;
            await Task.Delay((int)(moveTime * 1000 / 20));
        }
        torque = Torque;
        status = "Idle";
    }
}
```

---

## 快速开始：两种方式添加设备

### 方式 1：使用注册表编辑器（推荐）

推荐使用 Uni-Lab-OS 自带的可视化编辑器，它能自动分析您的设备驱动并生成大部分配置：

**步骤：**

1. 启动 Uni-Lab-OS
2. 在浏览器中打开"注册表编辑器"页面
3. 选择您的 Python 设备驱动文件
4. 点击"分析文件"，让系统读取类信息
5. 填写基本信息（设备描述、图标等）
6. 点击"生成注册表"，复制生成的内容
7. 保存到 `devices/` 目录下

**优点：**

- 自动识别设备属性和方法
- 可视化界面，易于操作
- 自动生成完整配置
- 减少手动配置错误

### 方式 2：手动编写注册表（简化版）

如果需要手动编写，只需要提供两个必需字段，系统会自动补全其余内容：

**最小配置示例：**

```yaml
my_device: # 设备唯一标识符
  class:
    module: unilabos.devices.your_module.my_device:MyDevice # Python 类路径
    type: python # 驱动类型
```

**注册表文件位置：**

- 默认路径：`unilabos/registry/devices`
- 自定义路径：启动时使用 `--registry_path` 参数指定
- 可将多个设备写在同一个 YAML 文件中

**系统自动生成的内容：**

系统会自动分析您的 Python 驱动类并生成：

- `status_types`：从 `@topic_config` 装饰的 `@property` 或方法自动识别状态属性
- `action_value_mappings`：从类方法自动生成动作映射
- `init_param_schema`：从 `__init__` 方法分析初始化参数
- `schema`：前端显示用的属性类型定义

**完整结构概览：**

```yaml
my_device:
  class:
    module: unilabos.devices.your_module.my_device:MyDevice
    type: python
    status_types: {} # 自动生成
    action_value_mappings: {} # 自动生成
  description: '' # 可选：设备描述
  icon: '' # 可选：设备图标
  init_param_schema: {} # 自动生成
  schema: {} # 自动生成
```

> 💡 **提示：** 详细的注册表编写指南和高级配置，请参考 {doc}`03_add_device_registry`。

---

## Python 类结构要求

Uni-Lab 设备驱动是一个 Python 类，需要遵循以下结构：

```python
from typing import Dict, Any
from unilabos.registry.decorators import device, topic_config

@device(id="my_device", category=["general"], description="My Device")
class MyDevice:
    """设备类文档字符串

    说明设备的功能、连接方式等
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化设备

        Args:
            config: 配置字典，来自图文件或注册表
        """
        self.port = config.get('port', '/dev/ttyUSB0')
        self.baudrate = config.get('baudrate', 9600)
        self._status = "idle"
        # 初始化硬件连接

    @property
    @topic_config()  # 必须添加 @topic_config 才会广播
    def status(self) -> str:
        """设备状态（通过 @topic_config 广播）"""
        return self._status

    def my_action(self, param: float) -> Dict[str, Any]:
        """执行动作

        Args:
            param: 参数说明

        Returns:
            {"success": True, "result": ...}
        """
        # 执行设备操作
        return {"success": True}
```

## 状态属性 vs 动作方法

### 状态属性（@property + @topic_config）

状态属性需要同时使用 `@property` 和 `@topic_config` 装饰器才会被识别并定期广播：

```python
from unilabos.registry.decorators import topic_config

@property
@topic_config()  # 必须添加，否则不会广播
def temperature(self) -> float:
    """当前温度"""
    return self._read_temperature()

@property
@topic_config(period=2.0)  # 可自定义发布周期（秒）
def status(self) -> str:
    """设备状态: idle, running, error"""
    return self._status

@property
@topic_config(name="ready")  # 可自定义发布名称
def is_ready(self) -> bool:
    """设备是否就绪"""
    return self._status == "idle"
```

也可以使用普通方法（非 @property）配合 `@topic_config`：

```python
@topic_config(period=10.0)
def get_sensor_data(self) -> Dict[str, float]:
    """获取传感器数据（get_ 前缀会自动去除，发布名为 sensor_data）"""
    return {"temp": self._temp, "humidity": self._humidity}
```

**`@topic_config` 参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `period` | float | 5.0 | 发布周期（秒） |
| `print_publish` | bool | 节点默认 | 是否打印发布日志 |
| `qos` | int | 10 | QoS 深度 |
| `name` | str | None | 自定义发布名称 |

**发布名称优先级**：`@topic_config(name=...)` > `get_` 前缀去除 > 方法名

**特点**:

- 必须使用 `@topic_config` 装饰器
- 支持 `@property` 和普通方法
- 添加到注册表的 `status_types`
- 定期发布到 ROS2 topic

> **⚠️ 重要：** 仅有 `@property` 装饰器而没有 `@topic_config` 的属性**不会**被广播。这是一个 Breaking Change。

### 动作方法

动作方法是设备可以执行的操作：

```python
def start_heating(self, target_temp: float, rate: float = 1.0) -> Dict[str, Any]:
    """开始加热

    Args:
        target_temp: 目标温度(°C)
        rate: 升温速率(°C/min)

    Returns:
        {"success": bool, "message": str}
    """
    self._status = "heating"
    self._target_temp = target_temp
    # 发送命令到硬件
    return {"success": True, "message": f"Heating to {target_temp}°C"}

async def async_operation(self, duration: float) -> Dict[str, Any]:
    """异步操作（长时间运行）

    Args:
        duration: 持续时间(秒)
    """
    # 使用 self.sleep 而不是 asyncio.sleep（ROS2 异步机制）
    await self.sleep(duration)
    return {"success": True}
```

**特点**:

- 普通方法或 async 方法
- 返回 Dict 类型的结果
- 自动注册为 ROS2 Action
- 支持参数和返回值

### 返回值设计指南

> **⚠️ 重要：返回值会自动显示在前端**
>
> 动作方法的返回值（字典）会自动显示在 Web 界面的工作流执行结果中。因此，**强烈建议**设计结构化、可读的返回值字典。

**推荐的返回值结构：**

```python
def my_action(self, param: float) -> Dict[str, Any]:
    """执行操作"""
    try:
        # 执行操作...
        result = self._do_something(param)

        return {
            "success": True,              # 必需：操作是否成功
            "message": "操作完成",          # 推荐：用户友好的消息
            "result": result,             # 可选：具体结果数据
            "param_used": param,          # 可选：记录使用的参数
            # 其他有用的信息...
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "操作失败"
        }
```

**最佳实践示例（参考 `host_node.test_latency`）：**

```python
def test_latency(self) -> Dict[str, Any]:
    """测试网络延迟

    返回值会在前端显示，包含详细的测试结果
    """
    # 执行测试...
    avg_rtt_ms = 25.5
    avg_time_diff_ms = 10.2
    test_count = 5

    # 返回结构化的测试结果
    return {
        "status": "success",                    # 状态标识
        "avg_rtt_ms": avg_rtt_ms,              # 平均往返时间
        "avg_time_diff_ms": avg_time_diff_ms,  # 平均时间差
        "max_time_error_ms": 5.3,              # 最大误差
        "task_delay_ms": 15.7,                 # 任务延迟
        "test_count": test_count,              # 测试次数
    }
```

**前端显示效果：**

当用户在 Web 界面执行工作流时，返回的字典会以 JSON 格式显示在结果面板中：

```json
{
  "status": "success",
  "avg_rtt_ms": 25.5,
  "avg_time_diff_ms": 10.2,
  "max_time_error_ms": 5.3,
  "task_delay_ms": 15.7,
  "test_count": 5
}
```

**返回值设计建议：**

1. **始终包含 `success` 字段**：布尔值，表示操作是否成功
2. **包含 `message` 字段**：字符串，提供用户友好的描述
3. **使用有意义的键名**：使用描述性的键名（如 `avg_rtt_ms` 而不是 `v1`）
4. **包含单位**：在键名中包含单位（如 `_ms`、`_ml`、`_celsius`）
5. **记录重要参数**：返回使用的关键参数值，便于追溯
6. **错误信息详细**：失败时包含 `error` 字段和详细的错误描述
7. **避免返回大数据**：不要返回大型数组或二进制数据，这会影响前端性能

**错误处理示例：**

```python
def risky_operation(self, param: float) -> Dict[str, Any]:
    """可能失败的操作"""
    if param < 0:
        return {
            "success": False,
            "error": "参数不能为负数",
            "message": f"无效参数: {param}",
            "param": param
        }

    try:
        result = self._execute(param)
        return {
            "success": True,
            "message": "操作成功",
            "result": result,
            "param": param
        }
    except IOError as e:
        return {
            "success": False,
            "error": "通信错误",
            "message": str(e),
            "device_status": self._status
        }
```

## 特殊参数类型：ResourceSlot 和 DeviceSlot

Uni-Lab 提供特殊的参数类型，用于在方法中声明需要选择资源或设备。

### 导入类型

```python
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from typing import List
```

### ResourceSlot - 资源选择

用于需要选择物料资源的场景：

```python
def pipette_liquid(
    self,
    source: ResourceSlot,              # 单个源容器
    target: ResourceSlot,              # 单个目标容器
    volume: float
) -> Dict[str, Any]:
    """从源容器吸取液体到目标容器

    Args:
        source: 源容器（前端会显示资源选择下拉框）
        target: 目标容器（前端会显示资源选择下拉框）
        volume: 体积(μL)
    """
    print(f"Pipetting {volume}μL from {source.id} to {target.id}")
    return {"success": True}
```

**多选示例**:

```python
def mix_multiple(
    self,
    containers: List[ResourceSlot],    # 多个容器选择
    speed: float
) -> Dict[str, Any]:
    """混合多个容器

    Args:
        containers: 容器列表（前端会显示多选下拉框）
        speed: 混合速度
    """
    for container in containers:
        print(f"Mixing {container.name}")
    return {"success": True}
```

### DeviceSlot - 设备选择

用于需要选择其他设备的场景：

```python
def coordinate_with_device(
    self,
    other_device: DeviceSlot,          # 单个设备选择
    command: str
) -> Dict[str, Any]:
    """与另一个设备协同工作

    Args:
        other_device: 协同设备（前端会显示设备选择下拉框）
        command: 命令
    """
    print(f"Coordinating with {other_device.name}")
    return {"success": True}
```

**多设备示例**:

```python
def sync_devices(
    self,
    devices: List[DeviceSlot],         # 多个设备选择
    sync_signal: str
) -> Dict[str, Any]:
    """同步多个设备

    Args:
        devices: 设备列表（前端会显示多选下拉框）
        sync_signal: 同步信号
    """
    for dev in devices:
        print(f"Syncing {dev.name}")
    return {"success": True}
```

### 完整示例：液体处理工作站

```python
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from typing import List, Dict, Any

class LiquidHandler:
    """液体处理工作站"""

    def __init__(self, config: Dict[str, Any]):
        self.simulation = config.get('simulation', False)
        self._status = "idle"

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    def transfer_liquid(
        self,
        source: ResourceSlot,               # 源容器选择
        target: ResourceSlot,               # 目标容器选择
        volume: float,
        tip: ResourceSlot = None            # 可选的枪头选择
    ) -> Dict[str, Any]:
        """转移液体

        前端效果：
        - source: 下拉框，列出所有可用容器
        - target: 下拉框，列出所有可用容器
        - volume: 数字输入框
        - tip: 下拉框（可选），列出所有枪头
        """
        self._status = "transferring"

        # source和target会被解析为实际的资源对象
        print(f"Transferring {volume}μL")
        print(f"  From: {source.id} ({source.name})")
        print(f"  To: {target.id} ({target.name})")

        if tip:
            print(f"  Using tip: {tip.id}")

        # 执行实际的液体转移
        # ...

        self._status = "idle"
        return {
            "success": True,
            "volume_transferred": volume,
            "source_id": source.id,
            "target_id": target.id
        }

    def multi_dispense(
        self,
        source: ResourceSlot,               # 单个源
        targets: List[ResourceSlot],        # 多个目标
        volumes: List[float]
    ) -> Dict[str, Any]:
        """从一个源分配到多个目标

        前端效果：
        - source: 单选下拉框
        - targets: 多选下拉框（可选择多个容器）
        - volumes: 数组输入（每个目标对应一个体积）
        """
        results = []
        for target, vol in zip(targets, volumes):
            print(f"Dispensing {vol}μL to {target.name}")
            results.append({
                "target": target.id,
                "volume": vol
            })

        return {
            "success": True,
            "dispense_results": results
        }

    def test_with_balance(
        self,
        target: ResourceSlot,               # 容器
        balance: DeviceSlot                 # 天平设备
    ) -> Dict[str, Any]:
        """使用天平测量容器

        前端效果：
        - target: 容器选择下拉框
        - balance: 设备选择下拉框（仅显示天平类型）
        """
        print(f"Weighing {target.name} on {balance.name}")

        # 可以调用balance的方法
        # weight = balance.get_weight()

        return {
            "success": True,
            "container": target.id,
            "balance_used": balance.id
        }
```

### 工作原理

#### 1. 类型识别

注册表扫描方法签名时：

```python
def my_method(self, resource: ResourceSlot, device: DeviceSlot):
    pass
```

系统识别到`ResourceSlot`和`DeviceSlot`类型。

#### 2. 自动添加 placeholder_keys

在注册表中自动生成：

```yaml
my_device:
  class:
    action_value_mappings:
      my_method:
        goal:
          resource: resource
          device: device
        placeholder_keys:
          resource: unilabos_resources # 自动添加！
          device: unilabos_devices # 自动添加！
```

#### 3. 前端 UI 生成

- `unilabos_resources`: 渲染为资源选择下拉框
- `unilabos_devices`: 渲染为设备选择下拉框

#### 4. 运行时解析

用户选择资源/设备后，实际调用时会传入完整的资源/设备对象：

```python
# 用户在前端选择了 plate_1
# 运行时，source参数会收到完整的Resource对象
source.id        # "plate_1"
source.name      # "96孔板"
source.type      # "resource"
source.class_    # "corning_96_wellplate_360ul_flat"
```

## 支持的通信方式

### 1. 串口（Serial）

```python
import serial

class SerialDevice:
    def __init__(self, config: Dict[str, Any]):
        self.port = config['port']
        self.baudrate = config.get('baudrate', 9600)
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=1
        )

    def send_command(self, cmd: str) -> str:
        """发送命令并读取响应"""
        self.ser.write(f"{cmd}\r\n".encode())
        response = self.ser.readline().decode().strip()
        return response

    def __del__(self):
        if hasattr(self, 'ser') and self.ser.is_open:
            self.ser.close()
```

### 2. TCP/IP Socket

```python
import socket

class TCPDevice:
    def __init__(self, config: Dict[str, Any]):
        self.host = config['host']
        self.port = config['port']
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

    def send_command(self, cmd: str) -> str:
        self.sock.sendall(cmd.encode())
        response = self.sock.recv(1024).decode()
        return response
```

### 3. Modbus

```python
from pymodbus.client import ModbusTcpClient

class ModbusDevice:
    def __init__(self, config: Dict[str, Any]):
        self.host = config['host']
        self.port = config.get('port', 502)
        self.client = ModbusTcpClient(self.host, port=self.port)
        self.client.connect()

    def read_register(self, address: int) -> int:
        result = self.client.read_holding_registers(address, 1)
        return result.registers[0]

    def write_register(self, address: int, value: int):
        self.client.write_register(address, value)
```

### 4. OPC UA

```python
from opcua import Client

class OPCUADevice:
    def __init__(self, config: Dict[str, Any]):
        self.url = config['url']
        self.client = Client(self.url)
        self.client.connect()

    def read_node(self, node_id: str):
        node = self.client.get_node(node_id)
        return node.get_value()

    def write_node(self, node_id: str, value):
        node = self.client.get_node(node_id)
        node.set_value(value)
```

### 5. HTTP/RPC

```python
import requests

class HTTPDevice:
    def __init__(self, config: Dict[str, Any]):
        self.base_url = config['url']
        self.auth_token = config.get('token')

    def send_command(self, endpoint: str, data: Dict) -> Dict:
        url = f"{self.base_url}/{endpoint}"
        headers = {'Authorization': f'Bearer {self.auth_token}'}
        response = requests.post(url, json=data, headers=headers)
        return response.json()
```

## 异步 vs 同步方法

### 同步方法（适合快速操作）

```python
def quick_operation(self, param: float) -> Dict[str, Any]:
    """快速操作，立即返回"""
    result = self._do_something(param)
    return {"success": True, "result": result}
```

### 异步方法（适合耗时操作）

```python
async def long_operation(self, duration: float) -> Dict[str, Any]:
    """长时间运行的操作"""
    self._status = "running"

    # 使用 ROS2 提供的 sleep 方法（而不是 asyncio.sleep）
    await self.sleep(duration)

    # 可以在过程中发送feedback
    # 需要配合ROS2 Action的feedback机制

    self._status = "idle"
    return {"success": True, "duration": duration}
```

> **⚠️ 重要提示：ROS2 异步机制 vs Python asyncio**
>
> Uni-Lab 的设备驱动虽然使用 `async def` 语法，但**底层是 ROS2 的异步机制，而不是 Python 的 asyncio**。
>
> **不能使用的 asyncio 功能：**
>
> - ❌ `asyncio.sleep()` - 会导致 ROS2 事件循环阻塞
> - ❌ `asyncio.create_task()` - 任务不会被 ROS2 正确调度
> - ❌ `asyncio.gather()` - 无法与 ROS2 集成
> - ❌ 其他 asyncio 标准库函数
>
> **应该使用的方法（继承自 BaseROS2DeviceNode）：**
>
> - ✅ `await self.sleep(seconds)` - ROS2 兼容的睡眠
> - ✅ `await self.create_task(func, **kwargs)` - ROS2 兼容的任务创建
> - ✅ ROS2 的 Action/Service 回调机制
>
> **示例：**
>
> ```python
> async def complex_operation(self, duration: float) -> Dict[str, Any]:
>     """正确使用 ROS2 异步方法"""
>     self._status = "processing"
>
>     # ✅ 正确：使用 self.sleep
>     await self.sleep(duration)
>
>     # ✅ 正确：创建并发任务
>     task = await self.create_task(self._background_work)
>
>     # ❌ 错误：不要使用 asyncio
>     # await asyncio.sleep(duration)  # 这会导致问题！
>     # task = asyncio.create_task(...)  # 这也不行！
>
>     self._status = "idle"
>     return {"success": True}
>
> async def _background_work(self):
>     """后台任务"""
>     await self.sleep(1.0)
>     self.lab_logger().info("Background work completed")
> ```
>
> **为什么不能混用？**
>
> ROS2 使用 `rclpy` 的事件循环来管理所有异步操作。如果使用 `asyncio` 的函数，这些操作会在不同的事件循环中运行，导致：
>
> - ROS2 回调无法正确执行
> - 任务可能永远不会完成
> - 程序可能死锁或崩溃
>
> **参考实现：**
>
> `BaseROS2DeviceNode` 提供的方法定义（`base_device_node.py:563-572`）：
>
> ```python
> async def sleep(self, rel_time: float, callback_group=None):
>     """ROS2 兼容的异步睡眠"""
>     if callback_group is None:
>         callback_group = self.callback_group
>     await ROS2DeviceNode.async_wait_for(self, rel_time, callback_group)
>
> @classmethod
> async def create_task(cls, func, trace_error=True, **kwargs) -> Task:
>     """ROS2 兼容的任务创建"""
>     return ROS2DeviceNode.run_async_func(func, trace_error, **kwargs)
> ```

## 错误处理

### 基本错误处理

```python
def operation_with_error_handling(self, param: float) -> Dict[str, Any]:
    """带错误处理的操作"""
    try:
        result = self._risky_operation(param)
        return {
            "success": True,
            "result": result
        }
    except ValueError as e:
        return {
            "success": False,
            "error": "Invalid parameter",
            "message": str(e)
        }
    except IOError as e:
        self._status = "error"
        return {
            "success": False,
            "error": "Communication error",
            "message": str(e)
        }
```

### 自定义异常

```python
class DeviceError(Exception):
    """设备错误基类"""
    pass

class DeviceNotReadyError(DeviceError):
    """设备未就绪"""
    pass

class DeviceTimeoutError(DeviceError):
    """设备超时"""
    pass

class MyDevice:
    def operation(self) -> Dict[str, Any]:
        if self._status != "idle":
            raise DeviceNotReadyError(f"Device is {self._status}")

        # 执行操作
        return {"success": True}
```

## 最佳实践

### 1. 使用 `@device` 装饰器标识设备类

```python
from unilabos.registry.decorators import device

@device(id="my_device", category=["heating"], description="My Heating Device", icon="heater.webp")
class MyDevice:
    ...
```

- `id`：设备唯一标识符，用于注册表匹配
- `category`：分类列表，前端用于分组显示
- `description`：设备描述
- `icon`：图标文件名（可选）

### 2. 使用 `@topic_config` 声明需要广播的状态

```python
from unilabos.registry.decorators import topic_config

# ✓ @property + @topic_config → 会广播
@property
@topic_config(period=2.0)
def temperature(self) -> float:
    return self._temp

# ✓ 普通方法 + @topic_config → 会广播（get_ 前缀自动去除）
@topic_config(period=10.0)
def get_sensor_data(self) -> Dict[str, float]:
    return {"temp": self._temp}

# ✓ 使用 name 参数自定义发布名称
@property
@topic_config(name="ready")
def is_ready(self) -> bool:
    return self._status == "idle"

# ✗ 仅有 @property，没有 @topic_config → 不会广播
@property
def internal_state(self) -> str:
    return self._state
```

> **注意：** 与 `@property` 连用时，`@topic_config` 必须放在 `@property` 下面。

### 3. 类型注解

```python
from typing import Dict, Any, Optional, List

def method(
    self,
    param1: float,
    param2: str,
    optional_param: Optional[int] = None
) -> Dict[str, Any]:
    """完整的类型注解有助于自动生成注册表"""
    pass
```

### 4. 文档字符串

```python
def method(self, param: float) -> Dict[str, Any]:
    """方法简短描述

    更详细的说明...

    Args:
        param: 参数说明，包括单位和范围

    Returns:
        Dict包含:
        - success (bool): 是否成功
        - result (Any): 结果数据

    Raises:
        DeviceError: 错误情况说明
    """
    pass
```

### 5. 配置验证

```python
def __init__(self, config: Dict[str, Any]):
    # 验证必需参数
    required = ['port', 'baudrate']
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config: {key}")

    self.port = config['port']
    self.baudrate = config['baudrate']
```

### 6. 资源清理

```python
def __del__(self):
    """析构函数，清理资源"""
    if hasattr(self, 'connection') and self.connection:
        self.connection.close()
```

### 7. 设计前端友好的返回值

**记住：返回值会直接显示在 Web 界面**

```python
import time

def measure_temperature(self) -> Dict[str, Any]:
    """测量温度

    ✅ 好的返回值设计：
    - 包含 success 状态
    - 使用描述性键名
    - 在键名中包含单位
    - 记录测量时间
    """
    temp = self._read_temperature()

    return {
        "success": True,
        "temperature_celsius": temp,      # 键名包含单位
        "timestamp": time.time(),          # 记录时间
        "sensor_status": "normal",         # 额外状态信息
        "message": f"温度测量完成: {temp}°C"  # 用户友好的消息
    }

def bad_example(self) -> Dict[str, Any]:
    """❌ 不好的返回值设计"""
    return {
        "s": True,          # ❌ 键名不明确
        "v": 25.5,          # ❌ 没有说明单位
        "t": 1234567890,    # ❌ 不清楚是什么时间戳
    }
```

**参考 `host_node.test_latency` 方法**（第 1216-1340 行），它返回详细的测试结果，在前端清晰显示：

```python
return {
    "status": "success",
    "avg_rtt_ms": 25.5,            # 有意义的键名 + 单位
    "avg_time_diff_ms": 10.2,
    "max_time_error_ms": 5.3,
    "task_delay_ms": 15.7,
    "test_count": 5,               # 记录重要信息
}
```

## 下一步

看完本文档后，建议继续阅读：

- {doc}`add_action` - 了解如何添加新的动作指令
- {doc}`add_yaml` - 学习如何编写和完善 YAML 注册表

进阶主题：

- {doc}`03_add_device_registry` - 了解如何配置注册表
- {doc}`04_add_device_testing` - 学习如何测试设备
- {doc}`add_old_device` - 没有 SDK 时如何开发设备驱动

## 参考

- [Python 类型注解](https://docs.python.org/3/library/typing.html)
- [ROS2 rclpy 异步编程](https://docs.ros.org/en/humble/Tutorials/Intermediate/Writing-an-Action-Server-Client/Py.html) - Uni-Lab 使用 ROS2 的异步机制
- [串口通信](https://pyserial.readthedocs.io/)

> **注意：** 虽然设备驱动使用 `async def` 语法，但请**不要参考** Python 标准的 [asyncio 文档](https://docs.python.org/3/library/asyncio.html)。Uni-Lab 使用的是 ROS2 的异步机制，两者不兼容。请使用 `self.sleep()` 和 `self.create_task()` 等 BaseROS2DeviceNode 提供的方法。
