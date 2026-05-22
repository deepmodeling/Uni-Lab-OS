---
name: add-workstation
description: Guide for adding new workstations to Uni-Lab-OS (接入新工作站). Uses @device decorator + AST auto-scanning. Walks through workstation type, sub-device composition, driver creation, deck setup, and graph file. Use when the user wants to add a workstation, create a workstation driver, configure a station with sub-devices, or mentions 工作站/工站/station/workstation.
---

# Uni-Lab-OS 工作站接入指南

工作站（workstation）是组合多个子设备的大型设备，拥有独立的物料管理系统和工作流引擎。使用 `@device` 装饰器注册，AST 自动扫描生成注册表。

---

## 工作站类型

| 类型                | 基类              | 适用场景                           |
| ------------------- | ----------------- | ---------------------------------- |
| **Protocol 工作站** | `ProtocolNode`    | 标准化学操作协议（泵转移、过滤等） |
| **外部系统工作站**  | `WorkstationBase` | 与外部 LIMS/MES 对接               |
| **硬件控制工作站**  | `WorkstationBase` | 直接控制 PLC/硬件                  |

---

## @device 装饰器（工作站）

工作站也使用 `@device` 装饰器注册，参数与普通设备一致：

```python
@device(
    id="my_workstation",           # 注册表唯一标识（必填）
    category=["workstation"],      # 分类标签
    description="我的工作站",
)
```

如果一个工作站类支持多个具体变体，可使用 `ids` / `id_meta`，与设备的用法相同（参见 add-device SKILL）。

---

## 工作站驱动模板

### 模板 A：基于外部系统的工作站

```python
import logging
from typing import Dict, Any, Optional
from pylabrobot.resources import Deck

from unilabos.registry.decorators import device, topic_config, not_action
from unilabos.devices.workstation.workstation_base import WorkstationBase

try:
    from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
except ImportError:
    ROS2WorkstationNode = None


@device(id="my_workstation", category=["workstation"], description="我的工作站")
class MyWorkstation(WorkstationBase):
    _ros_node: "ROS2WorkstationNode"

    def __init__(self, config=None, deck=None, protocol_type=None, **kwargs):
        super().__init__(deck=deck, **kwargs)
        self.config = config or {}
        self.logger = logging.getLogger("MyWorkstation")
        self.api_host = self.config.get("api_host", "")
        self._status = "Idle"

    @not_action
    def post_init(self, ros_node: "ROS2WorkstationNode"):
        super().post_init(ros_node)
        self._ros_node = ros_node

    async def scheduler_start(self, **kwargs) -> Dict[str, Any]:
        """注册为工作站动作"""
        return {"success": True}

    async def create_order(self, json_str: str, **kwargs) -> Dict[str, Any]:
        """注册为工作站动作"""
        return {"success": True}

    @property
    @topic_config()
    def workflow_sequence(self) -> str:
        return "[]"

    @property
    @topic_config()
    def material_info(self) -> str:
        return "{}"
```

### 模板 B：Protocol 工作站

直接使用 `ProtocolNode`，通常不需要自定义驱动类：

```python
from unilabos.devices.workstation.workstation_base import ProtocolNode
```

在图文件中配置 `protocol_type` 即可。

---

## 子设备访问（sub_devices）

工站初始化子设备后，所有子设备实例存储在 `self._ros_node.sub_devices` 字典中（key 为设备 id，value 为 `ROS2DeviceNode` 实例）。工站的驱动类可以直接获取子设备实例来调用其方法：

```python
# 在工站驱动类的方法中访问子设备
sub = self._ros_node.sub_devices["pump_1"]

# .driver_instance — 子设备的驱动实例（即设备 Python 类的实例）
sub.driver_instance.some_method(arg1, arg2)

# .ros_node_instance — 子设备的 ROS2 节点实例
sub.ros_node_instance._action_value_mappings  # 查看子设备支持的 action
```

**常见用法**：

```python
class MyWorkstation(WorkstationBase):
    def my_protocol(self, **kwargs):
        # 获取子设备驱动实例
        pump = self._ros_node.sub_devices["pump_1"].driver_instance
        heater = self._ros_node.sub_devices["heater_1"].driver_instance

        # 直接调用子设备方法
        pump.aspirate(volume=100)
        heater.set_temperature(80)
```

> 参考实现：`unilabos/devices/workstation/bioyond_studio/reaction_station/reaction_station.py` 中通过 `self._ros_node.sub_devices.get(reactor_id)` 获取子反应器实例并更新数据。

