# 添加设备：注册表配置完整指南

本文档说明如何为设备创建和配置注册表，包括基本结构、特殊类型识别、动作配置等内容。

## 概述

注册表（Registry）是Uni-Lab的设备配置系统，采用YAML格式定义设备的：
- 可用动作（Actions）
- 状态类型（Status Types）
- 初始化参数（Init Parameters）
- 连接点（Handles）

好消息是系统会自动生成大部分配置内容，你只需要提供核心信息，让系统帮你完成剩余工作。

## 快速开始：使用注册表编辑器

推荐使用 UniLabOS 自带的可视化编辑器，它能帮你自动生成大部分配置，省去手写的麻烦。

### 使用步骤

1. 启动 UniLabOS
2. 在浏览器中打开"注册表编辑器"页面
3. 上传你的 Python 设备驱动文件
4. 点击"分析文件"，让系统读取类信息
5. 填写基本信息（设备描述、图标等）
6. 点击"生成注册表"，复制生成的内容
7. 保存到 `unilabos/registry/devices/your_device.yaml`

**提示**：我们提供了测试驱动用于在界面上尝试注册表生成，参见：`test/registry/example_devices.py`

## 注册表的基本结构

### 核心字段说明

| 字段名            | 类型   | 需要手写 | 说明                                |
| ----------------- | ------ | -------- | ----------------------------------- |
| 设备标识符        | string | 是       | 设备的唯一名字，如 `mock_chiller`   |
| class             | object | 部分     | 设备的核心信息，必须配置            |
| description       | string | 否       | 设备描述，系统默认给空字符串        |
| handles           | array  | 否       | 连接关系，默认为空                  |
| icon              | string | 否       | 图标路径，默认为空                  |
| init_param_schema | object | 否       | 初始化参数，系统自动分析生成        |
| version           | string | 否       | 版本号，默认 "1.0.0"                |
| category          | array  | 否       | 设备分类，默认使用文件名            |
| config_info       | array  | 否       | 嵌套配置，默认为空                  |
| file_path         | string | 否       | 文件路径，系统自动设置              |
| registry_type     | string | 否       | 注册表类型，自动设为 "device"       |

### class 字段详解

class 是核心部分，包含这些内容：

| 字段名                | 类型   | 需要手写 | 说明                               |
| --------------------- | ------ | -------- | ---------------------------------- |
| module                | string | 是       | Python 类的路径，必须写            |
| type                  | string | 是       | 驱动类型，一般写 "python"          |
| status_types          | object | 否       | 状态类型，系统自动分析生成         |
| action_value_mappings | object | 部分     | 动作配置，系统会自动生成一些基础的 |

### 基本结构示例

```yaml
my_device:
  class:
    module: unilabos.devices.my_module.my_device:MyDevice
    type: python
    status_types:
      status: str
      temperature: float
    action_value_mappings:
      # 动作配置（详见后文）
      action_name:
        type: UniLabJsonCommand
        goal: {...}
        result: {...}

  description: "设备描述"
  version: "1.0.0"
  category:
    - device_category
  handles: []
  icon: ''
  init_param_schema:
    config:
      properties:
        port:
          default: DEFAULT_PORT
          type: string
      required: []
      type: object
    data:
      properties:
        status:
          type: string
        temperature:
          type: number
      required:
        - status
      type: object
```

## 创建注册表的方式

### 方式1: 使用注册表编辑器（推荐）

适合大多数场景，快速高效。

**步骤**：
1. 启动Uni-Lab
2. 访问Web界面的"注册表编辑器"
3. 上传您的Python设备驱动文件
4. 点击"分析文件"
5. 填写描述和图标
6. 点击"生成注册表"
7. 复制生成的YAML内容
8. 保存到 `unilabos/registry/devices/your_device.yaml`

### 方式2: 使用--complete_registry参数（开发调试）

适合开发阶段，自动补全配置。

