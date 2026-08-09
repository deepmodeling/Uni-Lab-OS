# Uni-Lab-OS 设备接入指南（AI 专用·自包含版）

> **本文件是完全自包含的。** 即使你无法访问 Uni-Lab-OS 仓库，也能根据本指南正确生成设备驱动。
> 如果你能访问仓库，建议搜索 `unilabos/registry/devices/` 目录获取最新的已有设备接口。
> 最新版本也可通过 GitHub 获取：https://github.com/dptech-corp/Uni-Lab-OS/tree/main/unilabos/registry/devices/

端到端向导，通过**设备类别（物模型）** 和 **通信协议** 两个维度引导设备接入。

---

## 第一步：选择设备类别（物模型）

每种设备类别有标准的属性和动作接口。向用户确认以下信息：

**Q1: 设备属于哪个类别？**

| 类别 ID | 说明 | 标准属性 | 标准动作 |
|---|---|---|---|
| `temperature` | 加热/冷却/温控 | `temp`, `temp_target`, `status` | `set_temperature`, `stop` |
| `pump_and_valve` | 泵、阀门、注射器 | 见下方子类型表 | 见下方子类型表 |
| `motor` | 电机、步进马达 | `position`, `status` | `enable`, `move_position`, `move_speed`, `stop` |
| `heaterstirrer` | 加热搅拌一体机 | `temp`, `stir_speed`, `status` | `set_temperature`, `stir`, `stop` |
| `balance` | 天平/称重 | `weight`, `unit`, `status` | `tare`, `read_weight` |
| `sensor` | 传感器（液位/温度/...） | `value`, `level`, `status` | `read_value`, `set_threshold` |
| `liquid_handling` | 液体处理机器人 | `status`, `deck_state` | `transfer_liquid`, `aspirate`, `dispense` |
| `robot_arm` | 机械臂 | `arm_pose`, `arm_status` | `moveit_task`, `pick_and_place` |
| `workstation` | 工作站（组合设备） | `workflow_sequence`, `material_info` | `create_order`, `scheduler_start`/`stop` |
| `virtual` | 虚拟/模拟设备 | 按模拟的真实设备定义 | 按模拟的真实设备定义 |
| `custom` | 不属于以上任何类别 | 用户自定义 | 用户自定义 |

**pump_and_valve 子类型：** 该类别包含差异较大的子类型，下表仅列出**最小通用接口**。具体项目中可能有更多属性和动作，由第四步（对齐同类设备接口）动态发现。

| 子类型 | 最小通用属性 | 最小通用动作 | 单位约定 |
|---|---|---|---|
| 注射泵（syringe pump） | `status`, `valve_position`, `position`(mL) | `initialize`, `set_valve_position`, `set_position`(mL), `pull_plunger`(mL), `push_plunger`(mL), `stop_operation` | 体积=mL, 速度=mL/s |
| 电磁阀（solenoid valve） | `status`, `valve_position` | `open`, `close`, `set_valve_position` | — |
| 蠕动泵（peristaltic pump） | `status`, `speed` | `start`, `stop`, `set_speed` | 流速=mL/min |

**单位约定（重要）：** 设备对外暴露的属性和动作参数**必须使用用户友好的物理单位**，不能使用原始步数或寄存器值。驱动内部负责在物理单位和硬件原始值之间转换。

| 类别 | 位置/体积 | 速度 | 温度 | 其他 |
|---|---|---|---|---|
| pump_and_valve (注射泵) | **mL** | **mL/s** | — | — |
| pump_and_valve (蠕动泵) | — | **mL/min** | — | — |
| motor | **mm** 或 **度** | **mm/s** 或 **RPM** | — | — |
| temperature | — | — | **°C** | — |
| balance | **g** 或 **mg** | — | — | — |
| sensor | 按传感器物理量定 | — | — | — |

**Q2: 设备英文名称？** （如 `my_heater`，用于类名和文件名）

---

## 第二步：选择通信协议

**Q3: 设备使用什么通信协议？**

| 协议 | config 参数 | 依赖包 | UniLab 现有抽象 |
|---|---|---|---|
| **Serial (RS232/RS485)** | `port`, `baudrate` | `pyserial` | 直接使用 `serial.Serial` |
| **Modbus RTU** | `port`, `baudrate`, `slave_id` | `pymodbus` | `device_comms/modbus_plc/`（RTUClient） |
| **Modbus TCP** | `host`, `port`, `slave_id` | `pymodbus` | `device_comms/modbus_plc/`（TCPClient） |
| **TCP Socket** | `host`, `port` | stdlib | 直接使用 `socket` |
| **HTTP API** | `url`, `token` | `requests` | `device_comms/rpc.py`（BaseRequest） |
| **OPC UA** | `url` | `opcua` | `device_comms/opcua_client/`（OpcUaClient） |
| **无通信（虚拟）** | 无 | 无 | 无 |

