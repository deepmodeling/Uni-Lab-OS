# yaml 注册表编写指南

> **📦 设备驱动放在外部驱动包中。** 主仓库只保留框架依赖的内置设备代码（工作站基类、虚拟设备、抽象液体处理器）；厂商驱动和新设备都放在独立驱动包里，详见 {doc}`add_device` 开头的说明。下文 `module:` 示例中的 `my_device_pkg.xxx` 指你的驱动包模块路径。

## 快速开始：使用注册表编辑器

推荐使用 Uni-Lab-OS 自带的可视化编辑器，它能帮你自动生成大部分配置，省去手写的麻烦。

### 怎么用编辑器

1. 启动 Uni-Lab-OS
2. 在浏览器中打开"注册表编辑器"页面
3. 选择你的 Python 设备驱动文件
4. 点击"分析文件"，让系统读取你的类信息
5. 填写一些基本信息（设备描述、图标啥的）
6. 点击"生成注册表"，复制生成的内容
7. 把内容保存到 `devices/` 目录下

我们为你准备了一个测试驱动，用于在界面上尝试注册表生成，参见目录：test\registry\example_devices.py

---

## 手动编写指南

如果你想自己写 yaml 文件，或者想深入了解结构，查阅下方说明。

## 注册表的基本结构

yaml 注册表就是设备的配置文件，里面定义了设备怎么用、有什么功能。好消息是系统会自动帮你填大部分内容，你只需要写两个必需的东西：设备名和 class 信息。

### 各字段用途

| 字段名            | 类型   | 需要手写 | 说明                                |
| ----------------- | ------ | -------- | ----------------------------------- |
| 设备标识符        | string | 是       | 设备的唯一名字，比如 `mock_chiller` |
| class             | object | 部分     | 设备的核心信息，必须写              |
| description       | string | 否       | 设备描述，系统默认给空字符串        |
| handles           | array  | 否       | 连接关系，默认是空的                |
| icon              | string | 否       | 图标路径，默认为空                  |
| init_param_schema | object | 否       | 初始化参数，系统自动分析生成        |
| version           | string | 否       | 版本号，默认 "1.0.0"                |
| category          | array  | 否       | 设备分类；没写时用文件名，写了就以你写的为准 |
| config_info       | array  | 否       | 嵌套配置，默认为空                  |
| file_path         | string | 否       | 文件路径，系统自动设置              |
| registry_type     | string | 否       | 注册表类型，自动设为 "device"       |

### class 字段里有啥

class 是核心部分，包含这些内容：

| 字段名                | 类型   | 需要手写 | 说明                               |
| --------------------- | ------ | -------- | ---------------------------------- |
| module                | string | 是       | Python 类的路径，必须写            |
| type                  | string | 是       | 驱动类型，一般写 "python"          |
| status_types          | object | 否       | 状态类型，系统自动分析生成         |
| action_value_mappings | object | 部分     | 动作配置，系统会自动生成一些基础的 |

## 怎么创建新的注册表

### 创建文件

在 devices 文件夹里新建一个 yaml 文件，比如 `new_device.yaml`。

### 完整结构是什么样的

```yaml
new_device: # 设备名，要唯一
  class: # 核心配置
    action_value_mappings: # 动作配置（后面会详细说）
      action_name:
        # 具体的动作设置
    module: my_device_pkg.new_device:NewDeviceClass # 你的 Python 类
    status_types: # 状态类型（系统会自动生成）
      status: str
      temperature: float
      # 其他状态
    type: python # 驱动类型，一般就是 python

  description: New Device Description # 设备描述
  handles: [] # 连接关系，通常是空的
  icon: '' # 图标路径
  init_param_schema: # 初始化参数（系统会自动生成）
    config: # 初始化时需要的参数
      properties:
        port:
          default: DEFAULT_PORT
          type: string
      required: []
      type: object
    data: # 前端显示用的数据类型
      properties:
        status:
          type: string
        temperature:
          type: number
      required:
        - status
      type: object

  version: 0.0.1 # 版本号
  category:
    - device_category # 设备类别
  config_info: [] # 嵌套配置，通常为空
```

## action_value_mappings 怎么写

这个部分定义设备能做哪些动作。好消息是系统会自动生成大部分动作，你通常只需要添加一些特殊的自定义动作。

### 系统自动生成哪些动作

系统会帮你生成这些：

1. 以 `auto-` 开头的动作：从你 Python 类的方法自动生成
2. 通用的驱动动作：
   - `_execute_driver_command`：同步执行驱动命令（仅本地可用）
   - `_execute_driver_command_async`：异步执行驱动命令（仅本地可用）

### 如果要手动定义动作