```bash
# 启动时自动补全注册表
unilab -g dev.json --complete_registry --registry_path ./my_registry
```

系统会：
1. 扫描Python类
2. 分析方法签名和类型
3. 自动生成缺失的字段
4. 保存到注册表文件

**或者在代码中**：

```python
# 启动系统时使用参数
启动系统时用 complete_registry=True 参数，让系统自动补全
```

### 方式3: 手动编写（高级）

适合需要精细控制或特殊需求的场景。

**最小化配置示例**：

```yaml
# devices/my_device.yaml
my_device:
  class:
    module: unilabos.devices.my_module.my_device:MyDevice
    type: python
```

然后启动时使用 `--complete_registry` 让系统自动补全其余内容。

## action_value_mappings 详解

这个部分定义设备能做哪些动作。系统会自动生成大部分动作，你通常只需要添加特殊的自定义动作。

### 系统自动生成的动作

1. **以 `auto-` 开头的动作**：从 Python 类的方法自动生成
2. **通用的驱动动作**：
   - `_execute_driver_command`：同步执行驱动命令（仅本地可用）
   - `_execute_driver_command_async`：异步执行驱动命令（仅本地可用）

### 动作配置字段

| 字段名           | 需要手写 | 说明                             |
| ---------------- | -------- | -------------------------------- |
| type             | 是       | 动作类型，必须指定               |
| goal             | 是       | 输入参数映射                     |
| feedback         | 否       | 实时反馈，通常为空               |
| result           | 是       | 结果返回映射                     |
| goal_default     | 部分     | 参数默认值，ROS 动作会自动生成   |
| schema           | 部分     | 前端表单配置，ROS 动作会自动生成 |
| handles          | 否       | 连接关系，默认为空               |
| placeholder_keys | 否       | 特殊输入字段配置                 |

### 动作类型

| 类型                   | 使用场景             | 系统自动生成内容       |
| ---------------------- | -------------------- | ---------------------- |
| UniLabJsonCommand      | 自定义同步 JSON 命令 | 无                     |
| UniLabJsonCommandAsync | 自定义异步 JSON 命令 | 无                     |
| ROS 动作类型           | 标准 ROS 动作        | goal_default 和 schema |

**常用的 ROS 动作类型**：
- `SendCmd`：发送简单命令
- `NavigateThroughPoses`：导航动作
- `SingleJointPosition`：单关节位置控制
- `Stir`：搅拌动作
- `HeatChill`、`HeatChillStart`：加热冷却动作

### 动作命名建议

根据设备用途来起名字：

- **启动停止类**：`start`、`stop`、`pause`、`resume`
- **设置参数类**：`set_speed`、`set_temperature`、`set_timer`
- **移动控制类**：`move_to_position`、`move_through_points`
- **功能操作类**：`stir`、`heat_chill_start`、`heat_chill_stop`
- **开关控制类**：`valve_open_cmd`、`valve_close_cmd`、`push_to`
- **命令执行类**：`send_nav_task`、`execute_command_from_outer`

### 动作配置示例

```yaml
heat_chill_start:
  type: HeatChillStart
  goal:
    purpose: purpose
    temp: temp
  goal_default:
    purpose: ''
    temp: 0.0
  handles:
    output:
      - handler_key: labware
        label: Labware
        data_type: resource
        data_source: handle
        data_key: liquid
  placeholder_keys:
    purpose: unilabos_resources
  result:
    status: status
    success: success
  schema:
    description: '启动加热冷却功能'
    properties:
      goal:
        properties:
          purpose:
            type: string
            description: '用途说明'
          temp:
            type: number
            description: '目标温度'
        required:
          - purpose
          - temp
        title: HeatChillStart_Goal
        type: object
    required:
      - goal
    title: HeatChillStart
    type: object
  feedback: {}
```

## 特殊类型的自动识别

### ResourceSlot和DeviceSlot识别

