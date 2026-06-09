---
name: add-device
description: Guide for adding new devices to Uni-Lab-OS (接入新设备). Uses @device decorator + AST auto-scanning instead of manual YAML. Walks through device category, communication protocol, driver creation with decorators, and graph file setup. Use when the user wants to add/integrate a new device, create a device driver, write a device class, or mentions 接入设备/添加设备/设备驱动/物模型.
---

# 添加新设备到 Uni-Lab-OS

本 Skill 是自包含的设备接入指南，不依赖外部文档。迁移给别人时，只复制 `.cursor/skills/add-device/SKILL.md` 即可获得核心规则、模板、验证方式和常见错误清单。

开始实现前，仍应搜索 `unilabos/devices/` 获取同类别已有设备的接口、参数名、状态字符串和返回值风格作为参考。

---

## 接入工作流

按下面顺序推进，并在工作中维护进度：

```text
设备接入进度：
- [ ] 1. 确定设备类别（物模型）和对外单位
- [ ] 2. 确定通信协议
- [ ] 3. 收集指令协议（SDK、厂商文档、寄存器表、HTTP API、用户口述）
- [ ] 4. 对齐同类设备接口（搜索 unilabos/devices/）
- [ ] 5. 创建驱动 unilabos/devices/<category>/<file>.py
- [ ] 6. 验证可导入、注册表扫描、启动测试
- [ ] 7. 如需要，配置实验图文件
```

## 设备类别（物模型）

优先使用已有类别。只有确实无法归类时才使用 `custom`。

| 类别 ID | 说明 | 标准属性 | 标准动作 |
|---|---|---|---|
| `temperature` | 加热、冷却、温控 | `temp`, `temp_target`, `status` | `set_temperature`, `stop` |
| `pump_and_valve` | 泵、阀门、注射器 | 见子类型表 | 见子类型表 |
| `motor` | 电机、步进马达 | `position`, `status` | `enable`, `move_position`, `move_speed`, `stop` |
| `heaterstirrer` | 加热搅拌一体机 | `temp`, `stir_speed`, `status` | `set_temperature`, `stir`, `stop` |
| `balance` | 天平、称重 | `weight`, `unit`, `status` | `tare`, `read_weight` |
| `sensor` | 传感器（液位、温度等） | `value`, `level`, `status` | `read_value`, `set_threshold` |
| `liquid_handling` | 液体处理机器人 | `status`, `deck_state` | `transfer_liquid`, `aspirate`, `dispense` |
| `robot_arm` | 机械臂 | `arm_pose`, `arm_status` | `moveit_task`, `pick_and_place` |
| `workstation` | 工作站、组合设备 | `workflow_sequence`, `material_info` | `create_order`, `scheduler_start`, `scheduler_stop` |
| `virtual` | 虚拟、模拟设备 | 按模拟的真实设备定义 | 按模拟的真实设备定义 |
| `custom` | 不属于以上类别 | 用户自定义 | 用户自定义 |

`pump_and_valve` 子类型：

| 子类型 | 最小通用属性 | 最小通用动作 | 单位约定 |
|---|---|---|---|
| 注射泵（syringe pump） | `status`, `valve_position`, `position` | `initialize`, `set_valve_position`, `set_position`, `pull_plunger`, `push_plunger`, `stop_operation` | 体积=mL, 速度=mL/s |
| 电磁阀（solenoid valve） | `status`, `valve_position` | `open`, `close`, `set_valve_position` | 无 |
| 蠕动泵（peristaltic pump） | `status`, `speed` | `start`, `stop`, `set_speed` | 流速=mL/min |

对外暴露的属性和动作参数必须使用用户友好的物理单位（mL、ul、degC、RPM 等），硬件原始值转换放在驱动内部。

## 通信协议和指令来源

先确认通信方式，再确认具体指令协议。物模型只定义设备“应该做什么”，不会告诉你硬件“具体发什么字节/请求”。

| 协议 | 常用 config 参数 | 常用依赖 | 现有抽象 |
|---|---|---|---|
| Serial (RS232/RS485) | `port`, `baudrate`, `timeout` | `pyserial` | 直接使用 `serial.Serial` |
| Modbus RTU | `port`, `baudrate`, `slave_id` | `pymodbus` | `device_comms/modbus_plc/` |
| Modbus TCP | `host`, `port`, `slave_id` | `pymodbus` | `device_comms/modbus_plc/` |
| TCP Socket | `host`, `port`, `timeout` | stdlib | 直接使用 `socket` |
| HTTP API | `url`, `token`, `timeout` | `requests` | `device_comms/rpc.py` |
| OPC UA | `url` | `opcua` | `device_comms/opcua_client/` |
| 无通信（虚拟） | 无 | 无 | 在动作中模拟行为 |

