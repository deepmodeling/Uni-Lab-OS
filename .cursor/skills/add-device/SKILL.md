---
name: add-device
description: Guide for adding new devices to Uni-Lab-OS (接入新设备). Uses @device decorator + AST auto-scanning instead of manual YAML. Walks through device category, communication protocol, driver creation with decorators, and graph file setup. Use when the user wants to add/integrate a new device, create a device driver, write a device class, or mentions 接入设备/添加设备/设备驱动/物模型.
---

# 添加新设备到 Uni-Lab-OS

**第一步：** 使用 Read 工具读取 `docs/ai_guides/add_device.md`，获取完整的设备接入指南。

该指南包含设备类别（物模型）列表、通信协议模板、常见错误检查清单等。搜索 `unilabos/devices/` 获取已有设备的实现参考。

---

## 装饰器参考

### @device — 设备类装饰器

```python
from unilabos.registry.decorators import device

# 单设备
@device(
    id="my_device.vendor",           # 注册表唯一标识（必填）
    category=["temperature"],         # 分类标签列表（必填）
    description="设备描述",            # 设备描述
    display_name="显示名称",           # UI 显示名称（默认用 id）
    icon="DeviceIcon.webp",           # 图标文件名
    version="1.0.0",                  # 版本号
    device_type="python",             # "python" 或 "ros2"
    handles=[...],                    # 端口列表（InputHandle / OutputHandle）
    model={...},                      # 3D 模型配置
    hardware_interface=HardwareInterface(...),  # 硬件通信接口
)

# 多设备（同一个类注册多个设备 ID，各自有不同的 handles 等配置）
@device(
    ids=["pump.vendor.model_A", "pump.vendor.model_B"],
    id_meta={
        "pump.vendor.model_A": {"handles": [...], "description": "型号 A"},
        "pump.vendor.model_B": {"handles": [...], "description": "型号 B"},
    },
    category=["pump_and_valve"],
)
```

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
    display_name="设备显示名",
)
class MyDevice:
    """设备类说明。"""

    _ros_node: BaseROS2DeviceNode

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        """
        初始化设备。

        Args:
            device_id[设备ID]: 设备实例 ID，默认使用 my_device。
            config[设备配置]: 设备启动配置。
        """
        self.device_id = device_id or "my_device"
        self.config = config or {}
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
- `__init__` 签名固定为 `(self, device_id=None, config=None, **kwargs)`
- `post_init` 用 `@not_action` 标记，参数类型标注为 `BaseROS2DeviceNode`
- 运行时状态存储在 `self.data` 字典中
- 设备文件放在 `unilabos/devices/<category>/` 目录下