当您在驱动代码中使用这些特殊类型时，系统会自动识别并生成相应的前端选择器。

**Python驱动代码示例**：

```python
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from typing import List

class MyDevice:
    def test_resource(
        self,
        resource: ResourceSlot,           # 单个资源
        resources: List[ResourceSlot],    # 多个资源
        device: DeviceSlot,               # 单个设备
        devices: List[DeviceSlot]         # 多个设备
    ):
        pass
```

**自动生成的注册表**（使用--complete_registry）：

```yaml
my_device:
  class:
    action_value_mappings:
      test_resource:
        type: UniLabJsonCommand
        goal:
          resource: resource
          resources: resources
          device: device
          devices: devices
        placeholder_keys:
          resource: unilabos_resources      # 自动添加！
          resources: unilabos_resources     # 自动添加！
          device: unilabos_devices          # 自动添加！
          devices: unilabos_devices         # 自动添加！
        result:
          success: success
```

### 识别规则

| Python类型 | placeholder_keys值 | 前端效果 |
|-----------|-------------------|---------|
| `ResourceSlot` | `unilabos_resources` | 单选资源下拉框 |
| `List[ResourceSlot]` | `unilabos_resources` | 多选资源下拉框 |
| `DeviceSlot` | `unilabos_devices` | 单选设备下拉框 |
| `List[DeviceSlot]` | `unilabos_devices` | 多选设备下拉框 |

### 前端UI效果

#### 单选资源

```yaml
placeholder_keys:
  source: unilabos_resources
```

**前端渲染**:
```
Source: [下拉选择框 ▼]
        ├── plate_1 (96孔板)
        ├── tiprack_1 (枪头架)
        ├── reservoir_1 (试剂槽)
        └── ...
```

#### 多选资源

```yaml
placeholder_keys:
  targets: unilabos_resources
```

**前端渲染**:
```
Targets: [多选下拉框 ▼]
         ☑ plate_1 (96孔板)
         ☐ plate_2 (384孔板)
         ☑ plate_3 (96孔板)
         └── ...
```

#### 单选设备

```yaml
placeholder_keys:
  pump: unilabos_devices
```

**前端渲染**:
```
Pump: [下拉选择框 ▼]
      ├── pump_1 (注射泵A)
      ├── pump_2 (注射泵B)
      └── ...
```

#### 多选设备

```yaml
placeholder_keys:
  sync_devices: unilabos_devices
```

**前端渲染**:
```
Sync Devices: [多选下拉框 ▼]
              ☑ heater_1 (加热器A)
              ☑ stirrer_1 (搅拌器)
              ☐ pump_1 (注射泵)
```

### 手动配置placeholder_keys

如果需要手动添加或覆盖自动生成的placeholder_keys：

#### 场景1: 非标准参数名

```yaml
action_value_mappings:
  custom_action:
    goal:
      my_custom_resource_param: resource_param
      my_device_param: device_param
    placeholder_keys:
      my_custom_resource_param: unilabos_resources
      my_device_param: unilabos_devices
```

#### 场景2: 混合类型

```python
def mixed_params(
    self,
    resource: ResourceSlot,
    normal_param: str,
    device: DeviceSlot
):
    pass
```

```yaml
placeholder_keys:
  resource: unilabos_resources    # 资源选择
  device: unilabos_devices        # 设备选择
  # normal_param不需要placeholder_keys
```

#### 场景3: 自定义选择器

```yaml
placeholder_keys:
  special_param: custom_selector   # 使用自定义选择器
```

## 系统自动生成的字段

### status_types

系统会扫描你的 Python 类，从状态方法（property或get_方法）自动生成这部分：

```yaml
status_types:
  current_temperature: float # 从 get_current_temperature() 或 @property current_temperature
  is_heating: bool           # 从 get_is_heating() 或 @property is_heating
  status: str               # 从 get_status() 或 @property status
```

