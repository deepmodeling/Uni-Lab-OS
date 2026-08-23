# Uni-Lab-OS 设备接入指南（AI 专用·自包含版）

> **本文件是完全自包含的。** 即使你无法访问 Uni-Lab-OS 仓库，也能根据本指南正确生成设备驱动。
> 如果你能访问仓库，建议搜索 `unilabos/devices/` 目录获取最新的已有设备实现。

端到端向导，通过**设备类别（物模型）** 和 **通信协议** 两个维度引导设备接入。

**核心变化：Uni-Lab-OS 使用装饰器 + AST 自动扫描生成注册表，无需手写 YAML。**

---

## 快速开始：使用 LabDeviceTemplate 创建外部设备包

如果你不需要修改 Uni-Lab-OS 核心代码，推荐使用外部设备包方式接入设备：

### 1. Fork 模板仓库

前往 [Xuwznln/LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate) 并 Fork。

### 2. 创建设备

将 `device_package_example/` 重命名为你的包名，在其中编写设备类：

```python
from unilabos.registry.decorators import device, action, topic_config

@device(id="my_device", category=["custom"], description="我的设备")
class MyDevice:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.device_id = device_id or "my_device"
        self.data = {}

    @action(description="执行操作")
    def do_something(self, param: str = "") -> dict:
        return {"success": True}

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "idle")
```

### 3. 本地验证

```bash
# 创建 conda 环境并安装 unilabos
mamba create -n unilab python=3.11.14 -c conda-forge -y
mamba activate unilab
mamba install uni-lab::unilabos -c uni-lab -c robostack-staging -c conda-forge -y

# 验证注册表（会自动检测并安装 requirements.txt 中的依赖）
unilab --check_mode --devices ./my_package --external_devices_only
```

### 4. 运行

```bash
unilab --devices ./my_package --external_devices_only -g graph.json
```

> `--external_devices_only` 跳过内置设备扫描，只加载你的外部设备包，启动更快。
> 设备包目录下的 `requirements.txt` 会被自动检测，缺失的依赖通过 `uv` 或 `pip` 自动安装。

详细的装饰器用法和设备类别说明见下文。

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

**pump_and_valve 子类型：**

| 子类型 | 最小通用属性 | 最小通用动作 | 单位约定 |
|---|---|---|---|
| 注射泵（syringe pump） | `status`, `valve_position`, `position`(mL) | `initialize`, `set_valve_position`, `set_position`(mL), `pull_plunger`(mL), `push_plunger`(mL), `stop_operation` | 体积=mL, 速度=mL/s |
| 电磁阀（solenoid valve） | `status`, `valve_position` | `open`, `close`, `set_valve_position` | — |
| 蠕动泵（peristaltic pump） | `status`, `speed` | `start`, `stop`, `set_speed` | 流速=mL/min |

**单位约定（重要）：** 设备对外暴露的属性和动作参数**必须使用用户友好的物理单位**，驱动内部负责在物理单位和硬件原始值之间转换。

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

| 来源 | AI 处理方式 |
|---|---|
| **现成 SDK/驱动代码** | 读取代码，提取指令逻辑，包装进 UniLab 框架 |
| **协议文档/手册** | 读取文档，解析指令格式 |
| **用户口述** | 按描述实现指令编解码 |
| **标准协议** | 直接使用标准实现（Modbus 寄存器表、SCPI 等） |
| **虚拟设备** | 跳过此步，动作方法中模拟行为 |

---

## 第四步：对齐同类设备接口（强制）

> **此步骤是强制性的，不可跳过。** 跳过此步会导致参数名不匹配、status 字符串不一致等问题。

1. 搜索 `unilabos/devices/` 目录找到同类别的已有设备实现
2. 对照已有设备的 `@property` + `@topic_config()` 属性和动作方法签名
3. **参数名必须与已有设备完全一致**（最常出错的地方）
4. **status 字符串值必须与已有设备一致**

---

## 第五步：创建设备驱动文件

文件路径：`unilabos/devices/<category>/<device_name>.py`

### 核心结构（使用装饰器，无需手写 YAML）

