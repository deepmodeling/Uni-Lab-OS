# 设备图文件说明

设备图文件定义了实验室中所有设备、资源及其连接关系。本文档说明如何创建和使用设备图文件。

## 概述

设备图文件采用 JSON 格式，节点定义基于 **`ResourceDict`** 标准模型（定义在 `unilabos.ros.nodes.resource_tracker`）。系统会自动处理旧格式并转换为标准格式，确保向后兼容性。

**核心概念**:

- **Nodes（节点）**: 代表设备或资源，通过 `parent` 字段建立层级关系
- **Links（连接）**: 可选的连接关系定义，用于展示设备间的物理或通信连接
- **UUID**: 全局唯一标识符，用于跨系统的资源追踪
- **自动转换**: 旧格式会通过 `ResourceDictInstance.get_resource_instance_from_dict()` 自动转换

## 文件格式

Uni-Lab 支持两种格式的设备图文件：

### JSON 格式（推荐）

**优点**:

- 易于编辑和阅读
- 支持注释（使用预处理）
- 与 Web 界面完全兼容
- 便于版本控制

**示例**: `workshop1.json`

### GraphML 格式

**优点**:

- 可用图形化工具编辑（如 yEd）
- 适合复杂拓扑可视化

**示例**: `setup.graphml`

## JSON 文件结构

一个完整的 JSON 设备图文件包含两个主要部分：

```json
{
  "nodes": [
    /* 设备和资源节点 */
  ],
  "links": [
    /* 连接关系（可选）*/
  ]
}
```

### Nodes（节点）

每个节点代表一个设备或资源。节点的定义遵循 `ResourceDict` 标准模型：

```json
{
  "id": "liquid_handler_1",
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "name": "液体处理工作站",
  "type": "device",
  "class": "liquid_handler",
  "config": {
    "port": "/dev/ttyUSB0",
    "baudrate": 9600
  },
  "data": {},
  "position": {
    "x": 100,
    "y": 200
  },
  "parent": null
}
```

**字段说明（基于 ResourceDict 标准定义）**:

| 字段          | 必需 | 说明                     | 示例                                                 | 默认值   |
| ------------- | ---- | ------------------------ | ---------------------------------------------------- | -------- |
| `id`          | ✓    | 唯一标识符               | `"pump_1"`                                           | -        |
| `uuid`        |      | 全局唯一标识符 (UUID)    | `"550e8400-e29b-41d4-a716-446655440000"`             | 自动生成 |
| `name`        | ✓    | 显示名称                 | `"主反应泵"`                                         | -        |
| `type`        | ✓    | 节点类型                 | `"device"`, `"resource"`, `"container"`, `"deck"` 等 | -        |
| `class`       | ✓    | 设备/资源类别            | `"liquid_handler"`, `"syringepump.runze"`            | `""`     |
| `config`      |      | Python 类的初始化参数    | `{"port": "COM3"}`                                   | `{}`     |
| `data`        |      | 资源的运行状态数据       | `{"status": "Idle", "position": 0.0}`                | `{}`     |
| `position`    |      | 在图中的位置             | `{"x": 100, "y": 200}` 或完整的 pose 结构            | -        |
| `pose`        |      | 完整的 3D 位置信息       | 参见下文                                             | -        |
| `parent`      |      | 父节点 ID                | `"deck_1"`                                           | `null`   |
| `parent_uuid` |      | 父节点 UUID              | `"550e8400-..."`                                     | `null`   |
| `children`    |      | 子节点 ID 列表（旧格式） | `["child1", "child2"]`                               | -        |
| `description` |      | 资源描述                 | `"用于精确控制试剂A的加料速率"`                      | `""`     |
| `schema`      |      | 资源 schema 定义         | `{}`                                                 | `{}`     |
| `model`       |      | 资源 3D 模型信息         | `{}`                                                 | `{}`     |
| `icon`        |      | 资源图标                 | `"pump.webp"`                                        | `""`     |
| `extra`       |      | 额外的自定义数据         | `{"custom_field": "value"}`                          | `{}`     |

### Position 和 Pose（位置信息）