**注意事项**：
- 系统会查找所有 `get_` 开头的方法和 `@property` 装饰的属性
- 类型会自动转成相应的类型（如 `str`、`float`、`bool`）
- 如果类型是 `Any`、`None` 或未知的，默认使用 `String`

### init_param_schema

完全由系统自动生成，无需手动编写：

```yaml
init_param_schema:
  config: # 从 __init__ 方法分析得出
    properties:
      port:
        type: string
        default: '/dev/ttyUSB0'
      baudrate:
        type: integer
        default: 9600
    required: []
    type: object

  data: # 根据 status_types 生成的前端类型定义
    properties:
      current_temperature:
        type: number
      is_heating:
        type: boolean
      status:
        type: string
    required:
      - status
    type: object
```

**生成规则**：
- `config` 部分：分析 `__init__` 方法的参数、类型和默认值
- `data` 部分：根据 `status_types` 生成前端显示用的类型定义

### 其他自动填充的字段

```yaml
version: '1.0.0'          # 默认版本
category: ['文件名']       # 使用 yaml 文件名作为类别
description: ''           # 默认为空
icon: ''                  # 默认为空
handles: []               # 默认空数组
config_info: []           # 默认空数组
file_path: '/path/to/file' # 系统自动填写
registry_type: 'device'   # 自动设为设备类型
```

### handles 字段

定义设备连接关系：

```yaml
handles: # 大多数情况为空，除非设备需要特定连接
  - handler_key: device_output
    label: Device Output
    data_type: resource
    data_source: value
    data_key: default_value
```

### 可选配置字段

```yaml
description: '设备的详细描述'

icon: 'device_icon.webp' # 设备图标文件名（会上传到OSS）

version: '0.0.1' # 版本号

category: # 设备分类，前端用于分组显示
  - 'heating'
  - 'cooling'
  - 'temperature_control'

config_info: # 嵌套配置，用于包含子设备
  - children:
      - opentrons_24_tuberack_nest_1point5ml_snapcap_A1
      - other_nested_component
```

## 完整示例

### Python驱动代码

```python
# unilabos/devices/my_lab/liquid_handler.py

from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from typing import List, Dict, Any, Optional

class AdvancedLiquidHandler:
    """高级液体处理工作站"""
    
    def __init__(self, config: Dict[str, Any]):
        self.simulation = config.get('simulation', False)
        self._status = "idle"
        self._temperature = 25.0
    
    @property
    def status(self) -> str:
        """设备状态"""
        return self._status
    
    @property
    def temperature(self) -> float:
        """当前温度"""
        return self._temperature
    
    def transfer(
        self,
        source: ResourceSlot,
        target: ResourceSlot,
        volume: float,
        tip: Optional[ResourceSlot] = None
    ) -> Dict[str, Any]:
        """转移液体"""
        return {"success": True}
    
    def multi_transfer(
        self,
        source: ResourceSlot,
        targets: List[ResourceSlot],
        volumes: List[float]
    ) -> Dict[str, Any]:
        """多目标转移"""
        return {"success": True}
    
    def coordinate_with_heater(
        self,
        plate: ResourceSlot,
        heater: DeviceSlot,
        temperature: float
    ) -> Dict[str, Any]:
        """与加热器协同"""
        return {"success": True}
```

### 生成的完整注册表