---

## 硬件通信接口（hardware_interface）

硬件控制型工作站通常需要通过串口（Serial）、Modbus 等通信协议控制多个子设备。Uni-Lab-OS 通过 **通信设备代理** 机制实现端口共享：一个串口只创建一个 `serial` 节点，多个子设备共享这个通信实例。

### 工作原理

`ROS2WorkstationNode` 初始化时分两轮遍历子设备（`workstation.py`）：

**第一轮 — 初始化所有子设备**：按 `children` 顺序调用 `initialize_device()`，通信设备（`serial_` / `io_` 开头的 id）优先完成初始化，创建 `serial.Serial()` 实例。其他子设备此时 `self.hardware_interface = "serial_pump"`（字符串）。

**第二轮 — 代理替换**：遍历所有已初始化的子设备，读取子设备的 `_hardware_interface` 配置：

```
hardware_interface = d.ros_node_instance._hardware_interface
# → {"name": "hardware_interface", "read": "send_command", "write": "send_command"}
```

1. 取 `name` 字段对应的属性值：`name_value = getattr(driver, hardware_interface["name"])`
   - 如果 `name_value` 是字符串且该字符串是某个子设备的 id → 触发代理替换
2. 从通信设备获取真正的 `read`/`write` 方法
3. 用 `setattr(driver, read_method, _read)` 将通信设备的方法绑定到子设备上

因此：

- **通信设备 id 必须与子设备 config 中填的字符串完全一致**（如 `"serial_pump"`）
- **通信设备 id 必须以 `serial_` 或 `io_` 开头**（否则第一轮不会被识别为通信设备）
- **通信设备必须在 `children` 列表中排在最前面**，确保先初始化

### HardwareInterface 参数说明

```python
from unilabos.registry.decorators import HardwareInterface

HardwareInterface(
    name="hardware_interface",   # __init__ 中接收通信实例的属性名
    read="send_command",         # 通信设备上暴露的读方法名
    write="send_command",        # 通信设备上暴露的写方法名
    extra_info=["list_ports"],   # 可选：额外暴露的方法
)
```

**`name` 字段的含义**：对应设备类 `__init__` 中，用于保存通信实例的**属性名**。系统据此知道要替换哪个属性。大部分设备直接用 `"hardware_interface"`，也可以自定义（如 `"io_device_port"`）。

### 示例 1：泵（name="hardware_interface"）

```python
from unilabos.registry.decorators import device, HardwareInterface

@device(
    id="my_pump",
    category=["pump_and_valve"],
    hardware_interface=HardwareInterface(
        name="hardware_interface",
        read="send_command",
        write="send_command",
    ),
)
class MyPump:
    def __init__(self, port=None, address="1", **kwargs):
        # name="hardware_interface" → 系统替换 self.hardware_interface
        self.hardware_interface = port  # 初始为字符串 "serial_pump"，启动后被替换为 Serial 实例
        self.address = address

    def send_command(self, command: str):
        full_command = f"/{self.address}{command}\r\n"
        self.hardware_interface.write(bytearray(full_command, "ascii"))
        return self.hardware_interface.read_until(b"\n")
```

### 示例 2：电磁阀（name="io_device_port"，自定义属性名）

```python
@device(
    id="solenoid_valve",
    category=["pump_and_valve"],
    hardware_interface=HardwareInterface(
        name="io_device_port",       # 自定义属性名 → 系统替换 self.io_device_port
        read="read_io_coil",
        write="write_io_coil",
    ),
)
class SolenoidValve:
    def __init__(self, io_device_port: str = None, **kwargs):
        # name="io_device_port" → 图文件 config 中用 "io_device_port": "io_board_1"
        self.io_device_port = io_device_port  # 初始为字符串，系统替换为 Modbus 实例
```

### Serial 通信设备（class="serial"）

`serial` 是 Uni-Lab-OS 内置的通信代理设备，代码位于 `unilabos/ros/nodes/presets/serial_node.py`：