**简单格式（旧格式，兼容）**:

```json
"position": {
  "x": 100,
  "y": 200,
  "z": 0
}
```

**完整格式（推荐）**:

```json
"pose": {
  "size": {
    "width": 127.76,
    "height": 85.48,
    "depth": 10.0
  },
  "scale": {
    "x": 1.0,
    "y": 1.0,
    "z": 1.0
  },
  "layout": "x-y",
  "position": {
    "x": 100,
    "y": 200,
    "z": 0
  },
  "position3d": {
    "x": 100,
    "y": 200,
    "z": 0
  },
  "rotation": {
    "x": 0,
    "y": 0,
    "z": 0
  },
  "cross_section_type": "rectangle"
}
```

### Links（连接）

定义节点之间的连接关系（可选，主要用于物理连接或通信关系的可视化）：

```json
{
  "source": "pump_1",
  "target": "reactor_1",
  "sourceHandle": "output",
  "targetHandle": "input",
  "type": "physical"
}
```

**字段说明**:

| 字段           | 必需 | 说明             | 示例                                     |
| -------------- | ---- | ---------------- | ---------------------------------------- |
| `source`       | ✓    | 源节点 ID        | `"pump_1"`                               |
| `target`       | ✓    | 目标节点 ID      | `"reactor_1"`                            |
| `sourceHandle` |      | 源节点的连接点   | `"output"`                               |
| `targetHandle` |      | 目标节点的连接点 | `"input"`                                |
| `type`         |      | 连接类型         | `"physical"`, `"communication"`          |
| `port`         |      | 端口映射信息     | `{"source": "port1", "target": "port2"}` |

**注意**: Links 主要用于图形化展示和文档说明，父子关系通过 `parent` 字段定义，不依赖 links。

## 完整示例

### 示例 1：液体处理工作站（PRCXI9300）

这是一个真实的液体处理工作站配置，包含设备、工作台和多个板资源。

**文件位置**: `test/experiments/prcxi_9300.json`

```json
{
  "nodes": [
    {
      "id": "PRCXI9300",
      "name": "PRCXI9300",
      "parent": null,
      "type": "device",
      "class": "liquid_handler.prcxi",
      "position": {
        "x": 0,
        "y": 0,
        "z": 0
      },
      "config": {
        "deck": {
          "_resource_child_name": "PRCXI_Deck_9300",
          "_resource_type": "unilabos.devices.liquid_handling.prcxi.prcxi:PRCXI9300Deck"
        },
        "host": "10.181.214.132",
        "port": 9999,
        "timeout": 10.0,
        "axis": "Left",
        "channel_num": 8,
        "setup": false,
        "debug": true,
        "simulator": true,
        "matrix_id": "71593"
      },
      "data": {},
      "children": ["PRCXI_Deck_9300"]
    },
    {
      "id": "PRCXI_Deck_9300",
      "name": "PRCXI_Deck_9300",
      "parent": "PRCXI9300",
      "type": "deck",
      "class": "",
      "position": {
        "x": 0,
        "y": 0,
        "z": 0
      },
      "config": {
        "type": "PRCXI9300Deck",
        "size_x": 100,
        "size_y": 100,
        "size_z": 100,
        "rotation": {
          "x": 0,
          "y": 0,
          "z": 0,
          "type": "Rotation"
        },
        "category": "deck"
      },
      "data": {},
      "children": [
        "RackT1",
        "PlateT2",
        "trash",
        "PlateT4",
        "PlateT5",
        "PlateT6"
      ]
    },
    {
      "id": "RackT1",
      "name": "RackT1",
      "parent": "PRCXI_Deck_9300",
      "type": "tip_rack",
      "class": "",
      "position": {
        "x": 0,
        "y": 0,
        "z": 0
      },
      "config": {
        "type": "TipRack",
        "size_x": 127.76,
        "size_y": 85.48,
        "size_z": 100
      },
      "data": {},
      "children": []
    }
  ]
}
```

**关键点**:

- 使用 `parent` 字段建立层级关系（PRCXI9300 → Deck → Rack/Plate）
- 使用 `children` 字段（旧格式）列出子节点
- `config` 中包含设备特定的连接参数
- `data` 存储运行时状态
- `position` 使用简单的 x/y/z 坐标

### 示例 2：有机合成工作站（带 Links）

这是一个格林纳德反应的流动化学工作站配置，展示了完整的设备连接和通信关系。

**文件位置**: `test/experiments/Grignard_flow_batchreact_single_pumpvalve.json`

```json
{
  "nodes": [
    {
      "id": "YugongStation",
      "name": "愚公常量合成工作站",
      "parent": null,
      "type": "device",
      "class": "workstation",
      "position": {
        "x": 620.6111111111111,
        "y": 171,
        "z": 0
      },
      "config": {
        "protocol_type": [
          "PumpTransferProtocol",
          "CleanProtocol",
          "SeparateProtocol",
          "EvaporateProtocol"
        ]
      },
      "data": {},
      "children": [
        "serial_pump",
        "pump_reagents",
        "flask_CH2Cl2",
        "reactor",
        "pump_workup",
        "separator_controller",
        "flask_separator",
        "rotavap",
        "column"
      ]
    },
    {
      "id": "serial_pump",
      "name": "serial_pump",
      "parent": "YugongStation",
      "type": "device",
      "class": "serial",
      "position": {
        "x": 620.6111111111111,
        "y": 171,
        "z": 0
      },
      "config": {
        "port": "COM7",
        "baudrate": 9600
      },
      "data": {},
      "children": []
    },
    {
      "id": "pump_reagents",
      "name": "pump_reagents",
      "parent": "YugongStation",
      "type": "device",
      "class": "syringepump.runze",
      "position": {
        "x": 620.6111111111111,
        "y": 171,
        "z": 0
      },
      "config": {
        "port": "/devices/PumpBackbone/Serial/serialwrite",
        "address": "1",
        "max_volume": 25.0
      },
      "data": {
        "max_velocity": 1.0,
        "position": 0.0,
        "status": "Idle",
        "valve_position": "0"
      },
      "children": []
    },
    {
      "id": "reactor",
      "name": "reactor",
      "parent": "YugongStation",
      "type": "container",
      "class": null,
      "position": {
        "x": 430.4087301587302,
        "y": 428,
        "z": 0
      },
      "config": {},
      "data": {},
      "children": []
    }
  ],
  "links": [
    {
      "source": "pump_reagents",
      "target": "serial_pump",
      "type": "communication",
      "port": {
        "pump_reagents": "port",
        "serial_pump": "port"
      }
    },
    {
      "source": "pump_workup",
      "target": "serial_pump",
      "type": "communication",
      "port": {
        "pump_workup": "port",
        "serial_pump": "port"
      }
    }
  ]
}
```

**关键点**:

- 多级设备层次：工作站包含多个子设备和容器
- `links` 定义通信关系（泵通过串口连接）
- `data` 字段存储设备状态（如泵的位置、速度等）
- `class` 可以使用点号分层（如 `"syringepump.runze"`）
- 容器的 `class` 可以为 `null`

## 格式兼容性和转换

### 旧格式自动转换

Uni-Lab 使用 `ResourceDictInstance.get_resource_instance_from_dict()` 方法自动处理旧格式的节点数据，确保向后兼容性。

**自动转换规则**:

1. **自动生成缺失字段**:

   ```python
   # 如果缺少 id，使用 name 作为 id
   if "id" not in content:
       content["id"] = content["name"]

   # 如果缺少 uuid，自动生成
   if "uuid" not in content:
       content["uuid"] = str(uuid.uuid4())
   ```

2. **Position 格式转换**:

   ```python
   # 旧格式：简单的 x/y 坐标
   "position": {"x": 100, "y": 200}

   # 自动转换为新格式
   "position": {
       "position": {"x": 100, "y": 200}
   }
   ```

3. **默认值填充**:

   ```python
   # 自动填充空字段
   if not content.get("class"):
       content["class"] = ""
   if not content.get("config"):
       content["config"] = {}
   if not content.get("data"):
       content["data"] = {}
   if not content.get("extra"):
       content["extra"] = {}
   ```