```yaml
# unilabos/registry/devices/advanced_liquid_handler.yaml

advanced_liquid_handler:
  class:
    module: unilabos.devices.my_lab.liquid_handler:AdvancedLiquidHandler
    type: python
    
    # 自动提取的状态类型
    status_types:
      status: str
      temperature: float
    
    # 自动生成的初始化参数
    init_param_schema:
      config:
        properties:
          simulation:
            type: boolean
            default: false
        type: object
      data:
        properties:
          status:
            type: string
          temperature:
            type: number
        required:
          - status
        type: object
    
    # 动作映射
    action_value_mappings:
      transfer:
        type: UniLabJsonCommand
        goal:
          source: source
          target: target
          volume: volume
          tip: tip
        goal_default:
          source: {}
          target: {}
          volume: 0.0
          tip: null
        placeholder_keys:
          source: unilabos_resources      # 自动添加
          target: unilabos_resources      # 自动添加
          tip: unilabos_resources         # 自动添加
        result:
          success: success
        schema:
          description: "转移液体"
          properties:
            goal:
              properties:
                source:
                  type: object
                  description: "源容器"
                target:
                  type: object
                  description: "目标容器"
                volume:
                  type: number
                  description: "体积(μL)"
                tip:
                  type: object
                  description: "枪头(可选)"
              required:
                - source
                - target
                - volume
              type: object
          required:
            - goal
          type: object
      
      multi_transfer:
        type: UniLabJsonCommand
        goal:
          source: source
          targets: targets
          volumes: volumes
        placeholder_keys:
          source: unilabos_resources      # 单选
          targets: unilabos_resources     # 多选
        result:
          success: success
      
      coordinate_with_heater:
        type: UniLabJsonCommand
        goal:
          plate: plate
          heater: heater
          temperature: temperature
        placeholder_keys:
          plate: unilabos_resources       # 资源选择
          heater: unilabos_devices        # 设备选择
        result:
          success: success
  
  description: "高级液体处理工作站，支持多目标转移和设备协同"
  version: "1.0.0"
  category:
    - liquid_handling
  handles: []
  icon: ""
```

### 另一个完整示例：温度控制器

```yaml
my_temperature_controller:
  class:
    action_value_mappings:
      heat_start:
        type: HeatChillStart
        goal:
          target_temp: temp
          vessel: vessel
        goal_default:
          target_temp: 25.0
          vessel: ''
        handles:
          output:
            - handler_key: heated_sample
              label: Heated Sample
              data_type: resource
              data_source: handle
              data_key: sample
        placeholder_keys:
          vessel: unilabos_resources
        result:
          status: status
          success: success
        schema:
          description: '启动加热功能'
          properties:
            goal:
              properties:
                target_temp:
                  type: number
                  description: '目标温度'
                vessel:
                  type: string
                  description: '容器标识'
              required:
                - target_temp
                - vessel
              title: HeatStart_Goal
              type: object
          required:
            - goal
          title: HeatStart
          type: object
        feedback: {}

      stop:
        type: UniLabJsonCommand
        goal: {}
        goal_default: {}
        handles: {}
        result:
          status: status
        schema:
          description: '停止设备'
          properties:
            goal:
              type: object
              title: Stop_Goal
          title: Stop
          type: object
        feedback: {}

    module: unilabos.devices.temperature.my_controller:MyTemperatureController
    status_types:
      current_temperature: float
      target_temperature: float
      is_heating: bool
      is_cooling: bool
      status: str
      vessel: str
    type: python

  description: '我的温度控制器设备'
  handles: []
  icon: 'temperature_controller.webp'
  init_param_schema:
    config:
      properties:
        port:
          default: '/dev/ttyUSB0'
          type: string
        baudrate:
          default: 9600
          type: number
      required: []
      type: object
    data:
      properties:
        current_temperature:
          type: number
        target_temperature:
          type: number
        is_heating:
          type: boolean
        is_cooling:
          type: boolean
        status:
          type: string
        vessel:
          type: string
      required:
        - current_temperature
        - target_temperature
        - status
      type: object

  version: '1.0.0'
  category:
    - 'temperature_control'
    - 'heating'
  config_info: []
```

## 部署和使用

### Python 驱动类要求

你的设备类需要符合以下要求：

