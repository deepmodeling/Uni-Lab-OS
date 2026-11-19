# Action Index — host_node

4 个动作，按功能分类。每个动作的完整 JSON Schema 在 `actions/<name>.json`。

---

## 资源管理

### `create_resource`

在资源树中创建新资源（容器、物料等），支持指定位置、类型和初始液体

- **action_type**: `ResourceCreateFromOuterEasy`
- **Schema**: [`actions/create_resource.json`](actions/create_resource.json)
- **可选参数**: `res_id`, `device_id`, `class_name`, `parent`, `bind_locations`, `liquid_input_slot`, `liquid_type`, `liquid_volume`, `slot_on_deck`
- **占位符字段**:
  - `res_id` — **ResourceSlot**（特例：目标物料可能尚不存在，直接填期望路径）
  - `device_id` — **DeviceSlot**，填路径字符串如 `"/host_node"`
  - `parent` — **NodeSlot**，填路径字符串如 `"/workstation/deck"`
  - `class_name` — **ClassSlot**，填类名如 `"container"`

### `test_resource`

测试资源系统，返回当前资源树和设备列表

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/test_resource.json`](actions/test_resource.json)
- **可选参数**: `resource`, `resources`, `device`, `devices`
- **占位符字段**:
  - `resource` — **ResourceSlot**，单个物料节点 `{id, name, uuid}`
  - `resources` — **ResourceSlot**，物料节点数组 `[{id, name, uuid}, ...]`
  - `device` — **DeviceSlot**，设备路径字符串
  - `devices` — **DeviceSlot**，设备路径字符串

---

## 系统工具

### `test_latency`

测试设备通信延迟，返回 RTT、时间差、任务延迟等指标

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/test_latency.json`](actions/test_latency.json)
- **参数**: 无（零参数调用）

---

## 人工确认

### `manual_confirm`

创建人工确认节点，等待用户手动确认后继续

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/manual_confirm.json`](actions/manual_confirm.json)
- **核心参数**: `timeout_seconds`（超时时间，秒）, `assignee_user_ids`（指派用户 ID 列表）
- **占位符字段**: `assignee_user_ids` — `unilabos_manual_confirm` 类型
