---
name: add-protocol
description: Guide for adding new experiment protocols to Uni-Lab-OS (添加新实验操作协议). Walks through ROS Action definition, Pydantic model creation, protocol generator implementation, and registration. Use when the user wants to add a new protocol, create a compile function, implement an experiment operation, or mentions 协议/protocol/编译/compile/实验操作.
---

# 添加新实验操作协议（Protocol）

Protocol 是对实验有意义的完整动作（如泵转移、过滤、溶解），需要多设备协同。`compile/` 中的生成函数根据设备连接图将抽象操作"编译"为设备指令序列。

添加一个 Protocol 需修改 **6 个文件**，按以下流程执行。

> **Protocol 不使用 `@device`/`@resource` 装饰器，仍需手动注册。**

---

## 第一步：确认协议信息

向用户确认：

| 信息 | 示例 |
|------|------|
| 协议英文名 | `MyNewProtocol` |
| 操作描述 | 将固体样品研磨至目标粒径 |
| Goal 参数（必需 + 可选） | `vessel: dict`, `time: float = 300.0` |
| Result 字段 | `success: bool`, `message: str` |
| 需要哪些设备协同 | 研磨器、搅拌器 |

---

## 第二步：创建 ROS Action 定义

路径：`unilabos_msgs/action/<ActionName>.action`

三段式结构（Goal / Result / Feedback），用 `---` 分隔：

```
# Goal
Resource vessel
float64 time
string mode
---
# Result
bool success
string return_info
---
# Feedback
string status
string current_device
builtin_interfaces/Duration time_spent
builtin_interfaces/Duration time_remaining
```

---

## 第三步：注册 Action 到 CMakeLists

在 `unilabos_msgs/CMakeLists.txt` 的 `set(action_files ...)` 块中添加：

```cmake
"action/MyNewAction.action"
```

---

## 第四步：创建 Pydantic 模型

在 `unilabos/experiments/models.py` 中的 `# Start Protocols` 和 `# End Protocols` 之间添加：

```python
class MyNewProtocol(BaseModel):
    vessel: dict = Field(..., description="目标容器")
    time: float = Field(300.0, description="操作时间 (秒)")
    mode: str = Field("default", description="操作模式")

    def model_post_init(self, __context):
        if self.time <= 0:
            self.time = 300.0
```

参数名必须与 `.action` 文件中 Goal 字段完全一致。将类名加入文件末尾的 `__all__` 列表。

---

## 第五步：实现协议生成函数

路径：`unilabos/experiments/compile/<protocol_name>_protocol.py`

```python
import networkx as nx
from typing import List, Dict, Any


def generate_my_new_protocol(
    G: nx.DiGraph,
    vessel: dict,
    time: float = 300.0,
    mode: str = "default",
    **kwargs,
) -> List[Dict[str, Any]]:
    from unilabos.experiments.compile.utils.vessel_parser import get_vessel

    vessel_id, vessel_data = get_vessel(vessel)
    actions = []

    actions.append({
        "device_id": "target_device_id",
        "action_name": "some_action",
        "action_kwargs": {"param": "value"}
    })

    return actions
```

---

## 第六步：注册协议生成函数

在 `unilabos/experiments/compile/__init__.py` 中添加导入和映射：

```python
from .my_new_protocol import generate_my_new_protocol

action_protocol_generators = {
    # ... 已有协议
    MyNewProtocol: generate_my_new_protocol,
}
```

---

## 验证

```bash
python -c "from unilabos.experiments.models import MyNewProtocol; print(MyNewProtocol.model_fields)"
python -c "from unilabos.experiments.compile import action_protocol_generators; print(list(action_protocol_generators.keys()))"
```

---

## 现有协议速查

| 协议 | Pydantic 类 | 生成函数 | 核心参数 |
|------|-------------|---------|---------|
| 泵转移 | `PumpTransferProtocol` | `generate_pump_protocol_with_rinsing` | `from_vessel, to_vessel, volume` |
| 加样 | `AddProtocol` | `generate_add_protocol` | `vessel, reagent, volume` |
| 过滤 | `FilterProtocol` | `generate_filter_protocol` | `vessel, filtrate_vessel` |
| 溶解 | `DissolveProtocol` | `generate_dissolve_protocol` | `vessel, solvent, volume` |
| 加热/冷却 | `HeatChillProtocol` | `generate_heat_chill_protocol` | `vessel, temp, time` |
| 搅拌 | `StirProtocol` | `generate_stir_protocol` | `vessel, time` |

详见 [reference.md](reference.md)：协议运行时数据流、mock graph 测试模式、单位解析工具、复杂协议组合模式。