如果你需要自定义一些特殊动作，需要这些字段：

| 字段名           | 需要手写 | 说明                             |
| ---------------- | -------- | -------------------------------- |
| type             | 是       | 动作类型，必须指定               |
| goal             | 是       | 输入参数怎么映射                 |
| feedback         | 否       | 实时反馈，通常为空               |
| result           | 是       | 结果怎么返回                     |
| goal_default     | 部分     | 参数默认值，ROS 动作会自动生成   |
| schema           | 部分     | 前端表单配置，ROS 动作会自动生成 |
| handles          | 否       | 连接关系，默认为空               |
| placeholder_keys | 否       | 特殊输入字段配置                 |

### 动作类型有哪些

| 类型                   | 什么时候用           | 系统会自动生成什么     |
| ---------------------- | -------------------- | ---------------------- |
| UniLabJsonCommand      | 自定义同步 JSON 命令 | 啥都不生成             |
| UniLabJsonCommandAsync | 自定义异步 JSON 命令 | 啥都不生成             |
| ROS 动作类型           | 标准 ROS 动作        | goal_default 和 schema |

常用的 ROS 动作类型：

- `SendCmd`：发送简单命令
- `NavigateThroughPoses`：导航动作
- `SingleJointPosition`：单关节位置控制
- `Stir`：搅拌动作
- `HeatChill`、`HeatChillStart`：加热冷却动作

### 复杂一点的例子

```yaml
heat_chill_start:
  type: HeatChillStart
  goal:
    purpose: purpose
    temp: temp
  goal_default: # ROS动作会自动生成，你也可以手动覆盖
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
  # schema 系统会自动生成，不用写
```

### 动作名字怎么起

根据设备用途来起名字：

- 启动停止类：`start`、`stop`、`pause`、`resume`
- 设置参数类：`set_speed`、`set_temperature`、`set_timer`
- 移动控制类：`move_to_position`、`move_through_points`
- 功能操作类：`stir`、`heat_chill_start`、`heat_chill_stop`
- 开关控制类：`valve_open_cmd`、`valve_close_cmd`、`push_to`
- 命令执行类：`send_nav_task`、`execute_command_from_outer`

### 常用的动作类型

- `UniLabJsonCommand`：自定义 JSON 命令（不走 ROS）
- `UniLabJsonCommandAsync`：异步 JSON 命令（不走 ROS）
- `SendCmd`：发送简单命令
- `NavigateThroughPoses`：导航相关
- `SingleJointPosition`：单关节控制
- `Stir`：搅拌
- `HeatChill`、`HeatChillStart`：加热冷却
- 其他的 ROS 动作类型：看具体的 ROS 服务

### 示例：完整的动作配置

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

## 系统自动生成的字段

### status_types

系统会扫描你的 Python 类，从状态方法自动生成这部分：

```yaml
status_types:
  current_temperature: float # 从 get_current_temperature() 方法来的
  is_heating: bool # 从 get_is_heating() 方法来的
  status: str # 从 get_status() 方法来的
```

注意几点：

- 系统会找所有 `get_` 开头的方法
- 类型会自动转成 ROS 类型（比如 `str` 变成 `String`）
- 如果类型是 `Any`、`None` 或者不知道的，就默认用 `String`

### init_param_schema

这个完全是系统自动生成的，你不用管：

```yaml
init_param_schema:
  config: # 从你类的 __init__ 方法分析出来的
    properties:
      port:
        type: string
        default: '/dev/ttyUSB0'
      baudrate:
        type: integer
        default: 9600
    required: []
    type: object

  data: # 根据 status_types 生成的前端用的类型
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

生成规则很简单：

- `config` 部分：看你类的 `__init__` 方法有什么参数，类型和默认值是啥
- `data` 部分：根据 `status_types` 生成前端显示用的类型定义

### 其他自动填充的字段

```yaml
version: '1.0.0' # 默认版本
category: ['文件名'] # 没写 category 时用 yaml 文件名当类别
description: '' # 默认为空，你可以手动改
icon: '' # 默认为空，你可以加图标
handles: [] # 默认空数组
config_info: [] # 默认空数组
file_path: '/path/to/file' # 系统自动填文件路径
registry_type: 'device' # 自动设为设备类型
```

### handles 字段

这个是定义设备连接关系的，类似动作里的 handles 一样：

```yaml
handles: # 大多数时候都是空的，除非设备本身需要连接啥
  - handler_key: device_output
    label: Device Output
    data_type: resource
    data_source: value
    data_key: default_value
```

### 其他可以配置的字段

```yaml
description: '设备的详细描述' # 写清楚设备是干啥的

icon: 'device_icon.webp' # 设备图标，文件名（会上传到OSS）

