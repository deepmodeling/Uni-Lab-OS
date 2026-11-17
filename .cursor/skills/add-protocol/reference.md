# 协议高级参考

本文件是 SKILL.md 的补充，包含协议运行时数据流、测试模式、单位解析工具和复杂协议组合模式。Agent 在需要实现这些功能时按需阅读。

---

## 1. 协议运行时数据流

从图文件到协议执行的完整链路：

```
实验图 JSON（unilab -g）
    ↓  legacy 适配层升级旧格式 → Graph Authority 登记（发号 / 模板 Site）
    ↓  load_graph_payload(权威 payload)（server/services/materials/graph.py）
physical_setup_graph (NetworkX Graph，边带 port；workstation_protocol.physical_setup_graph())
    ↓  ROS2WorkstationNode._setup_protocol_names(protocol_type)
为每个 protocol_name 创建 ActionServer
    ↓  收到 Action Goal
_create_protocol_execute_callback()
    ↓  convert_from_ros_msg_with_mapping(goal, mapping)
protocol_kwargs (Python dict)
    ↓  向 Host 查询 Resource 类型参数的当前状态
protocol_kwargs 更新（vessel 带上 children、data 等）
    ↓  protocol_steps_generator(G=physical_setup_graph(), **protocol_kwargs)
List[Dict] 动作序列
    ↓  逐步 execute_single_action / 并行 create_task
子设备 ActionClient 执行
```

### `_setup_protocol_names` 核心逻辑

```python
def _setup_protocol_names(self, protocol_type):
    if isinstance(protocol_type, str):
        self.protocol_names = [p.strip() for p in protocol_type.split(",")]
    else:
        self.protocol_names = protocol_type
    self.protocol_action_mappings = {}
    for protocol_name in self.protocol_names:
        protocol_type = globals()[protocol_name]  # 从 messages 模块取 Pydantic 类
        self.protocol_action_mappings[protocol_name] = get_action_type(protocol_type)
```

### `_create_protocol_execute_callback` 关键步骤

1. `convert_from_ros_msg_with_mapping(goal, action_value_mapping["goal"])` — ROS Goal → Python dict
2. 对 `Resource` 类型字段，通过 `resource_get` Service 查询 Host 的最新资源状态
3. `protocol_steps_generator(G=physical_setup_graph(), **protocol_kwargs)` — 调用编译函数
4. 遍历 steps：`dict` 串行执行，`list` 并行执行
5. `execute_single_action` 通过 `_action_clients[device_id]` 向子设备发送 Action Goal
6. 执行完毕后通过 `resource_update` Service 更新资源状态

---

## 2. 测试模式

### 2.1 协议文件内测试函数

许多协议文件末尾有 `test_*` 函数，主要测试参数解析工具：

```python
def test_dissolve_protocol():
    """测试溶解协议的各种参数解析"""
    volumes = ["10 mL", "?", 10.0, "1 L", "500 μL"]
    for vol in volumes:
        result = parse_volume_input(vol)
        print(f"体积解析: {vol} → {result}mL")

    masses = ["2.9 g", "?", 2.5, "500 mg"]
    for mass in masses:
        result = parse_mass_input(mass)
        print(f"质量解析: {mass} → {result}g")
```

### 2.2 使用 mock graph 测试协议生成器

推荐的端到端测试模式：

```python
import pytest
import networkx as nx
from unilabos.experiments.compile.stir_protocol import generate_stir_protocol


@pytest.fixture
def topology_graph():
    """创建测试拓扑图"""
    G = nx.DiGraph()
    G.add_node("flask_1", **{"class": "flask", "type": "container"})
    G.add_node("stirrer_1", **{"class": "virtual_stirrer", "type": "device"})
    G.add_edge("stirrer_1", "flask_1")
    return G


def test_generate_stir_protocol(topology_graph):
    """测试搅拌协议生成"""
    actions = generate_stir_protocol(
        G=topology_graph,
        vessel="flask_1",
        time="5 min",
        stir_speed=300.0
    )
    assert len(actions) >= 1
    assert actions[0]["device_id"] == "stirrer_1"
```