```python
from unilabos.common.device_base import DeviceBase

class MyDevice(DeviceBase):
    def __init__(self, config):
        """初始化，参数会自动分析到 init_param_schema.config"""
        super().__init__(config)
        self.port = config.get('port', '/dev/ttyUSB0')

    # 状态方法（会自动生成到 status_types）
    @property
    def status(self):
        """返回设备状态"""
        return "idle"

    @property
    def temperature(self):
        """返回当前温度"""
        return 25.0

    # 动作方法（会自动生成 auto- 开头的动作）
    async def start_heating(self, temperature: float):
        """开始加热到指定温度"""
        pass

    def stop(self):
        """停止操作"""
        pass
```

### 方法一：使用编辑器（推荐）

1. 先编写 Python 驱动类
2. 使用注册表编辑器自动生成 yaml 配置
3. 保存生成的文件到 `devices/` 目录
4. 重启 UniLabOS 即可使用

### 方法二：手动编写（简化版）

1. 创建最小配置：

```yaml
# devices/my_device.yaml
my_device:
  class:
    module: unilabos.devices.my_module.my_device:MyDevice
    type: python
```

2. 启动系统时使用 `--complete_registry` 参数，让系统自动补全

3. 检查生成的配置是否符合预期

### 系统集成

1. 将 yaml 文件放到 `unilabos/registry/devices/` 目录
2. 系统启动时会自动扫描并加载设备
3. 系统会自动补全所有缺失的字段
4. 设备即可在前端界面中使用

### 高级配置

如果需要特殊设置，可以手动添加：

```yaml
my_device:
  class:
    module: unilabos.devices.my_module.my_device:MyDevice
    type: python
    action_value_mappings:
      # 自定义动作
      special_command:
        type: UniLabJsonCommand
        goal: {}
        result: {}

  # 可选的自定义配置
  description: '我的特殊设备'
  icon: 'my_device.webp'
  category: ['temperature', 'heating']
```

## 调试和验证

### 1. 检查生成的注册表

```bash
# 使用complete_registry生成
unilab -g dev.json --complete_registry

# 查看生成的文件
cat unilabos/registry/devices/my_device.yaml
```

### 2. 验证placeholder_keys

确认：
- ResourceSlot参数有 `unilabos_resources`
- DeviceSlot参数有 `unilabos_devices`
- List类型被正确识别

### 3. 测试前端效果

1. 启动Uni-Lab
2. 访问Web界面
3. 选择设备
4. 调用动作
5. 检查是否显示正确的选择器

### 4. 检查文件路径和导入

```bash
# 确认模块路径正确
python -c "from unilabos.devices.my_module.my_device import MyDevice"
```

## 常见问题

### Q1: placeholder_keys没有自动生成

**检查**:
1. 是否使用了`--complete_registry`参数？
2. 类型注解是否正确？
   ```python
   # ✓ 正确
   def method(self, resource: ResourceSlot):
   
   # ✗ 错误（缺少类型注解）
   def method(self, resource):
   ```
3. 是否正确导入？
   ```python
   from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
   ```

### Q2: 前端显示普通输入框而不是选择器

**原因**: placeholder_keys未正确配置

**解决**:
```yaml
# 检查YAML中是否有
placeholder_keys:
  resource: unilabos_resources
```

### Q3: 多选不工作

**检查类型注解**:
```python
# ✓ 正确 - 会生成多选
def method(self, resources: List[ResourceSlot]):

# ✗ 错误 - 会生成单选
def method(self, resources: ResourceSlot):
```

### Q4: 运行时收到错误的类型

**说明**: 运行时会自动转换

前端传递：
```json
{
  "resource": "plate_1"  // 字符串ID
}
```

运行时收到：
```python
resource.id        # "plate_1"
resource.name      # "96孔板"
resource.type      # "resource"
# 完整的Resource对象
```

### Q5: 设备加载不了

**检查**:
1. 确认 `class.module` 路径是否正确
2. 确认 Python 驱动类能否正常导入
3. 使用 yaml 验证器检查文件格式
4. 查看 UniLabOS 启动日志中的错误信息