4. **Pose 字段同步**:
   ```python
   # 如果没有 pose，使用 position
   if "pose" not in content:
       content["pose"] = content.get("position", {})
   ```

### 使用示例

```python
from unilabos.resources.resource_tracker import ResourceDictInstance

# 旧格式节点
old_format_node = {
    "name": "pump_1",
    "type": "device",
    "class": "syringepump",
    "position": {"x": 100, "y": 200}
}

# 自动转换为标准格式
instance = ResourceDictInstance.get_resource_instance_from_dict(old_format_node)

# 访问标准化后的数据
print(instance.res_content.id)  # "pump_1"
print(instance.res_content.uuid)  # 自动生成的 UUID
print(instance.res_content.config)  # {}
print(instance.res_content.data)  # {}
```

### 格式迁移建议

虽然系统会自动处理旧格式，但建议在新文件中使用完整的标准格式：

| 字段   | 旧格式（兼容）                     | 新格式（推荐）                                   |
| ------ | ---------------------------------- | ------------------------------------------------ |
| 标识符 | 仅 `id` 或仅 `name`                | `id` + `uuid`                                    |
| 位置   | `"position": {"x": 100, "y": 200}` | 完整的 `pose` 结构                               |
| 父节点 | `"parent": "parent_id"`            | `"parent": "parent_id"` + `"parent_uuid": "..."` |
| 配置   | 可省略                             | 显式设置为 `{}`                                  |
| 数据   | 可省略                             | 显式设置为 `{}`                                  |

## 节点类型详解

### Device 节点

设备节点代表实际的硬件设备：

```json
{
  "id": "device_id",
  "name": "设备名称",
  "type": "device",
  "class": "设备类别",
  "parent": null,
  "config": {
    "port": "COM3"
  },
  "data": {},
  "children": []
}
```

**常见设备类别**:

- `liquid_handler`: 液体处理工作站
- `liquid_handler.prcxi`: PRCXI 液体处理工作站
- `syringepump`: 注射泵
- `syringepump.runze`: 润泽注射泵
- `heaterstirrer`: 加热搅拌器
- `balance`: 天平
- `reactor_vessel`: 反应釜
- `serial`: 串口通信设备
- `workstation`: 自动化工作站

### Resource 节点

资源节点代表物料容器、载具等：

```json
{
  "id": "resource_id",
  "name": "资源名称",
  "type": "resource",
  "class": "资源类别",
  "parent": "父节点ID",
  "config": {
    "size_x": 127.76,
    "size_y": 85.48,
    "size_z": 100
  },
  "data": {},
  "children": []
}
```

**常见资源类型**:

- `deck`: 工作台/甲板
- `plate`: 板（96 孔板等）
- `tip_rack`: 枪头架
- `tube`: 试管
- `container`: 容器
- `well`: 孔位
- `bottle_carrier`: 瓶架

## Handle（连接点）

每个设备和资源可以有多个连接点（handles），用于定义可以连接的接口。

### 查看可用 handles

设备和资源的可用 handles 定义在注册表中：

```yaml
# 设备注册表示例
liquid_handler:
  handles:
    - handler_key: pipette
      io_type: source
    - handler_key: deck
      io_type: target
```

### 常见 handles

| 设备类型   | Source Handles | Target Handles |
| ---------- | -------------- | -------------- |
| 泵         | output         | input          |
| 反应釜     | output, vessel | input          |
| 液体处理器 | pipette        | deck           |
| 板         | wells          | access         |

## 使用 Web 界面创建图文件

Uni-Lab 提供 Web 界面来可视化创建和编辑设备图：

### 1. 启动 Uni-Lab

```bash
unilab
```

### 2. 访问 Web 界面

打开浏览器访问 `http://localhost:8002`

### 3. 图形化编辑

- 拖拽添加设备和资源
- 连线建立连接关系
- 编辑节点属性
- 保存为 JSON 文件

### 4. 导出图文件

点击"导出"按钮，下载 JSON 文件到本地。