必须从以下来源之一获得指令细节：

| 来源 | 处理方式 |
|---|---|
| 现成 SDK/驱动代码 | 读取代码，提取指令逻辑，包装进 Uni-Lab-OS 类 |
| 协议文档/手册 | 解析命令、响应、校验、寄存器、错误码 |
| 用户口述 | 按描述实现指令编解码，标出不确定点 |
| 标准协议 | 使用标准实现，例如 Modbus 寄存器表、SCPI |
| 虚拟设备 | 跳过硬件通信，在动作方法中维护模拟状态 |

## 对齐已有实现（强制）

实现前必须搜索 `unilabos/devices/` 中同类别设备：

- 参数名必须与已有设备保持一致；动作方法参数名是接口契约，不要随意改成 `volume_ml`、`target_temp_c` 这类新名字。
- `status` 字符串值要和同类设备一致，优先使用英文稳定值，例如 `Idle`、`Running`、`Error`。
- 状态属性用 `@property` + `@topic_config()` 明确声明。
- 返回值使用结构化 dict，至少包含 `success`，需要给前端展示的信息放在 `message`、`data`、`error` 等字段。

## 架构选择

| 场景 | 推荐方式 |
|---|---|
| 简单设备 | 纯 Python 类 + `@device` |
| 工作站/组合设备 | `WorkstationBase` 或项目内已有工作站模式 |
| 液体处理 | `LiquidHandlerAbstract` / PyLabRobot 相关模式 |
| Modbus 设备 | 复用 `device_comms/modbus_plc/` 或项目内 Modbus 示例 |
| OPC UA 设备 | 复用 `device_comms/opcua_client/` |
| 外部独立包 | 使用 `create-device-package` skill |

---

## 装饰器参考

### @device — 设备类装饰器

```python
from unilabos.registry.decorators import device

# 单设备
@device(
    id="my_device_vendor",           # 注册表唯一标识（必填，只能包含英文、数字、下划线）
    category=["temperature"],         # 分类标签列表（必填）
    description="设备描述",            # 设备描述
    displayname="显示名称",            # UI 显示名称（默认用 id）
    icon="DeviceIcon.webp",           # 图标文件名
    version="1.0.0",                  # 版本号
    device_type="python",             # "python" 或 "ros2"
    handles=[...],                    # 端口列表（InputHandle / OutputHandle）
    model={...},                      # 3D 模型配置
    hardware_interface=HardwareInterface(...),  # 硬件通信接口
)

# 多设备（同一个类注册多个设备 ID，各自有不同的 handles 等配置）
@device(
    ids=["pump_vendor_model_A", "pump_vendor_model_B"],
    id_meta={
        "pump_vendor_model_A": {"handles": [...], "description": "型号 A", "displayname": "泵型号 A"},
        "pump_vendor_model_B": {"handles": [...], "description": "型号 B", "displayname": "泵型号 B"},
    },
    category=["pump_and_valve"],
)
```

**ID 与显示名规则：**
- `id` / `ids` 是注册表稳定标识，只能包含英文大小写字母、数字、下划线，推荐格式为 `vendor_model` 或 `category_vendor_model`。
- `id` / `ids` 不能包含中文、空格、短横线、点号或其他符号；不要把中文设备名放进 id。
- 中文名、品牌型号展示名、UI 友好名称使用 `displayname`，不要塞进 `id`。

### @action — 动作方法装饰器

```python
from unilabos.registry.decorators import action

@action                              # 无参：注册为 UniLabJsonCommand 动作
@action()                            # 同上
@action(description="执行操作")       # 带描述
@action(
    action_type=HeatChill,           # 指定 ROS Action 消息类型
    goal={"temperature": "temp"},    # Goal 字段映射
    feedback={},                     # Feedback 字段映射
    result={},                       # Result 字段映射
    handles=[...],                   # 动作级别端口
    goal_default={"temp": 25.0},     # Goal 默认值
    placeholder_keys={...},          # 参数占位符
    always_free=True,                # 不受排队限制
    auto_prefix=True,                # 强制使用 auto- 前缀
    parent=True,                     # 从父类 MRO 获取参数签名
)
```

