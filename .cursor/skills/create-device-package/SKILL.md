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
- `goal_default`：编辑器里的初始参数值，叠加在签名默认值上；它不让参数变成可选，没有签名默认值的参数仍然必填。
- `material_contract`（实验性，写了就按定义告警一次）：资源语义（搬运、两步搬运、分装；以及装载托管、原位处理、库位选择器提示、顺序敏感），一个字段，值是 `MaterialRole` 的一个方法或列表，见下文「资源语义」。
- `placeholder_keys`：前端选择器 `{参数名: PLACEHOLDER_...}`。物料 `ResourceSlot`、设备 `DeviceSlot`、库位 `SiteSlot` 由类型自动推出；
  节点、人工确认指派人、扣减等没有专门类型的选择器，或想把某个参数换一种显示，就在这里手写。两者取并集，同一参数以手写为准；
  只改前端显示，参数类型与校验不变（兼容规则：手写成 `unilabos_resources` 的参数也加物料锁）。键必须是参数名。
- `handles`：动作级输入/输出端口，使用 `ActionInputHandle`、`ActionOutputHandle`；不写时按 schema 生成。
- 已删除、写了会报错的写法：`contract=`、`resource_contract=`、改名前的 `material=`（改成 `material_contract=`，值不变）、改名前的声明类 `Material`（改成 `MaterialRole`，方法不变；`Material` 仍可导入，但 `Material.xxx(...)` 直接报错）、JSON 动作的 `goal=` / `result=` 重命名（参数名就是字段名），
  注解里的 `Placeholder(...)`（改成 `placeholder_keys=`）与 `SiteSelector(...)`（改成 `SiteSlot` + `MaterialRole.site_picker(...)`），
  以及 `Material.transfer / aliquot / site / uses`、带 `owner` / `site` / `carriers` 的 `Material.pick / place`（新写法见下文「资源语义」）。

参数与返回值只有一个标准，注册表按签名生成 schema，调度端与 Host 调用前校验参数，调用后按返回注解校验返回值：

| 写法 | 生成与校验 |
| --- | --- |
| `str` / `int` / `float` / `bool` / `Literal` / `list[X]` / `Optional[X]` / `Annotated[X, Field(...)]` | 严格类型与范围 |
| `x: X = None` | 同 `Optional[X]` |
| `ResourceSlot` / `list[ResourceSlot]` | 物料引用，自动加物料锁，Host 解析成物料实例 |
| `dict` / `Dict[str, Any]` | 任意 JSON 对象 |
| 不写类型、`Any` | 任意 JSON 值 |
| 其他（`Union`、枚举、PLR 类型……） | 退回签名推断，不会注册失败 |
| 没有默认值 | 必填，缺了直接报错（不会补 0 / 空串） |
| `**kwargs` | 允许多传字段；`*args` 忽略并告警 |
| 返回注解 | 没写不校验；`-> None` 只接受空返回；`-> dict` 只要求是对象；同模块 `TypedDict` 字段齐全、每个字段一个输出端口；`-> bool` 等按类型 |

所以返回注解要写真实的返回类型：注解 `-> bool` 却返回字典、注解 `-> None` 却返回值，都会让动作在执行后按失败处理。

旧的 `@always_free` 仍可被 AST 扫描识别，但新设备包优先使用 `@action(always_free=True)`，把动作元数据集中在一个装饰器里。

#### 资源语义：`material_contract=`（实验性）

```python
from unilabos.registry.decorators import MaterialRole, action
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot, SiteSlot

@action(display_name="搬运板", material_contract=MaterialRole.move("plate", to_site="target_site"))
def move_plate(self, plate: ResourceSlot, target_site: SiteSlot) -> dict: ...

@action(display_name="抓取", material_contract=MaterialRole.pick("plate"))
def pick_plate(self, plate: ResourceSlot) -> dict: ...

@action(display_name="放下", material_contract=MaterialRole.place("plate", to_site="target_site"))
def place_plate(self, plate: ResourceSlot, target_site: SiteSlot) -> dict: ...

@action(display_name="原位搅拌", material_contract=[MaterialRole.in_place("sample"), MaterialRole.order_sensitive()])
def stir(self, sample: ResourceSlot, rpm: int = 300) -> None: ...
```

物料动作：

| 写法 | 含义 |
| --- | --- |
| `MaterialRole.move("plate", to_site="site", from_site=None, carrier_device=None)` | 一个动作把容器搬到 `to_site`；所属方由库位决定，搬运途中在搬运设备的 `gripper` 夹爪库位上 |
| `MaterialRole.pick("plate", from_site=None, carrier_device=None)` | 两步搬运第一步：抓到搬运设备的夹爪库位，任务托管搬运设备 |
| `MaterialRole.place("plate", to_site="site", carrier_device=None)` | 两步搬运第二步：从夹爪库位放到 `to_site`，结束托管 |
| `MaterialRole.dispense(from_material="source", to_materials=["target", ...])` | 按实测量在容器之间转移内容物 |

不代表物料动作的声明：