## 从云端获取图文件

如果不指定`-g`参数，Uni-Lab 会自动从云端获取：

```bash
# 使用云端配置
unilab

# 日志会显示:
# [INFO] 未指定设备加载文件路径，尝试从HTTP获取...
# [INFO] 联网获取设备加载文件成功
```

**云端图文件管理**:

1. 登录 https://leap-lab.bohrium.com
2. 进入"设备配置"
3. 创建或编辑配置
4. 保存到云端

本地启动时会自动同步最新配置。

## 调试图文件

### 验证 JSON 格式

```bash
# 使用Python验证
python -c "import json; json.load(open('workshop1.json'))"

# 使用在线工具
# https://jsonlint.com/
```

### 检查节点引用

确保：

- 所有`links`中的`source`和`target`都存在于`nodes`中
- `parent`字段指向的节点存在
- `class`字段对应的设备/资源在注册表中存在

### 启动时验证

```bash
# Uni-Lab启动时会验证图文件
unilab -g workshop1.json

# 查看日志中的错误或警告
# [ERROR] 节点 xxx 的source端点 yyy 不存在
# [WARNING] 节点 zzz missing 'name', defaulting to ...
```

## 最佳实践

### 1. 命名规范

```json
{
  "id": "pump_reagent_1", // 小写+下划线，描述性
  "name": "试剂进料泵A", // 中文显示名称
  "class": "syringepump" // 使用注册表中的精确名称
}
```

### 2. 层级组织

```
host_node (主节点)
└── liquid_handler_1 (设备)
    └── deck_1 (资源)
        ├── tiprack_1 (资源)
        ├── plate_1 (资源)
        └── reservoir_1 (资源)
```

### 3. 配置分离

将设备特定配置放在`config`中：

```json
{
  "id": "pump_1",
  "class": "syringepump",
  "config": {
    "port": "COM3", // 设备特定
    "max_flow_rate": 10, // 设备特定
    "volume": 50 // 设备特定
  }
}
```

### 4. 版本控制

```bash
# 使用Git管理图文件
git add workshop1.json
git commit -m "Add new liquid handler configuration"

# 使用有意义的文件名
workshop_v1.json
workshop_production.json
workshop_test.json
```

### 5. 注释（通过描述字段）

虽然 JSON 不支持注释，但可以使用`description`字段：

```json
{
  "id": "pump_1",
  "name": "进料泵",
  "description": "用于精确控制试剂A的加料速率，最大流速10mL/min",
  "class": "syringepump"
}
```

## 示例文件位置

Uni-Lab 在安装时已预置了 **40+ 个真实的设备图文件示例**，位于 `unilabos/test/experiments/` 目录。这些都是真实项目中使用的配置文件，可以直接使用或作为参考。

### 📁 主要示例文件

```
test/experiments/
├── workshop.json                                 # 综合工作台（推荐新手）
├── empty_devices.json                            # 空设备配置（最小化）
├── prcxi_9300.json                               # PRCXI液体处理工作站（本文示例1）
├── prcxi_9320.json                               # PRCXI 9320工作站
├── biomek.json                                   # Biomek液体处理工作站
├── Grignard_flow_batchreact_single_pumpvalve.json # 格林纳德反应工作站（本文示例2）
├── dispensing_station_bioyond.json               # Bioyond配液站
├── reaction_station_bioyond.json                 # Bioyond反应站
├── HPLC.json                                     # HPLC分析系统
├── plr_test.json                                 # PyLabRobot测试配置
├── lidocaine-graph.json                          # 利多卡因合成工作站
├── opcua_example.json                            # OPC UA设备集成示例
│
├── mock_devices/                                 # 虚拟设备（用于离线测试）
│   ├── mock_all.json                             # 完整虚拟设备集
│   ├── mock_pump.json                            # 虚拟泵
│   ├── mock_stirrer.json                         # 虚拟搅拌器
│   ├── mock_heater.json                          # 虚拟加热器
│   └── ...                                       # 更多虚拟设备
│
├── Protocol_Test_Station/                        # 协议测试工作站
│   ├── pumptransfer_test_station.json            # 泵转移协议测试
│   ├── heatchill_protocol_test_station.json      # 加热冷却协议测试
│   ├── filter_protocol_test_station.json         # 过滤协议测试
│   └── ...                                       # 更多协议测试
│
└── comprehensive_protocol/                       # 综合协议示例
    ├── comprehensive_station.json                # 综合工作站
    └── comprehensive_slim.json                   # 精简版综合工作站
```