**自动识别规则：**
- 带 `@action` 的公开方法 → 注册为动作（方法名即动作名）
- **不带 `@action` 的公开方法** → 自动注册为 `auto-{方法名}` 动作
- `_` 开头的方法 → 不扫描
- `@not_action` 标记的方法 → 排除

### 参数文档 → JSON Schema 元数据

在 `__init__` 和 action 方法 docstring 的 `Args:` 小节里，使用以下格式生成入参 schema 的显示信息：

```python
"""
Args:
    param[显示名称]: 参数说明，会写入 JSON Schema 的 description。
"""
```

- `param[显示名称]` 的显示名称会写入 goal property 的 `title`。
- `:` 后面的说明会写入 goal property 的 `description`。
- 如果只写 `param: 参数说明`，`title` 会兜底为字段名，`description` 使用参数说明。
- 如果没有写参数文档，生成器也会兜底补齐 `title=<字段名>` 和 `description=""`，但新设备应优先写清楚显示名和说明。

### 特殊参数类型：ResourceSlot / DeviceSlot

需要前端选择资源或设备时，用特殊类型注解，registry 会自动生成 `placeholder_keys`：

```python
from typing import List
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot

@action(description="转移液体")
def transfer(self, source: ResourceSlot, target: ResourceSlot, volume_ul: float) -> dict:
    """
    Args:
        source[源资源]: 源容器或孔位。
        target[目标资源]: 目标容器或孔位。
        volume_ul[体积(ul)]: 转移体积。
    """
    return {"success": True}

@action(description="同步设备")
def sync_devices(self, devices: List[DeviceSlot]) -> dict:
    return {"success": True, "count": len(devices)}
```

### @topic_config — 状态属性配置

```python
from unilabos.registry.decorators import topic_config

@property
@topic_config(
    period=5.0,            # 发布周期（秒），默认 5.0
    print_publish=False,   # 是否打印发布日志
    qos=10,                # QoS 深度，默认 10
    name="custom_name",    # 自定义发布名称（默认用属性名）
)
def temperature(self) -> float:
    return self.data.get("temperature", 0.0)
```

### 辅助装饰器

```python
from unilabos.registry.decorators import not_action, always_free

@not_action          # 标记为非动作（post_init、辅助方法等）
@always_free         # 标记为不受排队限制（查询类操作）
```

---

## 设备模板

```python
import logging
from typing import Any, Dict, Optional

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.registry.decorators import action, device, not_action, topic_config

@device(
    id="my_device",
    category=["my_category"],
    description="设备描述",
    displayname="设备显示名",
)
class MyDevice:
    """设备类说明。"""

    _ros_node: BaseROS2DeviceNode

    def __init__(
        self,
        device_id: Optional[str] = None,
        port: str = "COM1",
        baudrate: int = 9600,
        timeout: float = 1.0,
        **kwargs,
    ):
        """
        初始化设备。

        Args:
            device_id[设备ID]: 设备实例 ID，默认使用 my_device。
            port[串口]: 设备串口号，例如 COM1 或 /dev/ttyUSB0。
            baudrate[波特率]: 串口波特率。
            timeout[超时时间(s)]: 通信超时时间，单位秒。
        """
        self.device_id = device_id or "my_device"
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.logger = logging.getLogger(f"MyDevice.{self.device_id}")
        self.data: Dict[str, Any] = {"status": "Idle"}

    @not_action
    def post_init(self, ros_node: BaseROS2DeviceNode) -> None:
        self._ros_node = ros_node

    @action
    async def initialize(self) -> bool:
        self.data["status"] = "Ready"
        return True

    @action
    async def cleanup(self) -> bool:
        self.data["status"] = "Offline"
        return True

    @action(description="执行操作")
    def my_action(self, param: float = 0.0, name: str = "") -> Dict[str, Any]:
        """
        带 @action 装饰器 → 注册为 'my_action' 动作。

        Args:
            param[操作数值]: 操作使用的数值参数。
            name[操作名称]: 操作名称或备注。
        """
        return {"success": True}

    def get_info(self) -> Dict[str, Any]:
        """无 @action → 自动注册为 'auto-get_info' 动作"""
        return {"device_id": self.device_id}

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Idle")

    @property
    @topic_config(period=2.0)
    def temperature(self) -> float:
        return self.data.get("temperature", 0.0)
```