```python
import logging
from typing import Dict, Any

from unilabos.registry.decorators import device, action, topic_config, not_action

try:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
except ImportError:
    BaseROS2DeviceNode = None


@device(
    id="my_device",
    category=["temperature"],
    description="我的温控设备",
)
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
        self.data = {
            "status": "Idle",
            "temp": 25.0,
            "temp_target": 25.0,
        }

        # --- 通信层初始化（按第二步选择的协议填入）---
        # self.ser = serial.Serial(...)

    @not_action
    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node

    @not_action
    async def initialize(self) -> bool:
        self.data.update({"status": "Idle"})
        return True

    @not_action
    async def cleanup(self) -> bool:
        self.data.update({"status": "Offline"})
        return True

    # --- 动作方法（自动识别为 action_value_mappings）---
    async def set_temperature(self, temp: float, **kwargs) -> bool:
        """设定目标温度 (°C)"""
        temp = float(temp)
        self.data["temp_target"] = temp
        # >>> 在此填入实际指令 <<<
        return True

    async def stop(self, **kwargs) -> bool:
        self.data["status"] = "Idle"
        return True

    # --- 状态属性（自动识别为 status_types）---
    @property
    @topic_config()
    def temp(self) -> float:
        return self.data.get("temp", 0.0)

    @property
    @topic_config()
    def temp_target(self) -> float:
        return self.data.get("temp_target", 0.0)

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### 装饰器说明

| 装饰器 | 用途 | 位置 |
|---|---|---|
| `@device(id, category, description)` | 注册设备到 AST 扫描 | 类 |
| `@action(...)` | 显式声明动作（可指定 ROS Action 类型等） | 方法 |
| `@topic_config(period, name, ...)` | 声明状态属性，配置 ROS topic 发布 | `@property` 方法 |
| `@not_action` | 排除为动作（`post_init`, `initialize`, `cleanup` 等） | 方法 |
| `@always_free` | 标记不受 busy 队列限制的动作 | 方法 |

### `@device` 装饰器参数

```python
@device(
    id="my_device",                # 注册表 ID（唯一标识）
    category=["temperature"],      # 设备类别列表
    description="温控设备",         # 描述
    version="1.0.0",               # 版本
    icon="",                       # 图标
    handles=[],                    # Handle 定义
    hardware_interface=None,       # 硬件接口类型
)
```

### 动作方法自动发现规则

AST 扫描器按以下规则自动发现动作方法：

1. **`@action` 装饰的方法** → 显式声明的动作
2. **无 `@action` 的公开方法** → 自动识别为 `auto-{method_name}` 动作
3. **`@not_action` 装饰的方法** → 排除，不作为动作
4. **`_` 开头的私有方法** → 自动排除
5. **`post_init`、`initialize`、`cleanup`** → 建议用 `@not_action` 标记

### 状态属性自动发现规则

1. **`@property` + `@topic_config()`** → 识别为 `status_types`，名称为属性名
2. **`get_xxx` 方法 + `@topic_config()`** → 识别为 `status_types`，名称为去掉 `get_` 后的 `xxx`
3. **`get_xxx` 方法（无 `@topic_config`，无额外参数）** → 也会被识别为状态属性

**推荐方式：** 使用 `@property` + `@topic_config()` 明确声明。

### `@topic_config` 参数

```python
@topic_config(
    period=5.0,            # 发布周期（秒），默认约 5.0
    print_publish=False,   # 是否打印发布日志
    qos=10,                # QoS 深度
    name="custom_name",    # 自定义 topic/属性名
)
```

### `__init__` 参数 → `init_param_schema`

AST 自动从 `__init__` 签名提取参数，生成 `init_param_schema.config`。类型标注会被转换为 JSON Schema。

```python
def __init__(
    self,
    device_id: str = None,       # → {"type": "string"}
    config: Dict[str, Any] = None,  # → {"type": "object"}
    port: str = "COM1",          # → {"type": "string", "default": "COM1"}
    baudrate: int = 9600,        # → {"type": "integer", "default": 9600}
    **kwargs,
):
```

### 特殊参数类型

需要前端资源/设备选择器时：

```python
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot

async def transfer(self, source: ResourceSlot, target: ResourceSlot, volume: float) -> Dict[str, Any]:
    return {"success": True, "volume": volume}
```

| Python 类型 | 前端效果 |
|---|---|
| `ResourceSlot` | 单选资源下拉框 |
| `List[ResourceSlot]` | 多选资源下拉框 |
| `DeviceSlot` | 单选设备下拉框 |
| `List[DeviceSlot]` | 多选设备下拉框 |

### Python → ROS 类型映射

| Python | ROS | `status_types` |
|---|---|---|
| `str` | `std_msgs/String` | `String` |
| `bool` | `std_msgs/Bool` | `Bool` |
| `int` | `std_msgs/Int64` | `Int64` |
| `float` | `std_msgs/Float64` | `Float64` |
| `list`/`dict` | `std_msgs/String`（JSON 序列化） | `String` |

---

## 第六步：验证（不需要手写 YAML！）

**无需创建注册表 YAML。** `@device` 装饰器 + AST 扫描会在启动时自动生成全部注册表条目。

```bash
# 1. 模块可导入
python -c "from unilabos.devices.<category>.<file> import <ClassName>"