---

## 第三步：收集指令协议（关键）

物模型定义了设备"应该做什么"，通信协议定义了"用什么方式通信"，但**具体发什么指令**是硬件厂商私有的，AI 无法凭空生成。必须从以下来源获取：

**Q4: 指令协议的信息来源？**

| 来源 | AI 处理方式 | 示例 |
|---|---|---|
| **现成 SDK/驱动代码** | 读取代码，提取指令逻辑，包装进 UniLab 框架 | 用户提供 `.py` 文件或 pip 包名 |
| **协议文档/手册** | 读取文档（PDF/图片/文本），解析指令格式 | 用户提供通信协议手册 |
| **用户口述** | 按描述实现指令编解码 | "设温指令是 `01 06 00 0B` + 温度值 + CRC" |
| **标准协议** | 直接使用标准实现 | 标准 Modbus 寄存器表、SCPI 指令集 |
| **HTTP API 文档** | 读取 API 文档，映射到动作方法 | Swagger/OpenAPI 文档 |

**根据来源执行对应流程：**

### 场景 A：用户提供了现成 SDK 或驱动代码

1. 读取用户提供的驱动代码
2. 分析其中的通信逻辑：初始化、指令编码、响应解码
3. 将核心逻辑包装进 UniLab 设备类框架（加入 `self.data` 状态管理、`@property` 属性等）

### 场景 B：用户提供了协议文档/手册

1. 读取文档（支持 PDF、图片、文本）
2. 从文档中提取：
   - **指令格式**（文本型 `SET_TEMP 100\r\n`、二进制帧、Modbus 寄存器地址等）
   - **响应格式**（如何解析返回数据）
   - **寄存器/地址映射表**（哪个地址对应什么功能）
3. 实现指令编解码方法

### 场景 C：用户口头描述指令

逐个确认每个物模型动作对应的具体指令：

```
对于第一步选定的每个标准动作，询问：
- set_temperature → 硬件指令是什么？（如 Modbus 写寄存器 0x000B）
- read_temperature → 硬件指令是什么？（如 发送 0xfe 0xA2 0x00 0x00）
- stop → 硬件指令是什么？
```

### 场景 D：虚拟设备（无实际通信）

跳过此步骤，动作方法中直接模拟行为（修改 `self.data`，用 `sleep` 模拟耗时）。

---

## 第四步：对齐同类设备接口（强制）

第一步给出的是**最小通用接口**。本步骤在此基础上，对照仓库现有注册表，**补充**额外的属性和动作，确保新驱动能无缝替换同类设备。

> **此步骤是强制性的，不可跳过。** 跳过此步会导致参数名不匹配、status 字符串不一致、缺失属性等问题，使设备无法在工作流中正确运行。

**执行步骤：**

1. 查阅下方「现有设备接口快照」章节，找到同类别的已有设备接口。如果你能访问仓库，建议直接搜索 `unilabos/registry/devices/` 目录获取最新版本。

2. 提取已有设备的**额外接口**（超出第一步最小通用接口的部分）：
   - **status_types** — 是否有额外属性？
   - **action_value_mappings** — 是否有额外动作？**逐个记录参数名和类型**
   - **status 字符串** — 已有设备用的是什么值？（如 `"Idle"` / `"Busy"` 还是中文？）
   - **单位** — 确认单位是否与第一步约定一致

3. 对齐决策：
   - 新驱动**必须实现**第一步的最小通用接口
   - 如果已有设备有额外属性/动作，**判断新硬件是否支持**：
     - 硬件支持 → **必须实现**（保持接口一致）
     - 硬件不支持 → 可提供合理的默认值或空实现，但属性必须存在
   - **参数名必须与已有设备完全一致**（这是最常出错的地方）
   - **status 字符串值必须与已有设备一致**
   - 可以**增加**新的属性和动作，但最小通用接口不能缺少

4. 如果同类别下没有已有设备，跳过对齐，按第一步的最小通用接口即可。

**对齐验证清单（完成第五步后必须逐项确认）：**

