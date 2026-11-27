---
name: create-device-package
description: Create an external Uni-Lab-OS device package from LabDeviceTemplate. Use when the user wants to create a standalone device package, fork LabDeviceTemplate, build an external device driver, or mentions 创建设备包/外部设备/设备模板/device package.
---

# 创建外部设备包（基于 LabDeviceTemplate）

本 Skill 用于引导用户基于 [Xuwznln/LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate) 创建独立的设备驱动包。

## 流程

### 1. 收集信息

使用 AskQuestion 工具向用户确认：

- **设备名称**：如 `my_analytical_balance`
- **设备类别**：如 `["balance"]`、`["temperature"]`、`["custom"]`
- **设备描述**：一句话描述
- **需要的 Python 依赖**：如 `pyserial`、`pymodbus`
- **要暴露的动作**：动作名、参数、返回值、是否查询类动作、是否需要进度反馈
- **要广播/展示的状态**：状态名、类型、发布周期
- **必要的背景信息**：如 pdf文件、LIMS接口定义、HTTP Api、网址资源等

### 2. 创建目录结构

在工作区根目录创建 `device_package_<name>/` 目录：

```
device_package_<name>/
├── README.md
├── requirements.txt
├── pyproject.toml
├── .gitignore
├── .github/
│   └── workflows/
│       └── check_registry.yml
└── <package_name>/
    ├── __init__.py
    └── <device_name>.py
```

### 3. 编写设备代码

使用 `@device` 装饰器模板。装饰器必须直接从 `unilabos.registry.decorators` 导入，并使用原名 `device`、`action`、`topic_config`，避免 AST 扫描器无法识别别名：

```python
from unilabos.registry.decorators import action, device, topic_config

@device(
    id="<device_id>",
    category=["<category>"],
    description="<description>",
)
class <ClassName>:

    def __init__(
        self,
        device_id=None,
        port: str = "COM1",
        baudrate: int = 9600,
        timeout: float = 1.0,
        **kwargs,
    ):
        """初始化设备。

        Args:
            device_id[设备ID]: 设备实例 ID。
            port[串口]: 设备串口号，例如 COM1 或 /dev/ttyUSB0。
            baudrate[波特率]: 串口波特率。
            timeout[超时时间(s)]: 通信超时时间，单位秒。
        """
        self.device_id = device_id or "<device_id>"
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.data = {"status": "idle"}

    def post_init(self, ros_node):
        self._ros_node = ros_node

    @action(
        description="初始化设备",
        always_free=False,
        feedback_interval=1.0,
    )
    async def initialize(self) -> bool:
        """初始化设备。"""
        self.data["status"] = "ready"
        return True

    @action(
        description="<action_description>",
        always_free=False,
        feedback_interval=1.0,
    )
    def <action_name>(self, <params>) -> dict:
        """<action_description>

        Args:
            <param_name>[<参数中文名>]: <参数描述，说明单位/范围/资源要求>
        """
        # 实现动作逻辑
        return {"success": True}

    @property
    @topic_config(period=5.0, print_publish=False, qos=10, name=None)
    def status(self) -> str:
        """设备状态。"""
        return self.data.get("status", "idle")
```

### 3.1 `@action` 规则

`@action()` 不传 `action_type` 时，registry 会根据方法签名生成 `UniLabJsonCommand` 或 `UniLabJsonCommandAsync`：

- `description`：动作描述；JSON Command 动作会写入 schema 的顶层描述。若为空，会使用 docstring 第一行作为描述。
- `always_free=True`：动作不占用设备 busy 队列，适合 `get_status`、`query_*`、`ping`、连接测试等轻量查询动作；会在 `action_value_mappings.<action>.always_free` 中写入 `true`。
- `feedback_interval`：动作反馈发布间隔，单位秒；AST 扫描默认补 `1.0`，长任务可调大，例如 `5.0`、`30.0`。
- `goal_default`：覆盖参数默认值；普通 Python 默认参数也会进入 `goal_default`。
- `placeholder_keys`：资源/设备选择器配置；`ResourceSlot`、`DeviceSlot` 通常会被自动检测，特殊字段再手动补。
- `handles`：动作级输入/输出端口，使用 `ActionInputHandle`、`ActionOutputHandle`。

旧的 `@always_free` 仍可被 AST 扫描识别，但新设备包优先使用 `@action(always_free=True)`，把动作元数据集中在一个装饰器里。

### 3.2 `@topic_config` 规则

状态属性推荐使用 `@property` + `@topic_config()`。`@topic_config` 必须写在离 `def` 更近的一行，也就是位于 `@property` 下方：

```python
@property
@topic_config(period=2.0, print_publish=False, qos=10, name="temperature")
def temperature(self) -> float:
    """当前温度。"""
    return self.data["temperature"]
```

参数说明：

- `period`：发布周期，单位秒；`None` 时使用节点默认值，装饰器文档默认 5.0。
- `print_publish`：是否打印发布日志；`None` 时使用节点默认配置。
- `qos`：ROS topic QoS 深度；`None` 时使用默认 10。
- `name`：自定义发布名称；`None` 时使用属性名，普通 `get_temperature()` 方法会去掉 `get_` 前缀生成 `temperature`。