# 2. 启动测试（AST 自动扫描，无需 YAML）
unilab -g <graph>.json

# 3. 仅检查注册表
unilab --check_mode --skip_env_check
```

### 何时仍需 YAML？

仅在以下**少数情况**需要手动创建 YAML：

| 场景 | 说明 |
|---|---|
| 旧代码无 `@device` 装饰器 | 第三方库或未迁移的旧设备 |
| 需手动覆盖特定字段 | 如特殊 `handles`、`placeholder_keys` |
| `--complete_registry` 补全 | 对仅有 YAML 的旧设备做一次性 AST 补全并写回 |

YAML 最小配置（如确实需要）：

```yaml
my_device:
  class:
    module: unilabos.devices.<category>.<file>:MyDevice
    type: python
```

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

`config` 中的参数直接传入 `__init__` 的 `config` 字典。`class` 对应 `@device(id=...)` 中的 id。

---

## 工作流清单

```
设备接入进度：
- [ ] 1. 确定设备类别（物模型）+ 单位约定
- [ ] 2. 确定通信协议
- [ ] 3. 收集指令协议（SDK/文档/口述）
- [ ] 4. 对齐同类设备接口（搜索 unilabos/devices/ 目录）
- [ ] 5. 创建驱动 unilabos/devices/<category>/<file>.py（使用 @device 装饰器）
- [ ] 6. 验证可导入 + 启动测试（无需 YAML）
- [ ] 7. 配置图文件（如需要）
```

---

## 关键规则（违反任何一条都会导致设备接入失败）

1. **使用 `@device` 装饰器** — 替代手写 YAML，AST 自动扫描生成注册表
2. **使用 `@property` + `@topic_config()`** — 声明状态属性，自动映射为 `status_types`
3. **使用 `@not_action`** — 标记 `post_init`、`initialize`、`cleanup` 等非动作方法
4. **禁止重命名参数** — 动作方法的参数名是接口契约，禁止加后缀或改名
5. **status 字符串必须一致** — 与同类已有设备保持相同的状态字符串
6. **`self.data` 必须预填充** — 不能用空字典 `{}`，每个 `@property` 对应的键都必须有初始值
7. **异步等待用 `_ros_node.sleep`** — `await self._ros_node.sleep()`，禁止 `time.sleep()` 和 `asyncio.sleep()`
8. **物理单位对外暴露** — 参数使用 mL、°C、RPM 等物理单位，内部负责转换
9. **串口解析先找帧头** — RS-485 响应前常有噪声字节，必须先定位帧起始标记

---

## 物模型代码模板

### temperature — 温控设备

```python
from unilabos.registry.decorators import device, topic_config, not_action


@device(id="my_heater", category=["temperature"], description="温控设备")
class MyTemperatureDevice:
    """温控设备：加热器、冷却器、恒温槽等"""

    def __init__(self, device_id=None, config=None, **kwargs):
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')
        self.device_id = device_id or "unknown"
        self.config = config or {}
        self.data = {
            "status": "Idle",
            "temp": 25.0,
            "temp_target": 25.0,
        }

    @not_action
    def post_init(self, ros_node):
        self._ros_node = ros_node

    async def set_temperature(self, temp: float, **kwargs) -> bool:
        """设定目标温度 (°C)"""
        temp = float(temp)
        self.data["temp_target"] = temp
        # >>> 在此填入实际指令 <<<
        return True

    async def stop(self, **kwargs) -> bool:
        self.data["status"] = "Idle"
        return True

    @property
    @topic_config()
    def temp(self) -> float:
        return self.data.get("temp", 0.0)

    @property
    @topic_config()
    def temp_target(self) -> float:
        return self.data.get("temp_target", 0.0)

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

### pump_and_valve — 注射泵