```
- [ ] 所有动作方法的参数名与已有设备完全一致（如 volume 而非 volume_ml）
- [ ] status 属性返回的字符串值与已有设备一致（如 "Idle" 而非 "就绪"）
- [ ] 已有设备的所有 status_types 字段在新驱动中都有对应 @property
- [ ] 已有设备的所有非 auto- 前缀的 action 在新驱动中都有对应方法
- [ ] self.data 在 __init__ 中已预填充所有属性字段的默认值
- [ ] 串口/二进制协议的响应解析先定位帧起始标记，不使用硬编码索引
```

---

## 第五步：创建设备驱动文件

文件路径：`unilabos/devices/<category>/<device_name>.py`

### 核心结构

设备类 = 物模型标准接口 + 通信协议层 + 具体指令编解码：

```python
import logging
import time as time_module
from typing import Dict, Any

try:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
except ImportError:
    BaseROS2DeviceNode = None


class MyDevice:
    """设备描述"""

    _ros_node: "BaseROS2DeviceNode"

    def __init__(self, device_id: str = None, config: Dict[str, Any] = None, **kwargs):
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')

        self.device_id = device_id or "unknown_device"
        self.config = config or {}
        self.logger = logging.getLogger(f"MyDevice.{self.device_id}")

        # self.data 必须预填充所有 @property 对应的字段
        # status 字符串必须与同类已有设备一致（查看第四步）
        self.data = {
            "status": "Idle",
            # "其他属性": 默认值,  ← 每个 @property 都要有对应的键
        }

        # --- 通信层初始化（按第二步选择的协议填入）---
        # self.ser = serial.Serial(...)
        # self.client = ModbusTcpClient(...)

    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        self.data.update({"status": "Idle"})
        return True

    async def cleanup(self) -> bool:
        self.data.update({"status": "Offline"})
        return True

    # --- 通信辅助方法（按第三步收集的指令协议实现）---
    # def _send_command(self, cmd: str) -> str: ...

    # --- 物模型标准动作（调用通信辅助方法发送实际指令）---
    # async def set_temperature(self, temp: float, **kwargs) -> bool: ...

    # --- 物模型标准属性 ---
    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### 关键规则

1. **参数类型转换** — 动作参数可能以字符串传入，必须显式 `float()`/`int()` 转换
2. **异步等待** — 使用 `await self._ros_node.sleep()`，**禁止** `asyncio.sleep()`，也**禁止** `time.sleep()`（会阻塞事件循环）
3. **状态存储** — 用 `self.data` 字典存储，`@property` 读取并自动广播
4. **进度反馈** — 长操作需循环更新 `self.data["status"]` 和 `remaining_time`
5. **返回值** — 返回 `bool` 或 `Dict[str, Any]`（含 `success` 字段），会显示在前端

### 禁止事项（严格遵守）

以下是导致设备无法接入的常见错误，**必须逐条检查**：

1. **禁止重命名模板参数** — 模板中的方法参数名（如 `volume`、`position`、`max_velocity`）是接口契约，框架通过参数名分派调用。**绝对不能**加后缀（如 `volume_ml`）、改名（如 `speed_ml_s`）或用其他"更可读"的名字替代。单位信息写在 docstring 中，不写在参数名中。
2. **status 字符串必须与同类已有设备一致** — 如果已有设备使用英文（如 pump_and_valve 的 `"Idle"` / `"Busy"`），新驱动**必须使用相同的字符串**，不能改为中文。上层代码可能通过 `status == "Idle"` 来判断状态。
3. **`self.data` 必须在 `__init__` 中预填充所有属性字段** — 不能用空字典 `{}`。框架在 `initialize()` 之前就可能读取属性值。每个 `@property` 对应的键都必须有初始值。
4. **禁止跳过第四步** — 对齐同类设备接口是强制步骤，不是可选步骤。缺失的属性和动作会导致设备在工作流中不可互换。
5. **禁止用硬编码索引解析串口响应** — RS-485 半双工总线上响应前常有回声/噪声字节。必须先定位帧起始标记（如 `/`、`0xFE`），再用相对偏移解析。否则所有解析方法（错误码、忙闲判断、数据提取）会同时出错，且部分可能歪打正着，造成隐蔽 bug。

### 特殊参数类型

需要前端资源/设备选择器时：

```python
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot

def transfer(self, source: ResourceSlot, target: ResourceSlot, volume: float) -> Dict[str, Any]:
    return {"success": True, "volume": volume}