AST/runtime registry 会把 `@property` 或带 `@topic_config` 的方法收集到 `status_types` 和 `init_param_schema.data`。需要稳定广播给外部系统的状态必须显式加 `@topic_config`。

### 3.3 参数中文名和描述格式

registry 通过 docstring 解析参数文档，并写入 JSON Schema：

- docstring 第一行：动作描述 fallback。
- `Args:` / `Parameters:` / `Params:` 小节：参数说明。
- `param[中文名]: 描述`：`中文名` 写入 schema `title`，描述写入 schema `description`。
- `param: 描述`：只写 `description`，`title` 默认使用参数名。
- 支持续行描述；缩进的后续行会合并到同一个参数描述。

推荐格式：

```python
@action(description="转移液体", feedback_interval=1.0)
def transfer(self, source: str, target: str, volume_ul: float) -> dict:
    """转移液体。

    Args:
        source[源孔位]: 源容器或孔位 ID，例如 plate_1/A1。
        target[目标孔位]: 目标容器或孔位 ID，例如 plate_1/B1。
        volume_ul[体积(ul)]: 转移体积，单位 ul，必须大于 0。
    """
    return {"success": True}
```

不要写成 `source [源孔位]: ...`，当前解析器要求参数名和 `[]` 紧挨。

### 3.4 Registry AST 扫描要点

外部设备包验证走 `unilab --check_mode --devices ./<package_name> --external_devices_only`，registry 会静态扫描 Python AST 构建设备 schema：

- 只识别来自 `unilabos.registry.decorators` 的 `@device`、`@action`、`@topic_config`、`@not_action`、`@always_free`。
- `@device` 类会生成设备条目；`id/category/description/handles/model/hardware_interface` 来自装饰器。
- `__init__` 参数、类型注解、默认值和 docstring 会进入 `init_param_schema.config`。
- 带 `@action` 的方法会进入 `action_value_mappings`；未加 `@action` 的 public 方法会生成 `auto-<method_name>` 动作。
- 辅助方法用私有名 `_helper` 或 `@not_action`，避免被自动暴露为动作。
- `@action(action_type=...)` 会优先从 ROS Action 类型补全 `goal/feedback/result/schema/goal_default`，再用装饰器里的 `goal/feedback/result/goal_default` 覆盖。
- `@action()` / auto action 会从方法参数生成 `goal/schema/goal_default`，从返回值注解补 result schema。
- 参数类型注解必须完整；`str/int/float/bool/list/dict`、`Optional`、`Union`、`List`、`Dict`、`Literal`、`TypedDict`、`ResourceSlot`、`DeviceSlot` 都有不同程度的自动 schema 支持。
- `__init__` 的现场配置参数不要设计成单个 `config: dict` 或其他大对象参数；按基础类型拆成 `port: str`、`baudrate: int`、`timeout: float`、`enabled: bool` 这类可直接生成表单字段的参数。

### 4. 生成 pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "<package_name>"
version = "0.1.0"
description = "<description>"
requires-python = ">=3.10"
dependencies = [
    # 用户指定的依赖
]

[tool.setuptools.packages.find]
include = ["<package_name>*"]
```

### 5. 生成 GitHub Actions CI

```yaml
name: Check Device Registry
on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: conda-incubator/setup-miniconda@v3
        with:
          miniforge-version: latest
          use-mamba: true
          python-version: '3.11.14'
          channels: conda-forge,robostack-staging,uni-lab
          channel-priority: flexible
          activate-environment: unilab
          auto-update-conda: false
      - name: Install unilabos
        shell: bash -el {0}
        run: mamba install -n unilab --override-channels -c uni-lab -c robostack-staging -c conda-forge uni-lab::unilabos -y
      - name: Validate
        shell: bash -el {0}
        run: unilab --check_mode --devices ./<package_name> --external_devices_only
```

### 6. 验证

提示用户运行：

```bash
# 确保已在 unilab conda 环境中
pip install -e .
unilab --check_mode --devices ./<package_name> --external_devices_only
```

## 关键规则

- 包目录名以 `device_package_` 开头（已在主仓库 `.gitignore` 中排除）
- 设备类 `__init__` 保留 `device_id` 和 `**kwargs`，需要现场配置的参数按基础类型显式展开；避免单个参数使用 `dict` 承载全部配置
- 装饰器必须从 `unilabos.registry.decorators` 导入并使用原名，便于 AST 扫描
- 状态属性使用 `@property` + `@topic_config()` 组合；需要自定义发布周期时填写 `period`
- 查询类动作使用 `@action(always_free=True)`；长任务根据需要设置 `feedback_interval`
- 参数文档使用 `param[中文名]: 描述` 格式，写入 schema `title/description`
- 所有动作参数和返回值尽量补类型注解，避免 schema 退化为 string/object
- 不应暴露的 public 辅助方法改成私有方法或加 `@not_action`
- 运行时数据存储在 `self.data` 字典中
- `post_init(self, ros_node)` 用于需要 ROS node 的初始化逻辑
- `--external_devices_only` 跳过内置设备扫描，仅加载外部设备包