```python
@device(id="my_syringe_pump", category=["pump_and_valve"], description="注射泵")
class MySyringePump:
    """注射泵设备 — 含阀门控制。参数名不可修改。"""

    def __init__(self, device_id=None, config=None, **kwargs):
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')
        self.device_id = device_id or "unknown"
        self.config = config or {}
        self.max_volume = float(self.config.get("max_volume", 25.0))
        self.data = {
            "status": "Idle",
            "valve_position": "I",
            "position": 0.0,
        }

    @not_action
    def post_init(self, ros_node):
        self._ros_node = ros_node

    def initialize(self):
        return True

    def set_valve_position(self, position):
        """设置阀门位置。参数名必须是 position"""
        return True

    def set_position(self, position: float, max_velocity: float = None):
        """移动到绝对体积位置 (mL)"""
        return True

    def pull_plunger(self, volume: float):
        """吸液 (mL)。参数名必须是 volume"""
        return True

    def push_plunger(self, volume: float):
        """排液 (mL)。参数名必须是 volume"""
        return True

    def stop_operation(self):
        return True

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Idle")

    @property
    @topic_config()
    def valve_position(self) -> str:
        return self.data.get("valve_position", "I")

    @property
    @topic_config()
    def position(self) -> float:
        return self.data.get("position", 0.0)
```

### pump_and_valve — 电磁阀

```python
@device(id="my_solenoid_valve", category=["pump_and_valve"], description="电磁阀")
class MySolenoidValve:
    def __init__(self, device_id=None, config=None, **kwargs):
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')
        self.device_id = device_id or "unknown"
        self.config = config or {}
        self.data = {"status": "Idle", "valve_position": "closed"}

    @not_action
    def post_init(self, ros_node):
        self._ros_node = ros_node

    async def open(self, **kwargs) -> bool:
        return True

    async def close(self, **kwargs) -> bool:
        return True

    async def set_valve_position(self, position: str, **kwargs) -> bool:
        self.data["valve_position"] = str(position)
        return True

    @property
    @topic_config()
    def valve_position(self) -> str:
        return self.data.get("valve_position", "closed")

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Idle")
```

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

---

## 人工确认动作（Manual Confirm）

### 前端 UI 行为契约

后端通过 `node_type` + `placeholder_keys` + `goal_default` 向前端表达"这是个需要人工弹窗的节点"。前端据此：

| 后端约定 | 前端渲染 |
|---|---|
| `node_type: "manual_confirm"` | 节点图标用「人工确认」样式；运行到此节点时弹出确认对话框 |
| `placeholder_keys["assignee_user_ids"]="unilabos_manual_confirm"` | 渲染**人员选择器**（搜索 + 多选用户，而非纯数组输入） |
| `placeholder_keys[X]="unilabos_resources"`（自动） | 字段 X 渲染**资源下拉框**（来自当前实验图） |
| `placeholder_keys[X]="unilabos_devices"`（自动） | 字段 X 渲染**设备下拉框** |
| `goal_default[X]=...` | 字段 X 的默认值 |
| `feedback_interval: 300` | 任务运行期间，`feedback` 消息节流到 5 分钟一次（前端可据此显示"等待中" 而不是疯狂刷新） |
| 函数 docstring | 弹窗顶部的说明文字 |

弹窗的整体形态（描述性，非渲染规范）：

```
┌────────────────────────────────────────────┐
│  人工确认 — workbench_1.manual_confirm     │
├────────────────────────────────────────────┤
│  说明：timeout_seconds: 超时时间（秒）...   │  ← 取自函数 docstring
│                                            │
│  目标孔位 (mount_resource):                │
│    [ 6-10-2 ✕ ] [ 6-10-3 ✕ ] [+ 添加 ▼ ]   │  ← unilabos_resources
│                                            │
│  极流体质量 (collector_mass): [3.64,...]   │  ← 普通数组
│  克容量 (capacity):           [270,...]    │
│                                            │
│  指派人 (assignee_user_ids):               │  ← unilabos_manual_confirm
│    🔍 [搜索用户...] [✓ 张三] [✓ 李四]      │
│                                            │
│  超时 (timeout_seconds): [3600] 秒          │
│                                            │
│              [ 取消 ]   [ 确认并继续 ]      │
└────────────────────────────────────────────┘
```

确认后前端把（可能被修改的）参数回传给该 action 的 goal，host 才放行任务进入下游节点。

### 可复制模板

#### 模板 A：host 全局确认（最简，仅卡点）

```python
from unilabos.registry.decorators import action, NodeType

@action(
    always_free=True,
    node_type=NodeType.MANUAL_CONFIRM,
    placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
    goal_default={"timeout_seconds": 3600, "assignee_user_ids": []},
)
def manual_confirm(self, timeout_seconds: int, assignee_user_ids: list[str], **kwargs) -> dict:
    """
    通用人工卡点。任意 kwargs 都会原样返回给下游。
    timeout_seconds: 超时时间（秒）
    """
    return kwargs
```