```

| Python 类型 | 前端效果 |
|---|---|
| `ResourceSlot` | 单选资源下拉框 |
| `List[ResourceSlot]` | 多选资源下拉框 |
| `DeviceSlot` | 单选设备下拉框 |
| `List[DeviceSlot]` | 多选设备下拉框 |

### 设备架构分支

| 场景 | 基类 | 说明 |
|---|---|---|
| 简单设备 | 无基类（纯 Python 类） | 大多数情况 |
| 工作站 | `WorkstationBase` | 组合多个子设备，有 Deck |
| 液体处理 | `LiquidHandlerAbstract` | PyLabRobot 集成 |
| Modbus 设备 | 可用 `device_comms/modbus_plc/` | 节点注册 + 工作流 |
| OPC UA 设备 | 可用 `device_comms/opcua_client/` | 节点发现 + CSV 配置 |

---

## 第六步：创建注册表 YAML

在 `unilabos/registry/devices/` 下创建。

### 最小配置（推荐）

```yaml
my_device:
  class:
    module: unilabos.devices.<category>.<file>:MyDevice
    type: python
```

启动时 `--complete_registry` 自动生成 `status_types`、`action_value_mappings` 等全部字段。

### 手动补充（可选）

```yaml
my_device:
  category:
    - temperature
  description: "我的温控设备"
  class:
    module: unilabos.devices.temperature.my_heater:MyHeater
    type: python
```

### 完整 YAML 结构参考

```yaml
my_device:
  description: "设备描述"
  version: "1.0.0"
  category: [my_category]
  icon: ""
  handles: []
  class:
    module: unilabos.devices.my_category.my_device:MyDevice
    type: python
    status_types:
      status: String      # str → String
      temp: Float64        # float → Float64
      is_running: Bool     # bool → Bool
      position: Int64      # int → Int64
    action_value_mappings:
      my_action:
        type: UniLabJsonCommandAsync   # 或 UniLabJsonCommand
        goal:
          param1: param1
        result:
          success: success
        goal_default:
          param1: 0.0
        handles: {}
        placeholder_keys: {}
        schema:
          title: my_action参数
          type: object
          properties:
            goal:
              type: object
              properties:
                param1:
                  type: number
              required: [param1]
          required: [goal]
```

### Python → ROS 类型映射

| Python | ROS | YAML `status_types` |
|---|---|---|
| `str` | `std_msgs/String` | `String` |
| `bool` | `std_msgs/Bool` | `Bool` |
| `int` | `std_msgs/Int64` | `Int64` |
| `float` | `std_msgs/Float64` | `Float64` |
| `list`/`dict` | `std_msgs/String`（JSON 序列化） | `String` |

---

## 第七步：配置图文件

在实验图文件（JSON）中添加设备节点：

```json
{
    "id": "my_device_1",
    "name": "我的设备",
    "children": [],
    "parent": null,
    "type": "device",
    "class": "my_device",
    "position": {"x": 0, "y": 0, "z": 0},
    "config": {
        "port": "/dev/ttyUSB0",
        "baudrate": 9600
    },
    "data": {}
}
```

`config` 中的参数对应通信协议所需的连接信息，直接传入 `__init__` 的 `config` 字典。

---

## 第八步：验证

```bash
# 1. 模块可导入
python -c "from unilabos.devices.<category>.<file> import <ClassName>"

# 2. 注册表补全（可选）
unilab -g <graph>.json --complete_registry

