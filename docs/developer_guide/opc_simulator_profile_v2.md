# OPC 模拟器 Profile v2 生成规范（LLM / 人工）

本文档描述 `scripts/szlab_task_opc_simulator.py` 消费的 **schema v2** JSON。大模型生成配置时请严格遵循；生成结果写入 `task-orchestration/configs/`，在 workflow UI 下拉选择并打开配置工作台。

**权威 runnable 示例**：`scripts/config/szlab_task_opc_simulator.json`（前端 **「查看配置模板」** 可只读打开）。

**Workflow UI 保存/导入的用户配置**写入 `task-orchestration/configs/`（可在 OPC 模拟器区域下拉选择并加载；目录默认被 git 忽略，仅保留本地）。

---

## 1. 顶层结构（仅允许以下字段）

```json
{
  "schema_version": 2,
  "status": "draft",
  "name": "Workflow OPC Simulator",
  "opc": {
    "url": "opc.tcp://127.0.0.1:4840",
    "poll_interval": 0.2,
    "io_timeout": 2.0
  },
  "variables": [],
  "nodes": []
}
```

| 字段 | 约束 |
|------|------|
| `schema_version` | 必须为整数 `2` |
| `status` | `"draft"` 或 `"runnable"`；`runnable` 时任何校验错误都会导致拒绝加载 |
| `name` | 非空字符串，≤256 字符 |
| `opc.url` | `opc.tcp://host:port`，无 query/fragment/用户名密码 |
| `opc.poll_interval` | 0.05–60（秒） |
| `opc.io_timeout` | 0.1–60（秒） |
| `variables` | 非空数组，变量名唯一 |
| `nodes` | 非空数组，`workflow_node_id` 唯一 |

**禁止**根级额外字段；**禁止** `unknown` 作为 `direction` / `data_type`（UI 草稿可能显示 unknown，但 LLM 输出应直接用合法值）。

---

## 2. variables[] 条目

```json
{
  "name": "Robot_Home",
  "direction": "plc_to_pc",
  "data_type": "bool",
  "source": "manual",
  "initial_value": true
}
```

| 字段 | 约束 |
|------|------|
| `name` | 与 PLC CSV「变量名」完全一致，≤512 字符 |
| `direction` | `"pc_to_plc"`（上位机写 PLC）或 `"plc_to_pc"`（PLC 状态/传感器，上位机只读） |
| `data_type` | `"bool"` \| `"int"` \| `"float"` \| `"string"` |
| `source` | `"manual"` \| `"action_node"` \| `"action_sensor"` \| `"task_input"` \| `"task_output"` |
| `initial_value` | **可选**；仅当 `direction=plc_to_pc` 时允许；类型必须与 `data_type` 一致 |

### 2.1 direction 推断规则（生成时可直接采用）

**pc_to_plc**（名称包含任一子串）：

`工艺选择`、`参数写入完成`、`任务号`、`Robot_任务写入完成`、`取放料产品`、`取放料编号`、`倒料产品选择`

**plc_to_pc**（名称包含任一子串）：

`工艺完成`、`加工完成`、`允许加工`、`原点信号`、`准备信号`、`Robot_Home`、`Robot_任务允许写入`、`Robot_任务完成`、`工站状态`、`磁搅状态`、`读数稳定`、`剩余液量`、`传感器状态_上位机`

其余传感器/状态变量默认 `plc_to_pc`。

### 2.2 SZLab 机械臂握手变量（每个 robot 节点相关 profile 都应声明）

| 变量 | direction | data_type | source | initial_value |
|------|-----------|-----------|--------|---------------|
| Robot_Home | plc_to_pc | bool | manual | true |
| Robot_任务允许写入 | plc_to_pc | bool | manual | true |
| Robot_任务写入完成 | pc_to_plc | bool | manual | （不设） |
| 任务号 | pc_to_plc | int | action_node | （不设） |
| Robot_任务完成 | plc_to_pc | int | manual | 0 |

任务参数字段（如 `S03取放料产品`）为 `pc_to_plc` / `int` / `action_node`。

**任务号对照**（method → 任务号，摘自 `robot_tasks.py`）：

| method | 任务号 |
|--------|--------|
| submit_pick_from_s03 | 6 |
| submit_place_to_s072 | 15 |
| submit_pick_from_s072 | 16 |
| submit_place_to_s06 | 11 |
| submit_pick_from_s06 | 12 |
| submit_place_to_s04 | 7 |
| submit_pick_from_s04 | 8 |
| … | 见 `s12_robot/robot_contract.md` 完整表 |

---

## 3. nodes[] 条目