适用于：仅在流程中插入「等待操作员点确认」，不携带物料/参数。

#### 模板 B：设备携带物料的确认（带数据中转）

```python
from typing import List
from unilabos.registry.decorators import (
    action, NodeType, ActionInputHandle, ActionOutputHandle, DataSource,
)
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from unilabos.resources.resource_tracker import ResourceTreeSet

@action(
    always_free=True,
    node_type=NodeType.MANUAL_CONFIRM,
    feedback_interval=300,
    placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
    goal_default={"timeout_seconds": 3600, "assignee_user_ids": []},
    handles=[
        # ──── 输入：来自上游 ────
        ActionInputHandle(key="target_device", data_type="device_id",
                          label="目标设备", data_key="target_device",
                          data_source=DataSource.HANDLE),
        ActionInputHandle(key="mount_resource", data_type="resource",
                          label="目标孔位", data_key="mount_resource",
                          data_source=DataSource.HANDLE),
        ActionInputHandle(key="capacity", data_type="capacity",
                          label="克容量", data_key="capacity",
                          data_source=DataSource.HANDLE),
        # ──── 输出：转发给下游 ────
        ActionOutputHandle(key="target_device", data_type="device_id",
                           label="目标设备", data_key="target_device",
                           data_source=DataSource.EXECUTOR),
        ActionOutputHandle(key="mount_resource", data_type="resource",
                           label="目标孔位", data_key="mount_resource.@flatten",
                           data_source=DataSource.EXECUTOR),
        ActionOutputHandle(key="capacity", data_type="capacity",
                           label="克容量", data_key="capacity",
                           data_source=DataSource.EXECUTOR),
    ],
)
def manual_confirm(
    self,
    target_device: DeviceSlot,
    mount_resource: List[ResourceSlot],
    capacity: List[float],
    timeout_seconds: int,
    assignee_user_ids: list[str],
    **kwargs,
) -> dict:
    """
    人工放置物料并确认工艺参数后，把数据转交给下游设备。
    timeout_seconds: 超时时间（秒），默认 3600
    """
    mount_resource = ResourceTreeSet.from_plr_resources(mount_resource).dump()
    kwargs.update(locals())
    kwargs.pop("kwargs")
    kwargs.pop("self")
    return kwargs
```

适用于：「人工放电池/装样 → 确认参数 → 启动后续测试」这种流程，确认后参数完整流入下游。

### 实现要点 / 易错点

| ✗ 反例 | ✓ 正确做法 |
|---|---|
| `node_type=NodeType.ILAB` 配人工弹窗 | 必须 `NodeType.MANUAL_CONFIRM`，否则前端不会弹窗 |
| 漏写 `always_free=True` | 设备会被「等待人工」长期锁住，其它动作排队 |
| 漏写 `placeholder_keys["assignee_user_ids"]` | 指派人字段退化成普通字符串数组输入框，无法搜索用户 |
| 函数返回 `List[ResourceSlot]`（PLR 对象）给下游 | 必须 `ResourceTreeSet.from_plr_resources(x).dump()` 转 dict |
| `ActionOutputHandle.data_source=HANDLE` | 输出口必须 `EXECUTOR`，否则下游拿不到数据 |
| `key` 与函数参数名/return dict key 不一致 | 接线找不到字段；保持完全同名 |
| 没给 `goal_default["timeout_seconds"]` | 前端必填校验卡死用户 |
| 把 `timeout_seconds` 改成 `timeout_ms` 之类的别名 | host 路由按固定字段名识别，必须保留 |

---

---

## 设备注册表进入微后端

节点启动时会扫描所有 `@device` 类和 Registry YAML，形成当前进程的设备能力清单。
Host 不再生成 `req_device_registry_upload.json`，也不会调用旧 `/lab/resource`
接口；物料模板由 `unilabos.server.adapters.registry_materials` 同步到微后端，
设备动作、状态与通信能力则通过当前 Registry 和运行时 API 提供给 Backend/前端。

调试时建议：

- 用 `unilab --check_mode --complete_registry --skip_env_check` 校验完整 Registry；
- 在 `/api/docs` 查看当前微后端实际暴露的接口；
- 检查启动日志中的模板同步数量、设备描述和 HostLink/ROS2 端点能力。