### 🚀 快速使用

无需下载或创建，直接使用 `-g` 参数指定路径：

```bash
# 使用简单工作台（推荐新手）
unilab --ak your_ak --sk your_sk -g test/experiments/workshop.json

# 使用虚拟设备（无需真实硬件）
unilab --ak your_ak --sk your_sk -g test/experiments/mock_devices/mock_all.json

# 使用 PRCXI 液体处理工作站
unilab --ak your_ak --sk your_sk -g test/experiments/prcxi_9300.json

# 使用格林纳德反应工作站
unilab --ak your_ak --sk your_sk -g test/experiments/Grignard_flow_batchreact_single_pumpvalve.json
```

### 📚 文件分类

| 类别         | 说明                     | 文件数量 |
| ------------ | ------------------------ | -------- |
| **主工作站** | 完整的实验工作站配置     | 15+      |
| **虚拟设备** | 用于开发测试的 mock 设备 | 10+      |
| **协议测试** | 各种实验协议的测试配置   | 12+      |
| **综合示例** | 包含多种协议的综合工作站 | 3+       |

这些文件展示了不同场景下的设备图配置，涵盖液体处理、有机合成、分析检测等多个领域，是学习和创建自己配置的绝佳参考。

## 快速参考：ResourceDict 完整字段列表

基于 `unilabos.ros.nodes.resource_tracker.ResourceDict` 的完整字段定义：

```python
class ResourceDict(BaseModel):
    # === 基础标识 ===
    id: str                          # 资源ID（必需）
    uuid: str                        # 全局唯一标识符（自动生成）
    name: str                        # 显示名称（必需）

    # === 类型和分类 ===
    type: Union[Literal["device"], str]  # 节点类型（必需）
    klass: str                       # 资源类别（alias="class"，必需）

    # === 层级关系 ===
    parent: Optional[ResourceDict]   # 父资源对象（不序列化）
    parent_uuid: Optional[str]       # 父资源UUID

    # === 位置和姿态 ===
    position: ResourceDictPosition   # 位置信息
    pose: ResourceDictPosition       # 姿态信息（推荐使用）

    # === 配置和数据 ===
    config: Dict[str, Any]           # 设备配置参数
    data: Dict[str, Any]             # 运行时状态数据
    extra: Dict[str, Any]            # 额外自定义数据

    # === 元数据 ===
    description: str                 # 资源描述
    resource_schema: Dict[str, Any]  # schema定义（alias="schema"）
    model: Dict[str, Any]            # 3D模型信息
    icon: str                        # 图标路径
```

**Position/Pose 结构**:

```python
class ResourceDictPosition(BaseModel):
    size: ResourceDictPositionSize           # width, height, depth
    scale: ResourceDictPositionScale         # x, y, z
    layout: Literal["2d", "x-y", "z-y", "x-z"]
    position: ResourceDictPositionObject     # x, y, z
    position3d: ResourceDictPositionObject   # x, y, z
    rotation: ResourceDictPositionObject     # x, y, z
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"]
```

## 下一步

- {doc}`../boot_examples/index` - 查看完整启动示例
- {doc}`../developer_guide/add_device` - 了解如何添加新设备
- {doc}`06_troubleshooting` - 图文件相关问题排查
- 源码参考: `unilabos/ros/nodes/resource_tracker.py` - ResourceDict 标准定义

## 获取帮助

- 在 Web 界面中使用模板创建
- 参考示例文件：`test/experiments/` 目录
- 查看 ResourceDict 源码了解完整定义
- [GitHub 讨论区](https://github.com/deepmodeling/Uni-Lab-OS/discussions)