```python
from serial import Serial, SerialException
from threading import Lock

class ROS2SerialNode(BaseROS2DeviceNode):
    def __init__(self, device_id, registry_name, port: str, baudrate: int = 9600, **kwargs):
        self.port = port
        self.baudrate = baudrate
        self._hardware_interface = {
            "name": "hardware_interface",
            "write": "send_command",
            "read": "read_data",
        }
        self._query_lock = Lock()

        self.hardware_interface = Serial(baudrate=baudrate, port=port)

        BaseROS2DeviceNode.__init__(
            self, driver_instance=self, registry_name=registry_name,
            device_id=device_id, status_types={}, action_value_mappings={},
            hardware_interface=self._hardware_interface, print_publish=False,
        )
        self.create_service(SerialCommand, "serialwrite", self.handle_serial_request)

    def send_command(self, command: str):
        with self._query_lock:
            self.hardware_interface.write(bytearray(f"{command}\n", "ascii"))
            return self.hardware_interface.read_until(b"\n").decode()

    def read_data(self):
        with self._query_lock:
            return self.hardware_interface.read_until(b"\n").decode()
```

在图文件中使用 `"class": "serial"` 即可创建串口代理：

```json
{
  "id": "serial_pump",
  "class": "serial",
  "parent": "my_station",
  "config": { "port": "COM7", "baudrate": 9600 }
}
```

### 图文件配置

**通信设备必须在 `children` 列表中排在最前面**，确保先于其他子设备初始化：

```json
{
  "nodes": [
    {
      "id": "my_station",
      "class": "workstation",
      "children": ["serial_pump", "pump_1", "pump_2"],
      "config": { "protocol_type": ["PumpTransferProtocol"] }
    },
    {
      "id": "serial_pump",
      "class": "serial",
      "parent": "my_station",
      "config": { "port": "COM7", "baudrate": 9600 }
    },
    {
      "id": "pump_1",
      "class": "syringe_pump_with_valve.runze.SY03B-T08",
      "parent": "my_station",
      "config": { "port": "serial_pump", "address": "1", "max_volume": 25.0 }
    },
    {
      "id": "pump_2",
      "class": "syringe_pump_with_valve.runze.SY03B-T08",
      "parent": "my_station",
      "config": { "port": "serial_pump", "address": "2", "max_volume": 25.0 }
    }
  ],
  "links": [
    {
      "source": "pump_1",
      "target": "serial_pump",
      "type": "communication",
      "port": { "pump_1": "port", "serial_pump": "port" }
    },
    {
      "source": "pump_2",
      "target": "serial_pump",
      "type": "communication",
      "port": { "pump_2": "port", "serial_pump": "port" }
    }
  ]
}
```

### 通信协议速查

| 协议                 | config 参数                    | 依赖包     | 通信设备 class             |
| -------------------- | ------------------------------ | ---------- | -------------------------- |
| Serial (RS232/RS485) | `port`, `baudrate`             | `pyserial` | `serial`                   |
| Modbus RTU           | `port`, `baudrate`, `slave_id` | `pymodbus` | `device_comms/modbus_plc/` |
| Modbus TCP           | `host`, `port`, `slave_id`     | `pymodbus` | `device_comms/modbus_plc/` |
| TCP Socket           | `host`, `port`                 | stdlib     | 自定义                     |
| HTTP API             | `url`, `token`                 | `requests` | `device_comms/rpc.py`      |

参考实现：`unilabos/test/experiments/Grignard_flow_batchreact_single_pumpvalve.json`

---

## Deck 与物料生命周期

### 1. Deck 入参与两种初始化模式

系统根据设备节点 `config.deck` 的写法，自动反序列化 Deck 实例后传入 `__init__` 的 `deck` 参数。目前 `deck` 是固定字段名，只支持一个主 Deck。建议一个设备拥有一个台面，台面上抽象二级、三级子物料。

有两种初始化模式：

#### init 初始化（推荐）

`config.deck` 直接包含 `_resource_type` + `_resource_child_name`，系统先用 Deck 节点的 `config` 调用 Deck 类的 `__init__` 反序列化，再将实例传入设备的 `deck` 参数。子物料随 Deck 的 `children` 一起反序列化。

```json
"config": {
    "deck": {
        "_resource_type": "unilabos.devices.liquid_handling.prcxi.prcxi:PRCXI9300Deck",
        "_resource_child_name": "PRCXI_Deck"
    }
}
```

#### deserialize 初始化

`config.deck` 用 `data` 包裹一层，系统走 `deserialize` 路径，可传入更多参数（如 `allow_marshal` 等）：

```json
"config": {
    "deck": {
        "data": {
            "_resource_child_name": "YB_Bioyond_Deck",
            "_resource_type": "unilabos.resources.bioyond.decks:BIOYOND_YB_Deck"
        }
    }
}
```

没有特殊需求时推荐 init 初始化。