# 3. 启动测试
unilab -g <graph>.json
```

---

## 工作流清单

```
设备接入进度：
- [ ] 1. 确定设备类别（物模型）+ 单位约定
- [ ] 2. 确定通信协议
- [ ] 3. 收集指令协议（SDK/文档/口述）
- [ ] 4. 对齐同类设备接口（对照快照或搜索注册表）
- [ ] 5. 创建驱动 unilabos/devices/<category>/<file>.py
- [ ] 6. 创建注册表 unilabos/registry/devices/<file>.yaml
- [ ] 7. 配置图文件（如需要）
- [ ] 8. 验证可导入 + 启动测试
```

---

## 现有设备接口快照

> 以下是仓库中已有设备的接口定义，用于第四步对齐。
> 如果你能访问仓库，建议搜索 `unilabos/registry/devices/` 获取最新版本。
> 最新版本也可通过 GitHub 获取：
> https://github.com/dptech-corp/Uni-Lab-OS/tree/main/unilabos/registry/devices/

### pump_and_valve — 注射泵子类型

已有设备：`syringe_pump_with_valve.runze.SY03B-T06` / `SY03B-T08`
驱动类：`unilabos.devices.pump_and_valve.runze_backbone:RunzeSyringePump`

**status_types（属性）：**

| 属性名 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | `"Idle"` / `"Busy"` |
| `valve_position` | `str` | 阀门位置 |
| `position` | `float` | 当前体积 (mL) |
| `max_velocity` | `float` | 最大速度 (mL/s) |
| `mode` | `int` | 运行模式 |
| `plunger_position` | `String` | 活塞位置 |
| `velocity_grade` | `String` | 速度档位 |
| `velocity_init` | `String` | 初始速度 |
| `velocity_end` | `String` | 终止速度 |

**关键动作方法签名（参数名不可修改）：**

```python
def initialize(self)
def set_valve_position(self, position)              # 参数名必须是 position
def set_position(self, position: float, max_velocity: float = None)
def pull_plunger(self, volume: float)                # 参数名必须是 volume
def push_plunger(self, volume: float)                # 参数名必须是 volume
def set_max_velocity(self, velocity: float)
def set_velocity_grade(self, velocity)
def stop_operation(self)
def send_command(self, full_command: str)
def set_baudrate(self, baudrate)
def close(self)
```

### pump_and_valve — 电磁阀子类型

已有设备：`solenoid_valve` / `solenoid_valve.mock`
驱动类：`unilabos.devices.pump_and_valve.solenoid_valve:SolenoidValve`

**status_types：**

| 属性名 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | 状态 |
| `valve_position` | `str` | 阀门位置 |

**关键动作方法签名：**

```python
def open(self)
def close(self)
def set_valve_position(self, position)   # 参数名是 position
def is_open(self)
def is_closed(self)
def send_command(self, command: str)
```

### temperature — 温控设备

已有设备：`dalong_heaterstirrer`（加热搅拌器）
驱动类：`unilabos.devices.temperature.dalong:DalongHeaterStirrer`

**status_types：**

| 属性名 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | 状态 |
| `temp` | `float` | 当前温度 (°C) |
| `temp_target` | `float` | 目标温度 (°C) |
| `stir_speed` | `float` | 搅拌速度 (RPM) |
| `temp_warning` | `float` | 警告温度 (°C) |

### motor — 电机设备

已有设备：`zdt_x42`（闭环步进电机）
驱动类：`unilabos.devices.motor.zdt_x42:ZDTX42Motor`

**status_types：**

| 属性名 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | 状态 |
| `position` | `int` | 当前位置 |

### sensor — 传感器

已有设备：`xkc_level_sensor`（液位传感器）
驱动类：`unilabos.devices.sensor.xkc_level_sensor:XKCLevelSensor`

**status_types：**

| 属性名 | 类型 | 说明 |
|---|---|---|
| `level` | `bool` | 液位状态 |
| `rssi` | `int` | 信号强度 |

---

## 物模型代码模板

### temperature — 温控设备

```python
class MyTemperatureDevice:
    """温控设备：加热器、冷却器、恒温槽等"""

    def __init__(self, device_id=None, config=None, **kwargs):
        # ... 标准 init ...
        self.data = {
            "status": "Idle",
            "temp": 25.0,
            "temp_target": 25.0,
        }

    async def set_temperature(self, temp: float, **kwargs) -> bool:
        """设定目标温度 (°C)"""
        temp = float(temp)
        self.data["temp_target"] = temp
        # >>> 在此填入实际指令 <<<
        return True

    async def stop(self, **kwargs) -> bool:
        self.data["status"] = "Idle"
        # >>> 在此填入实际指令 <<<
        return True

    @property
    def temp(self) -> float:
        return self.data.get("temp", 0.0)

    @property
    def temp_target(self) -> float:
        return self.data.get("temp_target", 0.0)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### pump_and_valve — 注射泵

> **严禁重命名参数！** 下方模板中的参数名（`volume`、`position`、`max_velocity` 等）是接口契约。禁止加后缀（如 ~~`volume_ml`~~）、改名（如 ~~`speed_ml_s`~~）或用其他名字替代。单位信息写在 docstring 里，不写在参数名中。

