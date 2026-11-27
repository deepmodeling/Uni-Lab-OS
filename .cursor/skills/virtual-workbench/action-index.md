# Action Index — virtual_workbench

当前纳入 5 个动作，按功能分类。每个动作的完整 JSON Schema 在 `actions/<name>.json`。

暂跳过：`manual_confirm`、扣电测试 `test`。这两个动作需要启用时，先从最新 `req_device_registry_upload.json` 重新提取 schema 并校验参数。

---

## 物料准备

### `auto-prepare_materials`

批量准备物料（虚拟起始节点），生成 A1-A5 物料编号，输出 5 个 handle 供后续节点使用

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/prepare_materials.json`](actions/prepare_materials.json)
- **可选参数**: `count`（物料数量，默认 5）

---

## 机械臂 & 加热台操作

### `auto-move_to_heating_station`

将物料从 An 位置移动到空闲加热台（竞争机械臂，自动查找空闲加热台）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_heating_station.json`](actions/move_to_heating_station.json)
- **核心参数**: `material_number`（物料编号，integer）

### `auto-start_heating`

启动指定加热台的加热程序（可并行，3 个加热台同时工作）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/start_heating.json`](actions/start_heating.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）

### `auto-move_to_output`

将加热完成的物料从加热台移动到输出位置 Cn

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_output.json`](actions/move_to_output.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）

---

## 物料转移

### `transfer`

异步转移物料到目标设备（通过 ROS 资源转移）

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/transfer.json`](actions/transfer.json)
- **核心参数**: `resource`, `target_device`, `mount_resource`
- **占位符字段**:
  - `resource` — **ResourceSlot**，待转移的物料数组 `[{id, name, uuid}, ...]`
  - `target_device` — **DeviceSlot**，目标设备路径字符串
  - `mount_resource` — **ResourceSlot**，目标孔位数组 `[{id, name, uuid}, ...]`

---

## 暂跳过动作

### `manual_confirm`

创建人工确认节点，等待用户手动确认后继续（含物料转移上下文）。当前先不纳入推荐操作范围。

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/manual_confirm.json`](actions/manual_confirm.json)
- **状态**: 暂跳过。源码参数已包含扣电测试相关字段，历史 JSON 可能过期；需要启用时重新提取 schema。

### `test`

启动扣电测试。当前先不纳入本 skill。

- **状态**: 暂跳过。需要启用时从注册表生成 `actions/test.json` 后再补充索引。