```json
{
  "workflow_node_id": "node_001_pick_from_s03",
  "task_template_ids": ["szlab-e2e-s07"],
  "device_id": "szlab_mixer_robot",
  "method": "submit_pick_from_s03",
  "params": {"product_type": 1, "position": "1-1"},
  "channel": "robot",
  "trigger": {"all": [
    {"variable": "Robot_任务写入完成", "operator": "eq", "value": true, "edge": "rising"},
    {"variable": "任务号", "operator": "eq", "value": 6, "edge": "level"}
  ]},
  "on_trigger": {"writes": [
    {"variable": "Robot_Home", "value": false},
    {"variable": "Robot_任务允许写入", "value": false},
    {"variable": "Robot_任务完成", "value": 0}
  ]},
  "on_complete": {"delay": 0.1, "writes": [
    {"variable": "Robot_任务完成", "value": 6},
    {"variable": "Robot_Home", "value": true},
    {"variable": "Robot_任务允许写入", "value": true}
  ]},
  "reset_when": {"all": [
    {"variable": "Robot_任务写入完成", "operator": "eq", "value": false, "edge": "level"}
  ]},
  "after_reset": {"delay": 0, "writes": []}
}
```

| 字段 | 约束 |
|------|------|
| `workflow_node_id` | 非空，全局唯一 |
| `task_template_ids` | 非空字符串数组 |
| `device_id` / `method` / `params` | 与 workflow 节点一致 |
| `channel` | 非空字符串；**runnable 必填**；同 channel 节点模拟器串行执行 |
| `trigger` | `{"all": [条件…]}`，**all 至少 1 条**（runnable） |
| `on_trigger` | `{"writes": [...]}`，无 delay |
| `on_complete` | `{"delay": number, "writes": [...]}`，**必须有 delay** |
| `reset_when` | 可选；`null` 或条件组 |
| `after_reset` | 仅当 `reset_when` 非 null 时允许；含 `delay` |

### 3.1 条件（trigger / reset_when）

```json
{"variable": "S07参数写入完成", "operator": "eq", "value": true, "edge": "rising"}
```

| 字段 | 约束 |
|------|------|
| `variable` | 必须已在 `variables[]` 中声明 |
| `operator` | 必须为 `"eq"` |
| `edge` | `"rising"` \| `"falling"` \| `"level"` |
| `value` | 类型必须与变量 `data_type` 一致（bool/int/float/string） |

### 3.2 写值（on_trigger / on_complete / after_reset）

```json
{"variable": "S07工艺完成", "value": 3}
```

- 只能写 `direction=plc_to_pc` 的变量（模拟器扮演 PLC 回写）
- `value` 类型必须与变量 `data_type` 一致

### 3.3 工站节点模式（S07 dose_powder）

- **trigger**：`S07参数写入完成=true`（rising）+ `S07工艺选择=<工艺号>`（level）
- **on_trigger**：`S07工艺完成=0`
- **on_complete**：`S07工艺完成=<与工艺选择相同>`
- **reset_when**：`S07参数写入完成=false`（level）
- **channel**：如 `"s07"`

### 3.4 工站节点模式（S06 run_solvent_addition）

- **trigger**：`S06参数写入完成=true`（rising）+ `S06工艺选择=<工艺号>`（level）
- **on_complete**：`S06加工完成=true`（bool）
- **channel**：如 `"s06"`

---

## 4. draft vs runnable

| | draft | runnable |
|---|-------|----------|
| 用途 | 保存半成品，UI 可继续编辑 | 可启动 OPC 模拟器 |
| channel / trigger | 可为空 | 必须完整 |
| 校验错误 | 允许存在 | **必须为零** |
| 启动模拟 | 否 | 是 |

生成时若不确定，先输出 `"status": "draft"`，补全节点后再改 `"runnable"`。

---

## 5. LLM 生成提示词（可直接复制）

```
你是 Uni-Lab OPC 模拟器配置生成器。只输出一个 JSON 对象，不要 markdown 代码块，不要解释。

要求：
1. schema_version=2，status=draft 或 runnable
2. 仅使用文档允许的字段；禁止 unknown direction/data_type
3. variables 覆盖 nodes 引用的全部变量；PLC→PC 状态变量设 initial_value
4. 每个 node：非空 channel；trigger.all 至少一条；on_complete 含 delay
5. trigger/on_complete 的 value 类型与 variables.data_type 严格一致
6. 写操作只能 target direction=plc_to_pc 的变量
7. szlab_mixer_robot 的 submit_* 节点：trigger 含 Robot_任务写入完成 rising + 任务号 level；on_trigger 清 Robot_Home/允许写入/任务完成；on_complete 写 Robot_任务完成=任务号并恢复握手

输入：workflow 节点列表（workflow_node_id, device_id, method, params, task_template_ids）
输出：完整 profile JSON
```

---

## 6. 校验与使用

1. **前端**：将 JSON 放入 `task-orchestration/configs/` → Task 页下拉选择配置 → **打开配置工作台** → 检查校验问题 → **校验并保存**
2. **CLI 校验**：`python -m scripts.szlab_task_opc_simulator --config your.json`（runnable 有错会拒绝启动）
3. **API**：`POST /api/opc-simulator/profiles:validate`

---

## 7. 参考文件

| 文件 | 说明 |
|------|------|
| `scripts/config/szlab_task_opc_simulator.json` | 六节点 runnable 全量示例 |
| `unilabos/.../s12_robot/robot_contract.md` | 机械臂任务号与握手 |
| `scripts/szlab_task_opc_simulator.py` | 解析器与状态机（最终权威） |