```python
class MySyringePump:
    """注射泵设备 — 含阀门控制"""

    def __init__(self, device_id=None, config=None, **kwargs):
        # ... 标准 init ...
        self.max_volume = float(config.get("max_volume", 25.0))
        self.total_steps = 6000
        self.data = {
            "status": "Idle",          # 必须用英文 "Idle" / "Busy"
            "valve_position": "I",
            "position": 0.0,           # 当前体积位置 (mL)
            # 第四步可能要求补充更多字段（如 max_velocity, mode 等）
        }

    def initialize(self):
        # >>> 发送初始化指令 <<<
        return response

    def set_valve_position(self, position):
        """设置阀门位置。参数名必须是 position"""
        # >>> 发送阀门指令 <<<
        return response

    def set_position(self, position: float, max_velocity: float = None):
        """移动到绝对体积位置 (mL)。参数名 position / max_velocity 不可修改"""
        pos_step = int(float(position) / self.max_volume * self.total_steps)
        # >>> 发送绝对位置指令 <<<
        return response

    def pull_plunger(self, volume: float):
        """吸液 (mL)。参数名必须是 volume"""
        pos_step = int(float(volume) / self.max_volume * self.total_steps)
        # >>> 发送相对吸液指令 <<<
        return response

    def push_plunger(self, volume: float):
        """排液 (mL)。参数名必须是 volume"""
        pos_step = int(float(volume) / self.max_volume * self.total_steps)
        # >>> 发送相对排液指令 <<<
        return response

    def stop_operation(self):
        # >>> 发送终止指令 <<<
        return response

    def close(self):
        self.hardware_interface.close()

    @property
    def status(self) -> str:
        return self._status  # "Idle" 或 "Busy"

    @property
    def valve_position(self) -> str:
        return self._valve_position

    @property
    def position(self) -> float:
        """当前体积位置 (mL)"""
        return self._position
```

### pump_and_valve — 电磁阀

```python
class MySolenoidValve:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {"status": "Idle", "valve_position": "closed"}

    async def open(self, **kwargs) -> bool:
        return True

    async def close(self, **kwargs) -> bool:
        return True

    async def set_valve_position(self, position: str, **kwargs) -> bool:
        self.data["valve_position"] = str(position)
        return True

    @property
    def valve_position(self) -> str:
        return self.data.get("valve_position", "closed")

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### pump_and_valve — 蠕动泵

```python
class MyPeristalticPump:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {"status": "Idle", "speed": 0.0, "direction": "CW"}

    async def set_speed(self, speed: float, **kwargs) -> bool:
        """设置流速 (mL/min)"""
        self.data["speed"] = float(speed)
        return True

    async def stop(self, **kwargs) -> bool:
        self.data["speed"] = 0.0
        self.data["status"] = "Idle"
        return True

    @property
    def speed(self) -> float:
        return self.data.get("speed", 0.0)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### motor — 电机设备

```python
class MyMotor:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {"status": "Idle", "position": 0, "speed": 0.0}

    async def enable(self, **kwargs) -> bool:
        self.data["status"] = "Enabled"
        return True

    async def move_position(self, position: int, speed: float = 100.0, **kwargs) -> bool:
        position, speed = int(position), float(speed)
        return True

    async def move_speed(self, speed: float, **kwargs) -> bool:
        self.data["speed"] = float(speed)
        return True

    async def stop(self, **kwargs) -> bool:
        self.data["status"] = "Idle"
        self.data["speed"] = 0.0
        return True

    @property
    def position(self) -> int:
        return self.data.get("position", 0)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### heaterstirrer — 加热搅拌

```python
class MyHeaterStirrer:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {
            "status": "Idle", "temp": 25.0, "temp_target": 25.0,
            "stir_speed": 0.0, "is_stirring": False,
        }

    async def set_temperature(self, temp: float, **kwargs) -> bool:
        self.data["temp_target"] = float(temp)
        return True

    async def stir(self, stir_speed: float, stir_time: float = 0, settling_time: float = 0, **kwargs) -> bool:
        self.data["stir_speed"] = float(stir_speed)
        self.data["is_stirring"] = True
        if stir_time > 0:
            start = time_module.time()
            while time_module.time() - start < stir_time:
                self.data["remaining_time"] = max(0, stir_time - (time_module.time() - start))
                await self._ros_node.sleep(1.0)
        self.data["is_stirring"] = False
        return True

    async def stop(self, **kwargs) -> bool:
        self.data.update({"status": "Idle", "stir_speed": 0.0, "is_stirring": False})
        return True

    @property
    def temp(self) -> float:
        return self.data.get("temp", 25.0)

    @property
    def stir_speed(self) -> float:
        return self.data.get("stir_speed", 0.0)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### balance — 天平