### Q6: 自动生成失败

**检查**:
1. 确认类继承了正确的基类
2. 确保状态方法的返回类型注解清晰
3. 检查类能否被动态导入
4. 确认启用了 `complete_registry=True`

### Q7: 前端显示问题

**解决步骤**:
1. 删除旧的 yaml 文件，用编辑器重新生成
2. 清除浏览器缓存，重新加载页面
3. 确认必需字段（如 `schema`）都存在
4. 检查 `goal_default` 和 `schema` 的数据类型是否一致

### Q8: 动作执行出错

**检查**:
1. 确认动作方法名符合规范（如 `execute_<action_name>`）
2. 检查 `goal` 字段的参数映射是否正确
3. 确认方法返回值格式符合 `result` 映射
4. 在驱动类中添加异常处理

## 最佳实践

### 开发流程

1. **优先使用编辑器**：除非有特殊需求，否则优先使用注册表编辑器
2. **最小化配置**：手动配置时只定义必要字段，让系统自动生成其他内容
3. **增量开发**：先创建基本配置，后续根据需要添加特殊动作
4. **及时测试**：每次修改后及时在开发环境测试

### 代码规范

1. **始终使用类型注解**

```python
# ✓ 好
def method(self, resource: ResourceSlot, device: DeviceSlot):
    pass

# ✗ 差
def method(self, resource, device):
    pass
```

2. **提供有意义的参数名**

```python
# ✓ 好 - 清晰的参数名
def transfer(self, source: ResourceSlot, target: ResourceSlot):
    pass

# ✗ 差 - 模糊的参数名
def transfer(self, r1: ResourceSlot, r2: ResourceSlot):
    pass
```

3. **使用Optional表示可选参数**

```python
from typing import Optional

def method(
    self,
    required_resource: ResourceSlot,
    optional_resource: Optional[ResourceSlot] = None
):
    pass
```

4. **添加详细的文档字符串**

```python
def method(
    self,
    source: ResourceSlot,        # 源容器
    targets: List[ResourceSlot]  # 目标容器列表
) -> Dict[str, Any]:
    """方法说明
    
    Args:
        source: 源容器，必须包含足够的液体
        targets: 目标容器列表，每个容器应该为空
    
    Returns:
        包含操作结果的字典
    """
    pass
```

5. **方法命名规范**
   - 状态方法使用 `@property` 装饰器或 `get_` 前缀
   - 动作方法使用动词开头
   - 保持命名清晰、一致

6. **完善的错误处理**
   - 实现完善的错误处理
   - 添加日志记录
   - 提供有意义的错误信息

### 配置管理

1. **版本控制**：所有 yaml 文件纳入版本控制
2. **命名一致性**：设备 ID、文件名、类名保持一致的命名风格
3. **定期更新**：定期运行完整注册以更新自动生成的字段
4. **备份配置**：在修改前备份重要的手动配置
5. **文档同步**：保持配置文件和文档的同步更新

### 测试验证

1. **本地测试**：在本地环境充分测试后再部署
2. **渐进部署**：先部署到测试环境，验证无误后再上生产环境
3. **监控日志**：密切监控设备加载和运行日志
4. **回滚准备**：准备快速回滚机制，以应对紧急情况
5. **自动化测试**：编写单元测试和集成测试

### 性能优化

1. **按需加载**：只加载实际使用的设备类型
2. **缓存利用**：充分利用系统的注册表缓存机制
3. **资源管理**：合理管理设备连接和资源占用
4. **监控指标**：设置关键性能指标的监控和告警

## 参考资料

- {doc}`add_device` - 设备驱动编写指南
- {doc}`04_add_device_testing` - 设备测试指南
- Python [typing模块](https://docs.python.org/3/library/typing.html)
- [YAML语法](https://yaml.org/)
- [JSON Schema](https://json-schema.org/)