#### config.deck 字段说明

| 字段 | 说明 |
|------|------|
| `_resource_type` | Deck 类的完整模块路径（`module:ClassName`） |
| `_resource_child_name` | 对应图文件中 Deck 节点的 `id`，建立父子关联 |

#### 设备 __init__ 接收

```python
def __init__(self, config=None, deck=None, protocol_type=None, **kwargs):
    super().__init__(deck=deck, **kwargs)
    # deck 已经是反序列化后的 Deck 实例
    # → PRCXI9300Deck / BIOYOND_YB_Deck 等
```

#### Deck 节点（图文件中）

Deck 节点作为设备的 `children` 之一，`parent` 指向设备 id：

```json
{
    "id": "PRCXI_Deck",
    "parent": "PRCXI",
    "type": "deck",
    "class": "",
    "children": [],
    "config": {
        "type": "PRCXI9300Deck",
        "size_x": 542, "size_y": 374, "size_z": 0,
        "category": "deck",
        "sites": [...]
    },
    "data": {}
}
```

- `config` 中的字段会传入 Deck 类的 `__init__`（因此 `__init__` 必须能接受所有 `serialize()` 输出的字段）
- `children` 初始为空时，由同步器或手动初始化填充
- `config.type` 填 Deck 类名

### 2. Deck 为空时自行初始化

如果 Deck 节点的 `children` 为空，工作站需在 `post_init` 或首次同步时自行初始化内容：

```python
@not_action
def post_init(self, ros_node):
    super().post_init(ros_node)
    if self.deck and not self.deck.children:
        self._initialize_default_deck()

def _initialize_default_deck(self):
    from my_labware import My_TipRack, My_Plate
    self.deck.assign_child_resource(My_TipRack("T1"), spot=0)
    self.deck.assign_child_resource(My_Plate("T2"), spot=1)
```

### 3. 物料双向同步

当工作站对接外部系统（LIMS/MES）时，需要实现 `ResourceSynchronizer` 处理双向物料同步：

```python
from unilabos.devices.workstation.workstation_base import ResourceSynchronizer

class MyResourceSynchronizer(ResourceSynchronizer):
    def sync_from_external(self) -> bool:
        """从外部系统同步到 self.workstation.deck"""
        external_data = self._query_external_materials()
        # 以外部工站为准：根据外部数据反向创建 PLR 资源实例
        for item in external_data:
            cls = self._resolve_resource_class(item["type"])
            resource = cls(name=item["name"], **item["params"])
            self.workstation.deck.assign_child_resource(resource, spot=item["slot"])
        return True

    def sync_to_external(self, resource) -> bool:
        """将 UniLab 侧物料变更同步到外部系统"""
        # 以 UniLab 为准：将 PLR 资源转为外部格式并推送
        external_format = self._convert_to_external(resource)
        return self._push_to_external(external_format)

    def handle_external_change(self, change_info) -> bool:
        """处理外部系统主动推送的变更"""
        return True
```

同步策略取决于业务场景：

- **以外部工站为准**：从外部 API 查询物料数据，反向创建对应的 PLR 资源实例放到 Deck 上
- **以 UniLab 为准**：UniLab 侧的物料变更通过 `sync_to_external` 推送到外部系统

在工作站 `post_init` 中初始化同步器：

```python
@not_action
def post_init(self, ros_node):
    super().post_init(ros_node)
    self.resource_synchronizer = MyResourceSynchronizer(self)
    self.resource_synchronizer.sync_from_external()
```

### 4. 序列化与持久化（serialize / serialize_state）

资源类需正确实现序列化，系统据此完成持久化和前端同步。

**`serialize()`** — 输出资源的结构信息（`config` 层），反序列化时作为 `__init__` 的入参回传。因此 **`__init__` 必须通过 `**kwargs`接受`serialize()` 输出的所有字段\*\*，即使当前不使用：

```python
class MyDeck(Deck):
    def __init__(self, name, size_x, size_y, size_z,
                 sites=None,          # serialize() 输出的字段
                 rotation=None,       # serialize() 输出的字段
                 barcode=None,        # serialize() 输出的字段
                 **kwargs):           # 兜底：接受所有未知的 serialize 字段
        super().__init__(size_x, size_y, size_z, name)
        # ...

    def serialize(self) -> dict:
        data = super().serialize()
        data["sites"] = [...]  # 自定义字段
        return data
```