```python
class MyBalance:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {"status": "Idle", "weight": 0.0, "unit": "g", "stable": True}

    def read_weight(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "weight_g": self.data["weight"], "stable": self.data["stable"]}

    def tare(self, **kwargs) -> Dict[str, Any]:
        self.data["weight"] = 0.0
        return {"success": True, "message": "去皮完成"}

    @property
    def weight(self) -> float:
        return self.data.get("weight", 0.0)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### sensor — 传感器

```python
class MySensor:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.data = {"status": "Idle", "value": 0.0, "level": False}

    def read_value(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "value": self.data["value"]}

    async def wait_for_level(self, target_level: bool = True, timeout: float = 60.0, **kwargs) -> bool:
        start = time_module.time()
        while time_module.time() - start < float(timeout):
            if self.data["level"] == bool(target_level):
                return True
            await self._ros_node.sleep(0.5)
        return False

    @property
    def value(self) -> float:
        return self.data.get("value", 0.0)

    @property
    def level(self) -> bool:
        return self.data.get("level", False)

    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

---

## 指令协议模式

通信协议解决"用什么方式通信"，指令协议解决"发什么内容"。

### 模式 1：文本指令

```python
def _send_command(self, cmd: str) -> str:
    self.ser.write(f"{cmd}\r\n".encode())
    return self.ser.readline().decode().strip()
```

### 模式 2：自定义二进制帧

```python
def _build_frame(self, func_code: int, data: bytes) -> bytes:
    frame = bytearray([0xFE, func_code]) + bytearray(data)
    while len(frame) < 5:
        frame.append(0x00)
    checksum = sum(frame[1:]) % 256
    frame.append(checksum)
    return bytes(frame)

def _send_frame(self, func_code: int, data: bytes) -> bytes:
    frame = self._build_frame(func_code, data)
    self.ser.write(frame)
    return self.ser.read(6)
```

### 模式 3：Modbus 寄存器读写

```python
REGISTER_MAP = {
    "temp_target": {"addr": 0x000B, "scale": 10},
    "temp_current": {"addr": 0x0001, "scale": 10},
}

def set_temperature(self, temp: float, **kwargs) -> bool:
    temp = float(temp)
    reg = REGISTER_MAP["temp_target"]
    value = int(temp * reg["scale"]) & 0xFFFF
    self.client.write_register(reg["addr"], value, slave=self.slave_id)
    self.data["temp_target"] = temp
    return True
```

### 模式 4：JSON/REST API

```python
API_MAP = {
    "set_temperature": {"method": "POST", "endpoint": "/api/temperature", "body_key": "target"},
    "get_status":      {"method": "GET",  "endpoint": "/api/status"},
}

def set_temperature(self, temp: float, **kwargs) -> bool:
    api = API_MAP["set_temperature"]
    resp = self._post(api["endpoint"], {api["body_key"]: float(temp)})
    return resp.get("success", False)
```

### 模式 5：SDK 封装

```python
from my_device_sdk import DeviceController

class MyDevice:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.controller = DeviceController(port=config.get('port', 'COM1'))
        self.data = {"status": "Idle"}

    def set_temperature(self, temp: float, **kwargs) -> bool:
        self.controller.set_target_temp(float(temp))
        return True
```

---

## 通信协议代码片段

### Serial（RS232 / RS485）

```python
import serial

self.ser = serial.Serial(
    port=self.config.get('port', 'COM1'),
    baudrate=self.config.get('baudrate', 9600),
    timeout=self.config.get('timeout', 1),
)

# cleanup:
if hasattr(self, 'ser') and self.ser.is_open:
    self.ser.close()
```

**串口响应解析健壮性（重要）：** RS-485 半双工总线上，设备响应前经常有前导垃圾字节（TX 回声、总线噪声等）。**禁止用硬编码索引直接解析原始响应**，必须先定位帧起始标记：

```python
# ✗ 错误 — 假设响应从 index 0 开始，前导垃圾字节会导致所有解析偏移
status_byte = ord(response[2])
data = response[3:etx_pos]

# ✓ 正确 — 先找到帧起始标记，再用相对偏移解析
def _normalize_response(self, raw: str, start_marker: str = "/") -> str:
    """去除帧起始标记之前的垃圾字节"""
    pos = raw.find(start_marker)
    return raw[pos:] if pos >= 0 else raw

# 在 _send_command 返回前调用:
resp_str = self._normalize_response(resp_str)
```