**要点：**
- 用 `nx.DiGraph()` 构建最小拓扑
- `add_node(id, **attrs)` 设置 `class`、`type`、`data` 等
- `add_edge(src, dst)` 建立物理连接
- 协议内的 `find_*` 函数依赖这些节点和边

---

## 3. 单位解析工具

路径：`unilabos/experiments/compile/utils/unit_parser.py`

| 函数 | 输入 | 返回 | 默认值 |
|------|------|------|--------|
| `parse_volume_input(input, default_unit)` | `"100 mL"`, `"2.5 L"`, `"500 μL"`, `10.0`, `"?"` | mL (float) | 50.0 |
| `parse_mass_input(input)` | `"19.3 g"`, `"500 mg"`, `2.5`, `"?"` | g (float) | 1.0 |
| `parse_time_input(input)` | `"30 min"`, `"1 h"`, `"300"`, `60.0`, `"?"` | 秒 (float) | 60.0 |

支持的单位：

- **体积**: mL, L, μL/uL, milliliter, liter, microliter
- **质量**: g, mg, kg, gram, milligram, kilogram
- **时间**: s/sec/second, min/minute, h/hr/hour, d/day

特殊值 `"?"`、`"unknown"`、`"tbd"` 返回默认值。

---

## 4. 复杂协议组合模式

以 `dissolve_protocol` 为例，展示如何组合多个子操作：

### 整体流程

```
1. 解析参数 (parse_volume_input, parse_mass_input, parse_time_input)
2. 设备发现 (find_connected_heatchill, find_connected_stirrer, find_solid_dispenser)
3. 判断溶解类型 (液体 vs 固体)
4. 组合动作序列:
   a. heat_chill_start / start_stir  (启动加热/搅拌)
   b. wait                           (等待温度稳定)
   c. pump_protocol_with_rinsing     (液体转移, 通过 extend 拼接)
      或 add_solid                   (固体加样)
   d. heat_chill / stir / wait       (溶解等待)
   e. heat_chill_stop                (停止加热)
```

### 关键代码模式

**设备发现 → 条件组合：**

```python
heatchill_id = find_connected_heatchill(G, vessel_id)
stirrer_id = find_connected_stirrer(G, vessel_id)
solid_dispenser_id = find_solid_dispenser(G)

actions = []

# 启动阶段
if heatchill_id and temp > 25.0:
    actions.append({
        "device_id": heatchill_id,
        "action_name": "heat_chill_start",
        "action_kwargs": {"vessel": {"id": vessel_id}, "temp": temp}
    })
    actions.append({"action_name": "wait", "action_kwargs": {"time": 30}})
elif stirrer_id:
    actions.append({
        "device_id": stirrer_id,
        "action_name": "start_stir",
        "action_kwargs": {"vessel": {"id": vessel_id}, "stir_speed": stir_speed}
    })

# 转移阶段（复用已有协议）
pump_actions = generate_pump_protocol_with_rinsing(
    G=G, from_vessel=solvent_vessel, to_vessel=vessel_id, volume=volume
)
actions.extend(pump_actions)

# 等待阶段
if heatchill_id:
    actions.append({
        "device_id": heatchill_id,
        "action_name": "heat_chill",
        "action_kwargs": {"vessel": {"id": vessel_id}, "temp": temp, "time": time}
    })
else:
    actions.append({"action_name": "wait", "action_kwargs": {"time": time}})
```

---

## 5. 关键路径

| 内容 | 路径 |
|------|------|
| 协议执行回调 | `unilabos/backend/ros2/presets/workstation.py` |
| ROS 消息映射 | `unilabos/backend/ros2/msgs/message_converter.py` |
| 物理拓扑图（装配） | `unilabos/server/services/materials/graph.py` (`load_graph_payload`) |
| 物理拓扑图（读取） | `unilabos/backend/runtime/workstation_protocol.py` (`physical_setup_graph()`) |
| 单位解析 | `unilabos/experiments/compile/utils/unit_parser.py` |
| 容器解析 | `unilabos/experiments/compile/utils/vessel_parser.py` |
| 溶解协议（组合示例） | `unilabos/experiments/compile/dissolve_protocol.py` |