| 写法 | 含义 |
| --- | --- |
| `MaterialRole.load("sample", device=None, from_device=None)` / `MaterialRole.unload("sample", device=None)` | 装进 / 取出设备，期间任务托管设备；`device` 省略即执行设备，`from_device` 表示设备间转移 |
| `MaterialRole.in_place("sample")` | 物料必须仍在执行设备内 |
| `MaterialRole.site_picker("site", of=None, for_material=None, occupied="show")` | 库位选择器提示：`site` 列出 `of`（省略即执行设备根节点）的库位，放进去的是 `for_material`；`occupied` 取 `"hide"` / `"show"` / `"allow"` |
| `MaterialRole.order_sensitive()` | 同一资源上的兄弟动作必须显式排序 |

字符串都是参数名，注册时对照签名检查：物料（`material`、`from_material`、`to_materials`、`for_material`）必须是单个 `ResourceSlot`；
库位（`to_site`、`from_site`、`site_picker` 的库位参数）必须是 `SiteSlot`（值是库位 uuid）；设备（`carrier_device`、`device`、
`from_device`）必须是 `DeviceSlot`（值是设备 id），省略就是执行设备；`site_picker` 的 `of` 是 `ResourceSlot` 或 `DeviceSlot`。
搬运设备的夹爪库位 `meta_data.role` 统一为 `gripper`（有且只有一个）。参数按类型加锁：`ResourceSlot` 物料锁、`DeviceSlot` 设备锁、
`SiteSlot` 库位锁。

用 `MaterialRole.move / pick / place` 声明了的搬运由核心按回执结算物料位置，驱动不要再对同一物料调用 `materials.transfer` /
`materials.assign`；`materials.*` 只用于没有声明的运行时变化（新建、写内容物、删除、启动对齐等）。

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
- 包内模块之间可以用相对导入（`from .labware import Deck`、`from ..common import helper`）：扫描器按包结构（`__init__.py`）把它们解析成绝对模块路径，动作参数注解同样按它解析；越过顶层包的相对导入会让该文件扫描失败。
- `@device` 类会生成设备条目；`id/category/description/handles/model/hardware_interface` 来自装饰器。
- `__init__` 参数、类型注解、默认值和 docstring 会进入 `init_param_schema.config`。
- 带 `@action` 的方法会进入 `action_value_mappings`；未加 `@action` 的 public 方法会生成 `auto-<method_name>` 动作。
- 辅助方法用私有名 `_helper` 或 `@not_action`，避免被自动暴露为动作。
- `@action(action_type=...)` 会优先从 ROS Action 类型补全 `goal/feedback/result/schema/goal_default`，再用装饰器里的 `goal/feedback/result/goal_default` 覆盖。
- `@action()` / auto action 按 3.1 的规则从方法签名生成 `goal/schema/goal_default/handles`，从返回值注解生成 result schema。
- 参数与返回值尽量写完整类型注解：写了类型才会严格校验，不写就接受任意值。
- `__init__` 的现场配置参数不要设计成单个 `config: dict` 或其他大对象参数；按基础类型拆成 `port: str`、`baudrate: int`、`timeout: float`、`enabled: bool` 这类可直接生成表单字段的参数。旧驱动保留的 `config` 参数在 HostLink 上收到整份节点配置，同时声明的其他具名参数仍按名取值。

### 4. 生成 pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "<package_name>"
version = "0.1.0"
description = "<description>"
requires-python = ">=3.14,<3.15"
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
      - uses: conda-incubator/setup-miniconda@v4
        with:
          miniforge-version: latest
          use-mamba: true
          python-version: '3.14.10'
          channels: conda-forge,robostack-jazzy,uni-lab
          channel-priority: strict
          activate-environment: unilab
          auto-update-conda: false
      - name: Install unilabos
        shell: bash -el {0}
        run: mamba install -n unilab --override-channels -c uni-lab -c conda-forge -c robostack-jazzy uni-lab::unilabos -y
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

check_mode 的退出码即结论：AST 扫描失败而被跳过的文件、读取 / 解析失败的注册表 YAML、导入失败的注册类（`@device` 与 YAML 条目都算）都会让它以退出码 1 结束并逐条列出；
`supported_backends` 不含当前 backend 的设备（HostLink 环境里的 ROS 2 专用节点）导入失败只记为跳过。

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
- `--devices` 可以指向包里的子目录（如一个设备家族 `./<package_name>/devices/<family>`）：模块路径按顶层包推导（沿 `__init__.py` 上溯），包级 `registry/` 只取类路径在该子目录内的条目，便于单独校验一个家族
- 模拟器 / 离线模式的驱动设 `self.run_in_test_mode = True`，`unilab --test_mode` 下才会真实执行；没设的驱动在测试模式下只返回模拟结果。这条规则同样作用于驱动之间的 `node.call_device_action[_async]`：测试模式下真跑的工作站调用没声明 `run_in_test_mode` 的子设备时拿到的是模拟结果，不会驱动真实硬件
- `--external_devices_only` 跳过内置设备扫描，仅加载外部设备包；核心 `container` 资源模板仍会加载，其它内置设备（如 `virtual_*`）不加载