同理，二进制帧协议也必须先查找帧头字节（如 `0xFE`），不能假设 `response[0]` 就是帧头。

### Modbus RTU

```python
from pymodbus.client import ModbusSerialClient

self.client = ModbusSerialClient(
    port=self.config.get('port', 'COM1'),
    baudrate=self.config.get('baudrate', 9600),
    timeout=self.config.get('timeout', 1),
)
self.client.connect()
self.slave_id = self.config.get('slave_id', 1)
```

### Modbus TCP

```python
from pymodbus.client import ModbusTcpClient

self.client = ModbusTcpClient(
    host=self.config.get('host', '192.168.1.100'),
    port=self.config.get('port', 502),
)
self.client.connect()
self.slave_id = self.config.get('slave_id', 1)
```

### TCP Socket

```python
import socket

self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
self.sock.settimeout(self.config.get('timeout', 5))
self.sock.connect((self.config['host'], self.config['port']))
```

### HTTP API

```python
import requests

self.base_url = self.config.get('url', 'http://localhost:8080')
self.session = requests.Session()
```

### OPC UA

```python
from opcua import Client

self.opc_client = Client(self.config.get('url', 'opc.tcp://localhost:4840'))
self.opc_client.connect()
```

---

## 常见错误（必读）

以下是历史上导致设备无法接入的真实案例，**生成代码后必须逐条对照检查**：

### 错误 1：重命名模板参数名

```python
# ✗ 错误
async def pull_plunger(self, volume_ml: float, speed_ml_s: float = None, **kwargs):
# ✓ 正确
async def pull_plunger(self, volume: float, **kwargs):

# ✗ 错误
async def set_position(self, position_ml: float, speed_ml_s: float = None, **kwargs):
# ✓ 正确
async def set_position(self, position: float, max_velocity: float = None, **kwargs):

# ✗ 错误
async def set_valve_position(self, valve_position: int, **kwargs):
# ✓ 正确
async def set_valve_position(self, position, **kwargs):
```

### 错误 2：status 字符串使用中文

```python
# ✗ 错误
self.data["status"] = "就绪"
# ✓ 正确
self.data["status"] = "Idle"
```

### 错误 3：self.data 初始化为空字典

```python
# ✗ 错误
self.data = {}
# ✓ 正确
self.data = {"status": "Idle", "valve_position": "I", "position": 0.0, "max_velocity": 0.0}
```

### 错误 4：跳过第四步，缺失已有设备的属性

```python
# ✓ 即使硬件不直接支持，也要提供属性（返回默认值）
@property
def max_velocity(self) -> float:
    return self.data.get("max_velocity", 0.0)
```

### 错误 5：在 async 方法中使用 time.sleep()

```python
# ✗ 错误
time.sleep(0.5)
# ✓ 正确
await self._ros_node.sleep(0.5)
```

### 错误 6：用硬编码索引解析串口响应

```python
# ✗ 错误 — RS-485 响应前有回声/噪声字节时，所有索引偏移，解析全部出错
#   而且 _parse_error / _is_busy 可能歪打正着返回"正确"结果，
#   导致轮询失效（永远认为设备空闲）、错误被吞、状态查询异常
status_byte = ord(response[2])
data = response[3:etx_pos]

# ✓ 正确 — 先定位帧起始标记（如 /、0xFE 等），再用相对偏移
start = response.find("/")
if start >= 0:
    response = response[start:]
status_byte = ord(response[2])
data = response[3:etx_pos]
```

**规则：** 串口协议解析必须先定位帧起始标记，禁止假设 `response[0]` 就是帧头。

---

## 返回值设计

```python
return {
    "success": True,
    "message": "操作完成",
    "temperature_celsius": 25.5,
}
```

---

## 图文件：工作站配置

工作站需要 `deck` 和 `children`：

```json
{
    "nodes": [
        {
            "id": "my_station",
            "type": "device",
            "class": "my_workstation",
            "children": ["my_deck"],
            "config": {},
            "deck": {
                "data": {
                    "_resource_child_name": "my_deck",
                    "_resource_type": "unilabos.resources.my_module:MyDeck"
                }
            }
        },
        {
            "id": "my_deck",
            "type": "deck",
            "class": "MyDeckClass",
            "parent": "my_station",
            "children": [],
            "config": {"type": "MyDeckClass", "setup": true}
        }
    ]
}
```