version: '0.0.1' # 版本号

category: # 设备分类，前端会用这个分组
  - 'heating'
  - 'cooling'
  - 'temperature_control'

config_info: # 嵌套配置，如果设备包含子设备
  - children:
      - opentrons_24_tuberack_nest_1point5ml_snapcap_A1
      - other_nested_component
```

## 完整的例子

这里是一个比较完整的设备配置示例：

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

    module: my_device_pkg.my_controller:MyTemperatureController
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

## 怎么部署和使用

### 方法一：用编辑器（推荐）

1. 先写好你的 Python 驱动类
2. 用注册表编辑器自动生成 yaml 配置
3. 把生成的文件保存到 `devices/` 目录
4. 重启 Uni-Lab-OS 就能用了

### 方法二：手动写（简化版）

1. 创建最简配置：

```yaml
# devices/my_device.yaml
my_device:
  class:
    module: my_device_pkg.my_device:MyDevice
    type: python
```

2. 启动系统时用 `complete_registry=True` 参数，让系统自动补全

3. 检查一下生成的配置是不是你想要的

### Python 驱动类要怎么写

你的设备类要符合这些要求：

```python
from unilabos.common.device_base import DeviceBase

class MyDevice(DeviceBase):
    def __init__(self, config):
        """初始化，参数会自动分析到 init_param_schema.config"""
        super().__init__(config)
        self.port = config.get('port', '/dev/ttyUSB0')

    # 状态方法（会自动生成到 status_types）
    def get_status(self):
        """返回设备状态"""
        return "idle"

    def get_temperature(self):
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

### 系统集成

1. 把 yaml 文件放到 `devices/` 目录下
2. 系统启动时会自动扫描并加载设备
3. 系统会自动补全所有缺失的字段
4. 设备马上就能在前端界面中使用

### 高级配置

如果需要特殊设置，可以手动加：

```yaml
my_device:
  class:
    module: my_device_pkg.my_device:MyDevice
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

## 常见问题怎么排查

### 设备加载不了

1. 检查模块路径：确认 `class.module` 路径写对了
2. 确认类能导入：看看你的 Python 驱动类能不能正常导入
3. 检查语法：用 yaml 验证器看看文件格式对不对
4. 查看日志：看 Uni-Lab-OS 启动时有没有报错信息

### 自动生成失败了

1. 类分析出问题：确认你的类继承了正确的基类
2. 方法类型不明确：确保状态方法的返回类型写清楚了
3. 导入有问题：检查类能不能被动态导入
4. 没开完整注册：确认启用了 `complete_registry=True`

### 前端显示有问题

1. 重新生成：删掉旧的 yaml 文件，用编辑器重新生成
2. 清除缓存：清除浏览器缓存，重新加载页面
3. 检查字段：确认必需的字段（比如 `schema`）都有
4. 验证数据：检查 `goal_default` 和 `schema` 的数据类型是不是一致

### 动作执行出错

1. 方法名不对：确认动作方法名符合规范（比如 `execute_<action_name>`）
2. 参数映射错误：检查 `goal` 字段的参数映射是否正确
3. 返回格式不对：确认方法返回值格式符合 `result` 映射
4. 没异常处理：在驱动类里加上异常处理

## 最佳实践

### 开发流程

1. **优先使用编辑器**：除非有特殊需求，否则优先使用注册表编辑器
2. **最小化配置**：手动配置时只定义必要字段，让系统自动生成其他内容
3. **增量开发**：先创建基本配置，后续根据需要添加特殊动作

### 代码规范

1. **方法命名**：状态方法使用 `get_` 前缀，动作方法使用动词开头
2. **类型注解**：为方法参数和返回值添加类型注解
3. **文档字符串**：为类和方法添加详细的文档字符串
4. **异常处理**：实现完善的错误处理和日志记录

### 配置管理

1. **版本控制**：所有 yaml 文件纳入版本控制
2. **命名一致性**：设备 ID、文件名、类名保持一致的命名风格
3. **定期更新**：定期运行完整注册以更新自动生成的字段
4. **备份配置**：在修改前备份重要的手动配置

### 测试验证

1. **本地测试**：在本地环境充分测试后再部署
2. **渐进部署**：先部署到测试环境，验证无误后再上生产环境
3. **监控日志**：密切监控设备加载和运行日志
4. **回滚准备**：准备快速回滚机制，以应对紧急情况

### 性能优化

1. **按需加载**：只加载实际使用的设备类型
2. **缓存利用**：充分利用系统的注册表缓存机制
3. **资源管理**：合理管理设备连接和资源占用
4. **监控指标**：设置关键性能指标的监控和告警