### 要点

- `_ros_node: BaseROS2DeviceNode` 类型标注放在类体顶部
- `__init__` 中需要现场配置的参数按基础类型显式展开，例如 `port: str`、`baudrate: int`、`timeout: float`、`enabled: bool`；不要把所有配置塞进单个 `config: dict`
- `__init__` 保留 `device_id` 和 `**kwargs` 兼容运行时注入，但不要把 `**kwargs` 当成主要配置入口
- `post_init` 用 `@not_action` 标记，参数类型标注为 `BaseROS2DeviceNode`
- 运行时状态存储在 `self.data` 字典中
- 设备文件放在 `unilabos/devices/<category>/` 目录下

---

## 通信实现片段

Serial 文本指令：

```python
def _send_command(self, cmd: str) -> str:
    self.ser.write(f"{cmd}\r\n".encode())
    return self.ser.readline().decode().strip()
```

RS-485 响应解析要先定位帧头，不要用硬编码索引直接解析原始响应：

```python
def _normalize_response(self, raw: str, start_marker: str = "/") -> str:
    pos = raw.find(start_marker)
    return raw[pos:] if pos >= 0 else raw
```

自定义二进制帧：

```python
def _build_frame(self, func_code: int, data: bytes) -> bytes:
    frame = bytearray([0xFE, func_code]) + bytearray(data)
    checksum = sum(frame[1:]) % 256
    frame.append(checksum)
    return bytes(frame)
```

Modbus 寄存器映射：

```python
REGISTER_MAP = {
    "temp_target": {"addr": 0x000B, "scale": 10},
}

def set_temperature(self, temp: float, **kwargs) -> bool:
    reg = REGISTER_MAP["temp_target"]
    value = int(float(temp) * reg["scale"]) & 0xFFFF
    self.client.write_register(reg["addr"], value, slave=self.slave_id)
    self.data["temp_target"] = temp
    return True
```

HTTP API 映射：

```python
API_MAP = {
    "set_temperature": {
        "method": "POST",
        "endpoint": "/api/temperature",
        "body_key": "target",
    },
}
```

SDK 封装：

```python
from my_device_sdk import DeviceController

class MyDevice:
    def __init__(self, device_id=None, port: str = "COM1", timeout: float = 1.0, **kwargs):
        self.controller = DeviceController(port=port, timeout=timeout)
```

---

## 验证

无需手写注册表 YAML。`@device` 装饰器 + AST 扫描会在启动或检查时生成注册表条目。

```bash
# 1. 模块可导入
python -c "from unilabos.devices.<category>.<file> import <ClassName>"

# 2. 启动测试
unilab -g <graph>.json

# 3. 仅检查注册表
unilab --check_mode --skip_env_check
```

仅在旧代码无 `@device`、需要覆盖特殊字段、或做 `--complete_registry` 旧设备补全时，才考虑 YAML。新设备默认不要手写 YAML。

## 图文件节点模板

实验图 JSON 中的 `class` 对应 `@device(id=...)`。`config` 中的字段应对应 `__init__` 的同名基础类型参数，不要只定义一个 `config: dict` 参数承载所有配置：

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

工作站需要同时配置 `deck` 和 `children`：

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
      "config": {"type": "MyDeckClass", "setup": true}
    }
  ]
}
```

---

## 常见错误清单

- 缺少 `@device`：设备不会被 AST 扫描发现。
- `@device(id=...)` 使用中文、点号、短横线或空格：id 必须只包含英文、数字、下划线，显示名称用 `displayname`。
- 只有 `@property` 没有 `@topic_config()`：属性不会稳定广播到 `status_types`。
- `post_init` 没有 `@not_action`：会被误暴露为动作。
- `self.data = {}`：空字典会导致属性读取和 schema 初始数据不稳定，必须预填充每个状态键。
- 动作参数重命名：不要把同类设备已有的 `volume` 改成 `volume_ml`，参数名是接口契约。
- `status` 使用中文或临时文本：前端和工作流依赖稳定英文状态值。
- async 方法中使用 `time.sleep()`：应使用 `await self._ros_node.sleep(seconds)`。
- 硬编码串口响应索引：RS-485 响应前可能有噪声字节，应先定位帧头。
- 把硬件寄存器单位暴露给用户：对外使用物理单位，驱动内部做 scale 转换。
