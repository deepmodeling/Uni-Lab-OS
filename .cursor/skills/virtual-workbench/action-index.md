# Action Index — virtual_workbench

8 个动作，按功能分类。每个动作的参数 schema 在 `actions/<name>.json`（从当前注册表导出）。
`action_type` 调单动作时必须原样填写。

---

## 物料准备

### `auto-prepare_materials`

批量准备物料（虚拟起始节点），生成 A1-A5 物料编号，输出 `material_1` ~ `material_5`

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/prepare_materials.json`](actions/prepare_materials.json)
- **可选参数**: `count`（物料数量，默认 5）

---

## 机械臂 & 加热台操作

### `auto-move_to_heating_station`

将物料从 An 位置移动到空闲加热台（竞争机械臂，自动查找空闲加热台），输出 `station_id`、`material_number`

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_heating_station.json`](actions/move_to_heating_station.json)
- **核心参数**: `material_number`（物料编号，integer）

### `auto-start_heating`

启动指定加热台的加热程序（可并行，3 个加热台同时工作），加热完成后返回

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/start_heating.json`](actions/start_heating.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）
- **失败**: 加热台编号无效、加热台上没有物料、已经在加热

### `auto-move_to_output`

将加热完成的物料从加热台移动到输出位置 Cn

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_output.json`](actions/move_to_output.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）
- **失败**: 加热台编号无效、加热台上没有物料、尚未加热完成

> 这 4 个动作声明了 `sample_uuids`（样品追踪），由运行时注入：作业带样品时是样品映射，否则为空；
> 返回值的 `unilabos_samples` 原样回显。调用时不要自己传。

---

## 跨设备调用

### `call_peer`

调用另一台设备的动作并返回其结果（演示 `DeviceNode.call_device_action`）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/call_peer.json`](actions/call_peer.json)
- **核心参数**: `target_device`（目标设备 id，不能是工作台自己），`function_name`（目标动作名），
  `function_args`（JSON 字符串）

---

## 物料转移

### `transfer`

把物料经物料权威转移到目标设备上的承载物料（台面 / 载架 / 容器）

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/transfer.json`](actions/transfer.json)
- **核心参数**: `resource`, `target_device`, `mount_resource`
- **占位符字段**:
  - `resource` — **ResourceSlot** 列表，待转移的物料 `[{"uuid": "<物料 uuid>"}]`（执行期间加锁，必须带 uuid）
  - `target_device` — **DeviceSlot**，目标设备 id 字符串
  - `mount_resource` — **ResourceSlot** 列表，目标承载物料 `[{"uuid": "<物料 uuid>"}]`，与 `resource` 一一对应，不能是设备

---

## 人工确认与扣电测试

### `manual_confirm`

人工确认物料转移目标与扣电测试参数，确认后原样透传给下游节点（调度器挂起等人，不调用驱动）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/manual_confirm.json`](actions/manual_confirm.json)
- **核心参数**: `resource`、`target_device`、`mount_resource`、`collector_mass`、`active_material`、`capacity`、
  `battery_system`、`timeout_seconds`、`assignee_user_ids`（必须显式给出，`[]` 表示任何人可确认）
- **确认**: `POST /api/v1/workflow-node-jobs/<job_uuid>/manual-confirmation`，`{"action": "approve"}` 或 `"reject"`

### `test`

启动扣电测试（虚拟实现只记录参数）

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/test.json`](actions/test.json)
- **核心参数**: `resource`、`mount_resource`（带 uuid 的物料引用）、`collector_mass`、`active_material`、`capacity`、`battery_system`
