# Workflow 执行机制说明

本文档记录本地 workflow UI 与 Uni-Lab 执行引擎相关的讨论结论（2026-07-03）。

---

## 1. Graph Workflow 是否支持节点并行执行

### 结论

**不支持。** 当前 graph workflow（ReactFlow 画板 → workflow JSON → 本地执行）本质是**串行**执行，不是 DAG 并行调度。

### 执行链路

1. **拓扑排序**：按边做 DAG 排序，得到一条线性节点列表
2. **逐个执行**：`for node in ordered_nodes`，一个跑完再跑下一个

相关代码：

- `scripts/run_workflow_local.py` — `build_execution_order()`：拓扑排序，同层节点按 JSON 原始顺序排列
- `scripts/workflow_ui.py` — `WorkflowRunManager._run_payload()`：对 `ordered_nodes` 串行调用 `_run_node_with_live_opc_sampling()`

### 画板分叉的含义

画布上即使画成「分叉再汇合」（例如 A → B、A → C，B/C → D），也只是表达**依赖关系**，不会变成并行：

- B 和 C 若无互相依赖，引擎仍会把它们排进一条队列
- 按拓扑顺序**先 B 后 C**（或按 JSON 原始顺序），不会同时跑

### Uni-Lab 中其他层级的「并行」

| 层级 | 是否并行 | 说明 |
|------|----------|------|
| Graph workflow（本地 UI） | ❌ | 拓扑排序 + 串行执行 |
| Protocol 模式（ROS2 Workstation） | ⚠️ 有限 | 单个 protocol step 可以是「动作列表」，这些 action 会并行；step 之间仍串行 |
| 设备/应用层 | ⚠️ 特例 | 如虚拟工作台多加热台并行，是设备自己实现的，不是通用 workflow 引擎能力 |

Protocol 并行的实现（`unilabos/ros/nodes/presets/workstation.py`）：

- 当一步的 `action` 是 `list` 时，用 `create_task` 同时执行多个 action
- 这是**编译器/Protocol 作者**在一步里显式写 `[actionA, actionB]` 才能并行
- **不是**用户在画板上拖两条分支就能自动并行

### 对 S04 磁搅场景的含义

- 6 个磁搅位若画成 6 个并行分支，**当前不会 6 台同时搅**
- 实际会按拓扑顺序一台接一台跑
- 若要真正并行，需要改执行引擎：每轮找出所有「前置依赖已满足」的节点，并发执行，并处理设备冲突、OPC 客户端并发安全等

---

## 2. `always_free` 动作属性

### 结论

**有这个参数。** `always_free` 是 Uni-Lab 注册表/动作系统的标准属性，表示该动作**不占设备独占锁、不参与排队**，可以随时并发执行。

### 如何配置

**Python 装饰器（推荐）：**

```python
@action(auto_prefix=True, always_free=True, description="读取指定 PLC 变量")
def read_variable(self, name: str) -> Any:
    ...
```

或单独使用旧装饰器：

```python
@always_free
def some_query_method(self):
    ...
```

**Registry YAML（手写 registry 时）：**

```yaml
auto-trigger_station_process:
  always_free: true
  schema: ...
  type: UniLabJsonCommand
```

相关定义见 `unilabos/registry/decorators.py` 中 `@action(..., always_free=False)` 及 `@always_free` 装饰器。

### 实际行为

在 `DeviceActionManager`（云端/ROS 路径，`unilabos/app/ws_client.py`）中：

- **普通动作**：同一设备上同时只能有一个在跑，后续请求进入 `QUEUE`
- **`always_free=True`**：跳过排队，直接设为 `READY` 并立即执行

文档说明（`docs/developer_guide/add_PLC.md`）：

> `always_free: true` 表示该动作不占用工站独占锁（多检测站可并发执行）。

典型用途：**读变量、心跳、状态查询**等轻量操作。例如 `unilabos/devices/workstation/szlab_poly_studio/plc.py` 中的 `read_variable`、`write_variable` 等均标记了 `always_free=True`。

### 与「同名动作」的关系

**不是「同名就要加 `always_free`」**，而是看是否需要绕过设备级排队：

| 场景 | 是否需要 `always_free` |
|------|------------------------|
| 画板上多个节点都调 `run_stirring`，只是 `position` 不同 | 若希望**同一设备上并发**跑多个磁搅，才需要加 |
| 同一动作名、但应串行（一个跑完再跑下一个） | **不要加**（默认 `False`） |
| 读/写/查询类动作 | 通常**要加** |

当前 S04 磁搅动作 `run_stirring`（`magnetic_stirring.py`）**未**设置 `always_free`，默认串行——对长耗时、占工位的加工操作是合理默认。

### 与本地 workflow UI 的关系

本地 `scripts/run_workflow_local.py` 直接调用 `device.run_stirring(**param)`，**不经过** `DeviceActionManager` 排队。

因此 `always_free` 对本地 UI **基本不生效**，主要影响：

- 云端下发 job
- ROS Action 路径
- 同一设备上多个 job 的并发调度

### 若要让 6 个磁搅位并行（ROS/云端路径）

需同时满足：

1. 执行引擎支持并行（当前 graph workflow **不支持**，见上文第 1 节）
2. `run_stirring` 加 `always_free=True`（否则 ROS 路径会排队）
3. 驱动 / OPC 客户端线程安全（同一 `_client` 并发读写需确认）

---

## 3. 相关文件索引

| 主题 | 文件 |
|------|------|
| 拓扑排序与串行执行 | `scripts/run_workflow_local.py` |
| 本地 UI 运行管理 | `scripts/workflow_ui.py` |
| Protocol 步骤内并行 | `unilabos/ros/nodes/presets/workstation.py` |
| `always_free` 装饰器 | `unilabos/registry/decorators.py` |
| Job 排队与 `always_free` | `unilabos/app/ws_client.py` |
| Registry YAML 示例 | `docs/developer_guide/add_PLC.md` |
| S04 磁搅动作 | `unilabos/devices/workstation/szlab_poly_studio/s04_magnetic_stirring/magnetic_stirring.py` |