**`serialize_state()`** — 输出资源的运行时状态（`data` 层），用于持久化可变信息。`data` 中的内容会被正确保存和恢复：

```python
class MyPlate(Plate):
    def __init__(self, name, size_x, size_y, size_z,
                 material_info=None, **kwargs):
        super().__init__(name, size_x, size_y, size_z, **kwargs)
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def serialize_state(self) -> Dict[str, Any]:
        data = super().serialize_state()
        data.update(self._unilabos_state)
        return data
```

关键要点：

- `serialize()` 输出的所有字段都会作为 `config` 回传到 `__init__`，所以 `__init__` 必须能接受它们（显式声明或 `**kwargs`）
- `serialize_state()` 输出的 `data` 用于持久化运行时状态（如物料信息、液体量等）
- `_unilabos_state` 中只存可 JSON 序列化的基本类型（str, int, float, bool, list, dict, None）

### 5. 子物料自动同步

子物料（Bottle、Plate、TipRack 等）放到 Deck 上后，系统会自动将其同步到前端的 Deck 视图。只需保证资源类正确实现了 `serialize()` / `serialize_state()` 和反序列化即可。

### 6. 图文件配置（参考 prcxi_9320_slim.json）

```json
{
  "nodes": [
    {
      "id": "my_station",
      "type": "device",
      "class": "my_workstation",
      "config": {
        "deck": {
          "_resource_type": "unilabos.resources.my_module:MyDeck",
          "_resource_child_name": "my_deck"
        },
        "host": "10.20.30.1",
        "port": 9999
      }
    },
    {
      "id": "my_deck",
      "parent": "my_station",
      "type": "deck",
      "class": "",
      "children": [],
      "config": {
        "type": "MyLabDeck",
        "size_x": 542,
        "size_y": 374,
        "size_z": 0,
        "category": "deck",
        "sites": [
          {
            "label": "T1",
            "visible": true,
            "occupied_by": null,
            "position": { "x": 0, "y": 0, "z": 0 },
            "size": { "width": 128.0, "height": 86, "depth": 0 },
            "content_type": ["plate", "tip_rack", "tube_rack", "adaptor"]
          }
        ]
      },
      "data": {}
    }
  ],
  "edges": []
}
```

Deck 节点要点：

- `config.type` 填 Deck 类名（如 `"PRCXI9300Deck"`）
- `config.sites` 完整列出所有 site（从 Deck 类的 `serialize()` 输出获取）
- `children` 初始为空（由同步器或手动初始化填充）
- 设备节点 `config.deck._resource_type` 指向 Deck 类的完整模块路径

---

## 子设备

子设备按标准设备接入流程创建（参见 add-device SKILL），使用 `@device` 装饰器。

子设备约束：

- 图文件中 `parent` 指向工作站 ID
- 在工作站 `children` 数组中列出

---

## 关键规则

1. **`__init__` 必须接受 `deck` 和 `**kwargs`** — `WorkstationBase.**init**`需要`deck` 参数
2. **Deck 通过 `config.deck._resource_type` 反序列化传入** — 不要在 `__init__` 中手动创建 Deck
3. **Deck 为空时自行初始化内容** — 在 `post_init` 中检查并填充默认物料
4. **外部同步实现 `ResourceSynchronizer`** — `sync_from_external` / `sync_to_external`
5. **通过 `self._children` 访问子设备** — 不要自行维护子设备引用
6. **`post_init` 中启动后台服务** — 不要在 `__init__` 中启动网络连接
7. **异步方法使用 `await self._ros_node.sleep()`** — 禁止 `time.sleep()` 和 `asyncio.sleep()`
8. **使用 `@not_action` 标记非动作方法** — `post_init`, `initialize`, `cleanup`
9. **子物料保证正确 serialize/deserialize** — 系统自动同步到前端 Deck 视图

---

## 验证

```bash
# 模块可导入
python -c "from unilabos.devices.workstation.<name>.<name> import <ClassName>"

# 启动测试（AST 自动扫描）
unilab -g <graph>.json
```

---

## 现有工作站参考

| 工作站         | 驱动类                        | 类型     |
| -------------- | ----------------------------- | -------- |
| Protocol 通用  | `ProtocolNode`                | Protocol |
| Bioyond 反应站 | `BioyondReactionStation`      | 外部系统 |
| 纽扣电池组装   | `CoinCellAssemblyWorkstation` | 硬件控制 |

参考路径：`unilabos/devices/workstation/` 目录下各工作站实现。
